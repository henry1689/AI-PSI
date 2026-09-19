"""Golden Case 的确定性执行器（阶段 7 · S1a）。

## 用的是哪条路径

🔴 **走的是正式组合根**（``container.build_container``），不是另拼一套认知流程。

评测若自己拼一套"迷你认知系统"，验证的就不是这个系统了（ADR-0019 的教训：
死代码之所以长期没被发现，正是因为没有任何入口真的走它）。
因此这里只做三件事：装配一个**内存 + Mock** 的容器、喂输入、读真实结构化产物。

## 隔离边界（S1a 要求的部分）

| 边界 | 怎么保证 |
|---|---|
| 不发网络请求 | ``llm_provider="mock"``；装配后**显式断言** provider 就是 Mock，
  而不是"配置写对了" |
| 不需要任何凭据 | ``Settings(_env_file=None, ...)``——**不读开发机上的 ``.env``** |
| 不写生产数据库 | ``storage_backend="memory"``，全程在进程内存里 |
| 不触发生产学习提案 | 同上：学习链路写的是内存存储，进程结束即消失 |
| 不改归因规则 / 提示词 / 配置默认值 | 本模块只读这些，一个都没碰 |

⚠️ **S2 的隔离工作不在本切片**：独立评测数据库、跨运行隔离、"生产库行数不变"
的钉子测试都属于 S2。S1a 的隔离靠"内存后端"这一条，已经足够，
但它**不是**"评测绝不污染生产"的完整证明。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.application.cognitive_runtime import (
    CognitiveRuntime,
    RoundOutcome,
    RoundRequest,
)
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.cognition.projection import project_artifacts
from ai_psi.config import Settings
from ai_psi.container import build_container
from ai_psi.domain.enums import EventType
from ai_psi.domain.events import Event
from ai_psi.evaluation.assertions import (
    AssertionResult,
    CaseObservation,
    evaluate,
)
from ai_psi.evaluation.executors import CaseExecution, CaseExecutor
from ai_psi.evaluation.manifest import (
    ReproducibilityManifest,
    prompt_versions_from_invocations,
)
from ai_psi.evaluation.models import GoldenCase

__all__ = [
    "CaseResult",
    "ExecutionMode",
    "GoldenRunner",
    "MockRuntime",
    "RunResult",
    "StorageIsolation",
    "build_mock_runtime",
    "deterministic_settings",
]


class ExecutionMode(StrEnum):
    """评测的执行模式（阶段 7）。

    * ``in_memory`` —— S1a 的路径：内存后端 + Mock，直接调运行时。
      它证明案例契约与断言判定，**不**证明正式 HTTP 路径；
    * ``postgres_http`` —— S2 的路径：专用 PostgreSQL + 正式 HTTP 路由。
    """

    IN_MEMORY = "in_memory"
    POSTGRES_HTTP = "postgres_http"


class StorageIsolation(BaseModel):
    """存储隔离证据（只有 ``postgres_http`` 模式才有）。

    🔴 这里只有**库名**与**布尔结论**：没有完整 URL、没有用户名、
    没有密码、没有主机凭据。报告会被提交到讨论区、粘进 issue，
    三者都不该在那里出现。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_database: str
    reference_database: str
    same_database: bool
    reference_snapshot_unchanged: bool
    reference_learning_state_unchanged: bool


#: 结果格式自身的版本。**与案例的 ``schema_version`` 是两回事**：
#: 前者说"这份 JSON 长什么样"，后者说"案例文件长什么样"。
RESULT_SCHEMA_VERSION: Final[int] = 1

#: 本切片唯一允许的 Provider。见 :func:`build_mock_runtime` 的守卫。
_MOCK_PROVIDER: Final[str] = "mock"


def deterministic_settings() -> Settings:
    """评测专用的确定性配置。

    🔴 ``_env_file=None`` 是**必须**的：否则一份评测的结果会取决于
    "这台机器上的 ``.env`` 里恰好配了什么"。这与 CI 上那三个配置用例
    失败是同一类问题（阶段 7 前置修复，提交 ``d9421a0``）。
    """
    return Settings(
        _env_file=None,
        storage_backend="memory",
        llm_provider="mock",
        llm_model="mock-model-v1",
    )


@dataclass(frozen=True, slots=True)
class MockRuntime:
    """装配好的 Mock 运行时及其依赖。

    它**同时**是 :class:`~ai_psi.evaluation.executors.CaseExecutor` 的一个实现：
    有 ``provider_name``，也有 ``execute``。于是 ``GoldenRunner`` 可以在
    "直接调运行时"与"走正式 HTTP"之间替换执行器，而断言判定与结果序列化
    两个切片一行都不用改。
    """

    runtime: CognitiveRuntime
    uow_factory: UnitOfWorkFactory
    provider_name: str

    async def execute(self, case: GoldenCase) -> CaseExecution:
        """执行一条案例（内存路径）。

        ⚠️ **不捕获异常**：回合执行期的异常原样上抛，由
        :meth:`GoldenRunner.run_case` 统一记成结构化失败。在这里吞掉它
        会让"跑失败"与"跑成功但断言不过"变成同一个东西。

        Args:
            case: 待执行的案例。

        Returns:
            回合 id 与观测。
        """
        outcome = await self.runtime.run_round(
            RoundRequest(
                user_message=case.stimulus.input,
                user_id=uuid4(),
                requested_depth=case.stimulus.requested_depth,
                conversation_summary=tuple(case.stimulus.user_context.conversation_summary),
                open_questions=tuple(case.stimulus.user_context.open_questions),
                confirmed_user_goals=tuple(case.stimulus.user_context.confirmed_user_goals),
                system_status=tuple(case.stimulus.user_context.system_status),
            )
        )
        return CaseExecution(
            cognitive_round_id=outcome.cognitive_round_id,
            observation=await self.observe(outcome),
            prompt_versions=await self.prompt_versions(outcome.cognitive_round_id),
        )

    async def prompt_versions(self, round_id: UUID) -> Mapping[str, str]:
        """读本回合**实际调用过**的模型任务及其 Prompt 版本。

        🔴 数据源是事件流里的模型调用记录（不变量 18 要求每次调用都写
        ``task_name`` 与 ``prompt_version``），因此它证明的是"真的用了什么"，
        而不是"代码里注册了什么"。
        """
        events = await self.read_events(round_id)
        artifacts = project_artifacts(events)
        return prompt_versions_from_invocations(
            item.model_dump(mode="json") for item in artifacts.model_invocations
        )

    async def observe(self, outcome: RoundOutcome) -> CaseObservation:
        """读事件流并整理出观测。"""
        events = await self.read_events(outcome.cognitive_round_id)
        artifacts = project_artifacts(events)
        analysis_kinds = tuple(
            sorted(
                {
                    str(record.payload["analysis_kind"])
                    for record in artifacts.of_type(EventType.COGNITION_ANALYSIS_COMPLETED)
                    if "analysis_kind" in record.payload
                }
            )
        )
        return observation_of(
            outcome,
            hypothesis_count=len(artifacts.of_type(EventType.HYPOTHESIS_CREATED)),
            analysis_kinds=analysis_kinds,
        )

    async def read_events(self, round_id: UUID) -> list[Event]:
        """按回合读取事件流（**只读**，不开事务写任何东西）。"""
        async with self.uow_factory() as uow:
            return await uow.events.read_stream(cognitive_round_id=round_id)


def build_mock_runtime() -> MockRuntime:
    """装配一个内存 + Mock 的运行时。

    Returns:
        运行时、工作单元工厂与 provider 名。

    Raises:
        RuntimeError: 配置里写的不是 mock，或装配出来的 provider 不是 Mock。
            **不静默继续**——"配置写的是 mock"与"现在跑的真是 mock"
            是两件事，而后者才是评测结果能否复现的前提。
    """
    settings = deterministic_settings()
    if settings.llm_provider != _MOCK_PROVIDER:
        msg = f"评测只允许 {_MOCK_PROVIDER!r} Provider，配置里写的是 {settings.llm_provider!r}"
        raise RuntimeError(msg)

    container = build_container(settings)
    provider = container.provider
    # 🔴 **不能用 isinstance 判断**：组合根会把 Provider 包进熔断装饰器
    # （``ResilientProvider``），于是 isinstance(MockProvider) 永远是 False。
    # 装饰器的 ``name`` 属性按文档"沿用内层的"，因此它是这里可用的公开身份。
    # 这一条**实测踩过**：第一版写成 isinstance，评测第一条案例就装配失败。
    if provider.name != _MOCK_PROVIDER:
        msg = (
            f"评测只允许 {_MOCK_PROVIDER!r} Provider，"
            f"装配出来的是 {type(provider).__name__}（name={provider.name!r}）；"
            "拒绝在非确定性 Provider 上执行 Golden Case"
        )
        raise RuntimeError(msg)
    return MockRuntime(
        runtime=container.runtime,
        uow_factory=container.uow_factory,
        provider_name=provider.name,
    )


class CaseResult(BaseModel):
    """一条案例的执行结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    #: 运行时生成的回合 id——**易变字段**，只出现在原始输出里，
    #: 用来在失败时回到那个回合的事件流。canonical 输出不含它。
    cognitive_round_id: UUID | None = None
    #: 真实观测。回合根本没跑起来时为 ``None``（不是"观测到空"）。
    observation: CaseObservation | None = None
    assertions: tuple[AssertionResult, ...] = ()
    passed: bool
    #: **稳定**的失败摘要：只用断言名与异常类型名，不含路径、时间、内存地址。
    failure_reason: str | None = None
    #: 可能不稳定的失败细节（异常原文等）。**只出现在原始输出里。**
    failure_detail: str | None = None


class RunResult(BaseModel):
    """一次数据集运行的结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = RESULT_SCHEMA_VERSION
    case_type: str
    provider: str
    total: int
    passed: int
    failed: int
    passed_overall: bool
    cases: tuple[CaseResult, ...] = Field(default_factory=tuple)
    #: 本次运行走的是哪条执行路径（阶段 7 · S2）。
    execution_mode: str = ExecutionMode.IN_MEMORY.value
    #: 存储隔离证据；``in_memory`` 模式下为 ``None``（那时没有第二个库）。
    storage_isolation: StorageIsolation | None = None
    #: 可复现性清单（阶段 7 · S3）。
    #:
    #: 🔴 原始报告带**完整**清单；canonical 只带其中的稳定身份子集
    #: （见 :meth:`~ai_psi.evaluation.manifest.ReproducibilityManifest.canonical_identity`）。
    manifest: ReproducibilityManifest | None = None


def observation_of(
    outcome: RoundOutcome,
    *,
    hypothesis_count: int,
    analysis_kinds: tuple[str, ...],
) -> CaseObservation:
    """把一个回合的产物整理成可断言的观测。

    三个来源各司其职：

    * ``RoundOutcome`` —— 终态、深度、停止原因、回答、判断、调用数、循环数；
    * 事件流投影 —— ``hypothesis.created`` 条数与 ``cognition.analysis.completed``
      的 ``analysis_kind``（这两样**只**存在于事件流里）；
    * 判断对象 —— 置信档、认知动作、未解未知、反证。

    Args:
        outcome: 回合结果。
        hypothesis_count: 该回合 ``hypothesis.created`` 事件的条数。
        analysis_kinds: 该回合实际执行的分析模块（已排序）。

    Returns:
        只含**有真实来源**字段的观测。
    """
    judgment = outcome.judgment
    return CaseObservation(
        state=outcome.state.value,
        depth=outcome.depth.value,
        stop_reason_present=outcome.stop_reason is not None,
        response_present=outcome.response_text is not None,
        response_text=outcome.response_text,
        judgment_present=judgment is not None,
        model_calls_used=outcome.model_calls_used,
        metacognitive_loops=outcome.metacognitive_loops,
        confidence_band=None if judgment is None else judgment.confidence_band.value,
        epistemic_action=(
            None if judgment is None else judgment.recommended_epistemic_action.value
        ),
        unresolved_unknown_count=(None if judgment is None else len(judgment.unresolved_unknowns)),
        counterargument_count=(
            None if judgment is None else len(judgment.strongest_counterarguments)
        ),
        hypothesis_count=hypothesis_count,
        analysis_kinds=analysis_kinds,
    )


def _unevaluated(
    mode: str,
    name: str,
    expected: str | int | bool,
    reason: str,
) -> AssertionResult:
    """回合没跑起来时的断言占位。

    🔴 ``observed=None``（**不可观测**），``passed=False``。
    绝不写成 ``observed=False``——那不是"观测到不成立"，那是**伪造观测**，
    而一条被伪造的 ``response_present: false`` 会让失败的案例看起来像通过的。
    """
    return AssertionResult(
        name=name,
        mode=mode,
        expected=expected,
        observed=None,
        passed=False,
        detail=f"[不可观测] {reason}",
    )


class GoldenRunner:
    """在某个**执行器**上执行 Golden Case。

    🔴 **执行器是可替换的。** S1a 的"直接调运行时"（内存）与 S2 的
    "走正式 HTTP"（PostgreSQL）都实现 :class:`CaseExecutor`；断言判定与
    结果序列化对两者一视同仁——它们本来就不该知道回合是怎么跑起来的。

    ``MockRuntime`` 自身就满足 ``CaseExecutor``，因此
    ``GoldenRunner(build_mock_runtime())`` 仍是 S1a 那条路径，签名未变。
    """

    def __init__(self, executor: CaseExecutor) -> None:
        """初始化。

        Args:
            executor: 执行器。传 :func:`build_mock_runtime` 的产物就是
                内存路径。
        """
        self._executor = executor

    async def run_case(self, case: GoldenCase) -> CaseResult:
        """执行一条案例。

        断言按**声明顺序**求值（``required`` 在前，``forbidden`` 在后）——
        顺序稳定是结果可比的前提之一。

        Returns:
            案例结果。执行期异常**不向上抛**：它被记进 ``failure_reason`` /
            ``failure_detail``，其余案例继续跑，由调用方（CLI）返回非零退出码。
        """
        result, _ = await self._execute_case(case)
        return result

    async def _execute_case(self, case: GoldenCase) -> tuple[CaseResult, Mapping[str, str]]:
        """执行一条案例，并带回它**实际用到**的 Prompt 版本。

        ``run_dataset`` 需要后者来组装可复现性清单，而 ``run_case`` 的
        公开签名不该为了这件事变复杂——所以拆成两层。

        Returns:
            ``(案例结果, 实际用到的 Prompt 版本)``。执行失败时版本为空。
        """
        expectations = [("required", expectation) for expectation in case.expectations.required] + [
            ("forbidden", expectation) for expectation in case.expectations.forbidden
        ]

        try:
            execution = await self._executor.execute(case)
        except Exception as exc:
            # 🔴 **兜住一切，这是刻意的。**
            # 一条案例炸掉不该带走整个数据集——那样剩下的 9 条永远没机会说话。
            # 但兜住不等于吞掉：异常类型与原文都进结果（原文只进原始输出），
            # 且断言**不评估**（见 ``_unevaluated``）。
            reason = f"回合执行抛出异常：{type(exc).__name__}"
            return (
                CaseResult(
                    case_id=case.case_id,
                    category=case.category.value,
                    assertions=tuple(
                        _unevaluated(mode, expectation.assertion, expectation.expected, reason)
                        for mode, expectation in expectations
                    ),
                    passed=False,
                    failure_reason=reason,
                    failure_detail=f"{type(exc).__name__}: {exc}",
                ),
                {},
            )

        observation = execution.observation
        if observation is None:
            # 🔴 执行器报告"跑完了，但观测不到"。这条路径同样**不算通过**：
            # 一条没有观测的案例若被记成通过，报告会显示绿，而它什么都没验证。
            reason = "回合执行完成但没有产生可观测结果"
            return (
                CaseResult(
                    case_id=case.case_id,
                    category=case.category.value,
                    cognitive_round_id=execution.cognitive_round_id,
                    assertions=tuple(
                        _unevaluated(mode, expectation.assertion, expectation.expected, reason)
                        for mode, expectation in expectations
                    ),
                    passed=False,
                    failure_reason=reason,
                ),
                execution.prompt_versions,
            )

        assertions = tuple(
            evaluate(expectation.assertion, mode, expectation.expected, observation)
            for mode, expectation in expectations
        )
        failed = [result.name for result in assertions if not result.passed]
        return (
            CaseResult(
                case_id=case.case_id,
                category=case.category.value,
                cognitive_round_id=execution.cognitive_round_id,
                observation=observation,
                assertions=assertions,
                passed=not failed,
                failure_reason=None if not failed else f"未通过的断言：{'、'.join(failed)}",
            ),
            execution.prompt_versions,
        )

    async def run_dataset(
        self,
        cases: tuple[GoldenCase, ...],
        *,
        execution_mode: ExecutionMode = ExecutionMode.IN_MEMORY,
        storage_isolation: StorageIsolation | None = None,
        manifest_factory: Callable[[Mapping[str, str]], ReproducibilityManifest] | None = None,
    ) -> RunResult:
        """顺序执行整个数据集，产出汇总结果。

        🔴 **顺序执行**，没有并发：V0.1 没有 worker 与队列（任务书 §12.1），
        评测不该成为第一个引入它们的地方。

        Args:
            cases: 案例（已按 ``case_id`` 排序）。
            execution_mode: 本次运行走的是哪条执行路径。
            storage_isolation: 存储隔离证据；只有 PostgreSQL 路径才有。
            manifest_factory: 用**实际用到的 Prompt 版本**构造可复现性清单的回调。
                🔴 清单里还有存储与 Provider 身份，那些只有调用方（CLI）知道，
                所以这里收一个回调而不是自己去读配置——runner 不该知道
                "这次连的是哪个库"。``None`` 表示本次不采集清单。

        Returns:
            汇总结果。
        """
        prompt_versions: dict[str, str] = {}
        results: list[CaseResult] = []
        for case in cases:
            result, versions = await self._execute_case(case)
            # 取**并集**：不同深度的案例会走到不同的 Prompt 组件，
            # 清单要回答的是"这次运行一共用到了哪些"。
            prompt_versions.update(versions)
            results.append(result)

        passed = sum(1 for result in results if result.passed)
        return RunResult(
            case_type="cognitive_behavior",
            provider=self._executor.provider_name,
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            passed_overall=passed == len(results),
            cases=tuple(results),
            execution_mode=execution_mode.value,
            storage_isolation=storage_isolation,
            manifest=None if manifest_factory is None else manifest_factory(prompt_versions),
        )
