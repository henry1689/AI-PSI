"""提案门禁：落库之前的**权威**门槛复核（阶段 6.5 §二.13–15）。

```
事件流（真相）
   ↓  重新查询
ExperienceReader.load()
   ↓  重新计算
PatternDetector.detect()  →  只取这一个键
   ↓  重新裁决
PromotionPolicy.decide()
   ↓
GateVerdict（结论由算出的事实派生，不是一个可以填的字段）
   ↓  必须携带
ProposalService.create()
```

🔴 **本模块存在的理由：让"已经被算过一次"这件事不再是一次信任。**

阶段 6 的生成链路上，门槛只被计算了一次，算完把结果（一个布尔）
往下传。于是任何能构造 ``PromotionDecision(allowed=True)`` 的调用方
都可以绕过门槛——而那个构造在 Python 里是一行代码。

§二.13 要求门禁**从仓储重新查询真实经验并重新计算门槛**。
做法有两层：

1. **输入里没有 ``PromotionDecision``。** 门禁只接受"要看哪个键"
   （错误类别 + 情境签名），其余全部自己算。§二.14 因此是
   **结构性成立**的——它根本没有那个参数可以信任。
2. **``authorised`` 是算出来的属性，不是构造参数。**
   即使有人绕过了凭据检查，他也必须同时伪造一个真正达标的
   ``ErrorPattern`` 与一份准许的 ``PromotionDecision``——
   而"真正达标"这件事由 :meth:`GateVerdict.authorised` 里那行
   与门槛的比较回答。

## 为什么在 application 层而不是 learning 层

``learning/`` 是纯函数：给定输入产出输出，不碰 IO。
而本模块要做的事情恰恰是**读存储**。把它放进 ``learning/``
会让整个包不再纯粹，也会让"learning 层不做任何 IO"这条
容易检查的规则变成"基本不做 IO"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, NoReturn
from uuid import UUID

from ai_psi.application.experience_reader import ExperienceLoad, ExperienceReader
from ai_psi.domain.common import is_blank
from ai_psi.domain.enums import ErrorType
from ai_psi.domain.exceptions import InvalidRequestError
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD
from ai_psi.learning.evaluation_weighting import DEFAULT_WEIGHTING, EvaluationWeighting
from ai_psi.learning.pattern_detector import ErrorPattern, PatternDetector, PatternScan
from ai_psi.learning.promotion_policy import (
    SEVERE_ERROR_TYPES,
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
)

__all__ = ["GateEvidence", "GateVerdict", "ProposalGate"]


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """门禁需要的**非模式**证据（§11.3 条件二～五）。

    🔴 **这里没有 ``pattern`` 字段，也不会有。**

    模式是"发生了多少次"这个问题，而它必须由门禁**自己**从存储里
    重查重算——接受调用方传来的模式，等于接受调用方传来的结论。
    其余四条条件（严重错误 + 修复方向、离线退化、用户纠正、模块
    连低于阈值）的输入无法从经验里推出，只能由调用方提供，
    因此它们在这里，且**只影响理由与附加触发条件，不影响次数门槛**。

    ⚠️ 拿不到观测时传 ``None``，不要传 ``False``/``0``——
    合并两者会让系统永远只从最好数的那个条件产生提案
    （ADR-0018 §3）。
    """

    fix_direction: str | None = None
    offline_regression: bool | None = None
    module_streak: int | None = None
    user_corrections: int | None = None
    user_correction_shows_systemic_issue: bool = False


#: 只有本模块能拿到的构造凭据。
#:
#: 🔴 **这不是密码学意义上的防伪**——Python 进程内没有那种东西。
#: 它的作用是让"随手构造一个 ``GateVerdict()`` 然后往上填数"立刻炸掉，
#: 而不是安静地生效。真正的防线是 :attr:`GateVerdict.authorised`
#: 那行比较，以及"所有数字都来自一次真实读取"这件事本身。
_GATE_TOKEN: Final[object] = object()


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """门禁的结论。

    🔴 **结论由算出的事实派生，不是一个可以填进去的字段。**
    :attr:`authorised` 每次读取时重算，因此"结论说可以、证据说不行"
    这种状态不存在。

    Attributes:
        error_type: 被复核的错误类别。
        situation_signature: 被复核的情境签名。
        threshold: 复核时用的门槛。
        pattern: 重新计算出的模式；未达门槛时为 ``None``。
        decision: 重新做出的裁决；未达门槛时为 ``None``。
        reasons: 逐条理由——**包括未授权时为什么没授权**。
        data_quality: 本次读取的完整性统计。它随结论一起交出去，
            因为"读到 3 条"与"读到 3 条、另有 2 条读不回来"
            是强度不同的两次授权。
    """

    error_type: ErrorType
    situation_signature: str
    threshold: int
    pattern: ErrorPattern | None = None
    decision: PromotionDecision | None = None
    reasons: tuple[str, ...] = field(default=())
    data_quality: str = ""
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """拒绝任何不是由 :class:`ProposalGate` 构造出来的实例。

        Raises:
            InvalidRequestError: 缺少门禁凭据。
        """
        if self._token is not _GATE_TOKEN:
            msg = (
                "GateVerdict 只能由 ProposalGate 构造。"
                "手工构造的结论没有经过任何读取与重算——"
                "阶段 6.5 §二.15 不允许它授权任何落库"
            )
            raise InvalidRequestError(msg, context={"error_type": self.error_type.value})

    @property
    def authorised(self) -> bool:
        """是否授权落库。

        🔴 **三个条件同时成立才算数，任何一个缺失都是拒绝：**

        * 确实找到了一个达到门槛的模式；
        * 裁决准许；
        * **加权计数不低于门槛**——这一条是冗余的（前两条蕴含它），
          但它被显式写出来，因为它是整个门禁唯一真正要回答的问题。
          将来有人放宽前两条中的任何一条时，这一条仍然会拦住
          一份不达标的证据。
        """
        return (
            self.pattern is not None
            and self.decision is not None
            and self.decision.allowed
            and self.pattern.weighted_count >= self.threshold
        )

    @property
    def key(self) -> tuple[str, str]:
        """本次复核针对的 ``(错误类别, 情境签名)``。"""
        return (self.error_type.value, self.situation_signature)

    @property
    def verified_pattern(self) -> ErrorPattern:
        """已被授权的模式。

        🔴 **它把"``authorised`` 为真时模式必然存在"这条蕴含
        变成一个可读的入口**，而不是让每个调用方各写一遍
        ``if verdict.pattern is None``。那些重复的检查检查的是
        同一件事，而它们与 :attr:`authorised` 之间没有任何机制
        保证同步——迟早会有一处漏掉。

        Returns:
            重新计算出的、达到门槛的模式。

        Raises:
            InvalidRequestError: 本次结论未授权。
        """
        return self._require_pattern()

    @property
    def verified_decision(self) -> PromotionDecision:
        """已被授权的裁决。

        Returns:
            重新做出的裁决。

        Raises:
            InvalidRequestError: 本次结论未授权。
        """
        if self.decision is None:
            self._raise_not_authorised()
        return self.decision

    def _require_pattern(self) -> ErrorPattern:
        if self.pattern is None:
            self._raise_not_authorised()
        return self.pattern

    def _raise_not_authorised(self) -> NoReturn:
        msg = (
            f"本次门禁结论未授权（{self.error_type.value} / "
            f"{self.situation_signature}）：{'；'.join(self.reasons) or '未给出理由'}"
        )
        raise InvalidRequestError(msg, context={"error_type": self.error_type.value})

    @property
    def recomputed_occurrences(self) -> int:
        """重新计算出的独立发生次数。"""
        return self.pattern.count if self.pattern is not None else 0

    @property
    def recomputed_weighted_count(self) -> int:
        """重新计算出的加权计数。"""
        return self.pattern.weighted_count if self.pattern is not None else 0

    @property
    def evidence_experience_ids(self) -> tuple[UUID, ...]:
        """支撑本次授权的经验 id（升序）。"""
        return self.pattern.experience_ids if self.pattern is not None else ()


class ProposalGate:
    """从存储重新计算门槛，决定一个模式能否落库成提案。"""

    def __init__(
        self,
        reader: ExperienceReader,
        *,
        threshold: int = PROPOSAL_ESCALATION_THRESHOLD,
        weighting: EvaluationWeighting = DEFAULT_WEIGHTING,
    ) -> None:
        """初始化。

        Args:
            reader: 经验读取器。🔴 与主链路**同一个实例**——
                另造一个不会出错，但会让"读到的是什么"有两种口径。
            threshold: 门槛。
            weighting: 评价权重表。必须与检测器、裁决器一致。

        Raises:
            ValueError: 门槛低于 2（不变量 10）。
        """
        if threshold < 2:
            msg = "门禁门槛不得低于 2：单次经验不足以支撑全局策略（不变量 10）"
            raise ValueError(msg)
        self._reader = reader
        self._threshold = threshold
        self._weighting = weighting
        self._detector = PatternDetector(threshold=threshold, weighting=weighting)
        self._policy = PromotionPolicy(threshold=threshold, weighting=weighting)

    @property
    def threshold(self) -> int:
        """当前门槛。"""
        return self._threshold

    @property
    def weighting(self) -> EvaluationWeighting:
        """当前使用的评价权重表。

        暴露它是因为调用链路的那一头（``LearningService``）要用
        **同一份**权重跑一遍模式发现来生成待办清单。两份不同的
        权重表会让"链路发现了 N 个模式"与"门禁批准了 0 个"同时成立，
        而两者都不报错。
        """
        return self._weighting

    async def review(
        self,
        *,
        error_type: ErrorType,
        situation_signature: str,
        evidence: GateEvidence | None = None,
    ) -> GateVerdict:
        """重新查询、重新计算、重新裁决。

        🔴 **输入里没有 ``PromotionDecision``，也没有 ``ErrorPattern``。**
        这是 §二.14 的结构性实现：调用方**无法**把"我已经算过了"
        这件事告诉门禁——它根本没有那个参数可以传。

        Args:
            error_type: 要复核的错误类别。
            situation_signature: 要复核的情境签名。
            evidence: 其余四条条件的观测输入（可选）。

        Returns:
            门禁结论。**未授权也是结论**，理由在其中。

        Raises:
            InvalidRequestError: 情境签名为空白。
        """
        if is_blank(situation_signature):
            msg = "情境签名不得为空白：门禁无法复核一个没有范围的模式"
            raise InvalidRequestError(msg, context={"error_type": error_type.value})

        load = await self._reader.load()
        scan = self._detector.detect(load.assessments)
        pattern, suppressed = _find(scan, error_type=error_type, signature=situation_signature)
        quality = _describe_quality(load)

        if pattern is None:
            reasons = list(suppressed) or [
                f"{error_type.value} 在情境 {situation_signature} 下"
                f"没有任何达到门槛的独立发生（门槛 {self._threshold}）"
            ]
            return GateVerdict(
                error_type=error_type,
                situation_signature=situation_signature,
                threshold=self._threshold,
                reasons=tuple(reasons),
                data_quality=quality,
                _token=_GATE_TOKEN,
            )

        supplied = evidence if evidence is not None else GateEvidence()
        decision = self._policy.decide(
            PromotionEvidence(
                pattern=pattern,
                fix_direction=(
                    supplied.fix_direction if error_type in SEVERE_ERROR_TYPES else None
                ),
                offline_regression=supplied.offline_regression,
                module_streak=supplied.module_streak,
                user_corrections=supplied.user_corrections,
                user_correction_shows_systemic_issue=(
                    supplied.user_correction_shows_systemic_issue
                ),
            )
        )
        return GateVerdict(
            error_type=error_type,
            situation_signature=situation_signature,
            threshold=self._threshold,
            pattern=pattern,
            decision=decision,
            reasons=decision.reasons,
            data_quality=quality,
            _token=_GATE_TOKEN,
        )


def _find(
    scan: PatternScan,
    *,
    error_type: ErrorType,
    signature: str,
) -> tuple[ErrorPattern | None, tuple[str, ...]]:
    """在扫描结果里找指定的键。

    Returns:
        ``(合格模式或 None, 未达门槛的理由)``。
    """
    for pattern in scan.patterns:
        if pattern.error_type is error_type and pattern.situation_signature == signature:
            return pattern, ()
    for item in scan.suppressed:
        if item.error_type is error_type and item.situation_signature == signature:
            return None, item.reasons
    return None, ()


def _describe_quality(load: ExperienceLoad) -> str:
    """把读取的完整性写成一句话。

    🔴 **它随结论一起交给落库方。**
    "读到 3 条" 与 "读到 3 条、另有 2 条读不回来" 是不同强度的授权，
    而后者恰恰是最需要被看见的情形——那些读不回来的记录里
    可能就有把门槛顶过去的那一条。
    """
    parts = [
        f"经验 {len(load.assessments)} 条",
        f"读不回来 {load.unreadable_experiences} 条",
        f"评价读不回来 {load.unreadable_evaluations} 条",
        f"孤立评价 {load.orphan_evaluations} 条",
    ]
    return "；".join(parts)
