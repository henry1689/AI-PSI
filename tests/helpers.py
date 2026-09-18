"""测试辅助工具。

本模块只放一类东西：**让"故意构造非法对象"这件事有一个显式出口。**

为什么需要它——验证"非法输入必须被拒绝"的测试，本质上就是要把
静态类型检查不认可的值传进构造函数。mypy 会正确地报错，但这里它
报的不是缺陷，而是测试的**意图**。

两个可选做法各有一个缺点：

* ``# type: ignore`` —— 会同时屏蔽该行的**真实**类型错误，
  而且无法从代码上区分"我故意的"与"我写错了"；
* 对测试整体关闭 ``arg-type`` —— 会放过测试代码里真正的类型错误。

因此改为一个带类型的显式出口：函数名本身说明了意图，
调用点一眼可辨，且不影响其他任何检查。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ai_psi.application.artifact_service import ArtifactService, RoundScope
from ai_psi.application.feedback_service import FeedbackService
from ai_psi.application.round_service import CognitiveRoundService
from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    ErrorType,
    EventType,
    ExperienceEvaluation,
    ExperienceEvaluator,
    ExperienceKind,
    FeedbackType,
)
from ai_psi.domain.experiences import (
    EXTRACTOR_VERSION,
    Experience,
    canonical_key_for,
    evaluation_target_for_judgment,
    independence_group_for,
)

__all__ = ["construct", "rejects", "seed_learning_evidence"]


def construct[T: BaseModel](model: type[T], /, **fields: Any) -> T:
    """构造领域对象，**有意绕过静态类型检查**。

    用于「字段值合法，但静态类型看不出来」的场景，例如从
    异构字典解包构造。若只是想验证非法输入被拒绝，请用 :func:`rejects`。

    Args:
        model: 目标领域对象类型。
        **fields: 传给构造函数的字段。

    Returns:
        构造出的实例。

    Raises:
        pydantic.ValidationError: 输入非法。
    """
    return model(**fields)


def rejects[T: BaseModel](model: type[T], /, **fields: Any) -> ValidationError:
    """断言构造**必定失败**，并返回捕获到的校验错误。

    🔴 不要用裸 ``construct()`` 来测拒绝路径——
    如果校验意外地没有触发，裸调用会**静默通过**，
    测试就成了摆设。本函数在构造成功时主动失败。

    Args:
        model: 目标领域对象类型。
        **fields: 应当触发校验失败的字段。

    Returns:
        捕获到的 ``ValidationError``，供进一步断言字段路径。

    Raises:
        AssertionError: 构造**成功**了——即系统接受了一个非法输入。
    """
    try:
        model(**fields)
    except ValidationError as exc:
        return exc
    pytest.fail(f"{model.__name__} 接受了本应被拒绝的输入：{sorted(fields)}")


async def seed_learning_evidence(
    *,
    round_service: CognitiveRoundService,
    artifact_service: ArtifactService,
    feedback_service: FeedbackService,
    rounds: int = 3,
    error_type: ErrorType | None = None,
    situation_signature: str = "reasoning|d2|multi_source",
    confirm: bool = True,
    user_id: UUID | None = None,
) -> list[UUID]:
    """为**不针对学习链路本身**的测试准备一批经验。

    🔴 **这不是一条黑盒路径，用之前请先读这段。**

    它直接用生产写经验的那条调用（``ArtifactService.record`` +
    ``experience.created``），只是负载是**合成**的——因为它跳过了
    认知流水线，没有真实回合跑在里面。

    因此它有明确的使用边界：

    * ✅ **可以**用于「提案生命周期」「反馈与记忆」这类测试——
      它们要的是"已经有三条被确认过的经验"这个**前提**，
      而不是"经验是怎么产生的"这个结论；
    * ❌ **不可以**用于学习链路、提案门槛或阶段 6.5 §七 的黑盒验收。
      那些测试必须让经验从**真实回合**里长出来，
      否则它们证明的只是"这个辅助函数好使"。

    阶段 6.5 把这条边界写在函数名和文档里，而不是靠约定——
    因为"测试全绿而结论是假的"正是阶段 6 翻过的那次车。

    ⚠️ 刻意**不接收整个容器**：那样这个辅助函数就会依赖组合根的
    形状，而它需要的其实只是三个服务。参数列出来，调用方一眼能看出
    它到底动用了什么。

    Args:
        round_service: 回合服务（造真实回合）。
        artifact_service: 产物服务（走生产写经验的那条调用）。
        feedback_service: 反馈服务（把经验抬到被确认的档位）。
        rounds: 造几个回合（每个回合一条经验）。
        error_type: 经验里的错误类别；``None`` 时用 ``REASONING_ERROR``。
        situation_signature: 情境签名——它决定这些经验是否"同类"。
        confirm: 是否给每个回合发一条 ``correction`` 反馈。
            🔴 只有 ``True`` 时经验才会被抬到 ``CONFIRMED``，
            门槛才可能被跨过（``SUSPECTED`` 的默认权重是 0）。
        user_id: 归属用户。

    Returns:
        造出来的经验 id 列表。
    """
    resolved_error = error_type if error_type is not None else ErrorType.REASONING_ERROR
    created: list[UUID] = []

    for _ in range(rounds):
        started = await round_service.start_round(
            created_by="test_helper",
            user_id=user_id,
            depth_level=CognitiveDepth.D2,
        )
        round_id = started.round.id
        judgment_id = uuid4()
        target = evaluation_target_for_judgment(judgment_id)
        experience = Experience(
            created_by="test_helper",
            cognitive_round_id=round_id,
            judgment_id=judgment_id,
            situation_signature=situation_signature,
            inquiry_type="factual",
            error_type=resolved_error,
            attribution_confidence=ConfidenceBand.MODERATE,
            # 🔴 抽取时刻最多 SUSPECTED——辅助函数也绕不过这条
            evaluation=ExperienceEvaluation.SUSPECTED,
            evaluator_type=ExperienceEvaluator.INTERNAL_METACOGNITION,
            evaluator_version="test-helper/1",
            experience_kind=ExperienceKind.ROUND_OUTCOME,
            evaluation_target=target,
            extractor_version=EXTRACTOR_VERSION,
            independence_group=independence_group_for(
                idempotency_key=None, cognitive_round_id=round_id
            ),
            canonical_key=canonical_key_for(
                cognitive_round_id=round_id,
                evaluation_target=target,
                experience_kind=ExperienceKind.ROUND_OUTCOME,
                extractor_version=EXTRACTOR_VERSION,
            ),
        )
        await artifact_service.record(
            scope=RoundScope(
                cognitive_round_id=round_id,
                correlation_id=started.round.correlation_id or uuid4(),
                user_id=user_id,
            ),
            event_type=EventType.EXPERIENCE_CREATED,
            payload={"experience": experience.model_dump(mode="json")},
            actor_id="test_helper",
        )
        if confirm:
            await feedback_service.record(
                round_id=round_id,
                feedback_type=FeedbackType.CORRECTION,
                content="你这里判断错了",
                actor_id="test_helper",
            )
        created.append(experience.id)

    return created
