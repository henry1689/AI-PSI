"""认知运行时 —— 一次完整认知回合的执行者。

🔴 **本模块是认知流水线的唯一驱动者，也是本阶段唯一同时接触
"认知模块"与"持久化"的地方。**

这个位置不是随意选的。架构规则 3 规定"只有 ``application/`` 能发起持久化写入"，
而任务书 §4 的目录结构也正是把 ``cognitive_runtime.py`` 放在 ``application/``、
把 ``orchestrator.py`` 放在 ``cognition/``。
:mod:`ai_psi.cognition.orchestrator` 只回答"该跑哪些模块"（纯函数），
本模块回答"什么时候跑、跑完写什么、预算不够时怎么办"。

**一次回合的执行顺序**（状态机见 ``docs/state_machine.md``）::

    CREATED → TRIAGING → FRAMING → RETRIEVING → ANALYZING → DELIBERATING
            → METACOGNITIVE_REVIEW ⇄ DELIBERATING
            → SYNTHESIZING → RESPONDING → COMPLETED

🔴 **循环永不超预算。** 每一次模型调用前先向
:class:`~ai_psi.reliability.budgets.BudgetTracker` 扣额度；
可选模块在预算不足时**跳过并记录**，强制模块（判断合成、回答渲染）
始终有额度保留。超预算在这里是**不可能事件**，而不是"应当避免的事"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from pydantic import BaseModel

from ai_psi.application.artifact_service import ArtifactService, RoundScope
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.round_service import CognitiveRoundService, StartRoundResult
from ai_psi.cognition.base import ModuleOutcome
from ai_psi.cognition.causal_analyzer import CausalAnalyzer
from ai_psi.cognition.concept_analyzer import ConceptAnalyzer
from ai_psi.cognition.concern_detector import ConcernDetector
from ai_psi.cognition.context_builder import ContextBuilder, ContextBundle
from ai_psi.cognition.depth_router import DepthRoutingInput, route_depth
from ai_psi.cognition.dialectical_analyzer import DialecticalAnalyzer
from ai_psi.cognition.epistemic_analyzer import EpistemicAnalysis, EpistemicAnalyzer
from ai_psi.cognition.hypothesis_generator import HypothesisEvaluator, HypothesisGenerator
from ai_psi.cognition.inquiry_framer import InquiryFramer, to_domain_inquiry
from ai_psi.cognition.judgment_synthesizer import JudgmentSynthesizer
from ai_psi.cognition.logical_analyzer import LogicalAnalyzer
from ai_psi.cognition.metacognition import (
    Metacognition,
    MetacognitionRequest,
    MetacognitiveReview,
    StopReason,
    stop_condition_reached,
)
from ai_psi.cognition.orchestrator import (
    MODULE_MATRIX,
    CognitiveStep,
    StepKind,
    StepSpec,
    plan_for_depth,
)
from ai_psi.cognition.philosophical_analyzer import PhilosophicalAnalyzer
from ai_psi.cognition.response_planner import ResponsePlanner
from ai_psi.cognition.response_renderer import ResponseRenderer
from ai_psi.config import Settings
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.concerns import Concern
from ai_psi.domain.enums import (
    CognitiveDepth,
    ErrorType,
    EventType,
    OrdinalLevel,
    RoundState,
    SensitivityLevel,
    SourceType,
)
from ai_psi.domain.events import Event
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.exceptions import (
    AIPsiError,
    BudgetExhaustedError,
    ProviderError,
    StructuredOutputError,
)
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.memories import Memory
from ai_psi.domain.observations import Observation
from ai_psi.domain.reflections import Reflection
from ai_psi.learning.error_classifier import STAGE_ERROR_CATEGORY, ErrorSignals
from ai_psi.learning.experience_builder import ExperienceBuilder, RoundRecord
from ai_psi.prompts.registry import PromptRegistry
from ai_psi.prompts.schemas import ResponsePlan
from ai_psi.providers.base import LLMProvider
from ai_psi.providers.gateway import ModelGateway
from ai_psi.providers.registry import resolve_model
from ai_psi.reliability.budgets import BudgetTracker

__all__ = ["CognitiveRuntime", "RoundOutcome", "RoundRequest"]

#: 深度路由之前使用的**初始预算上限**。
#:
#: 回合在 CREATED 时还不知道最终深度，先给最深一档的上限；
#: ``FRAMING`` 阶段定级后再下调到该深度的真实预算。
#: 反过来（先给最小值、再往上加）会让"浅层问题花掉深层预算"
#: 这件事在账面上看起来是合法的。
_INITIAL_BUDGET_DEPTH: CognitiveDepth = CognitiveDepth.D4

#: 判断产出之后仍需保留的调用数：只剩回答渲染。
_RENDER_ONLY_RESERVE = 1


@dataclass(frozen=True, slots=True)
class RoundRequest:
    """一次认知回合的请求。"""

    user_message: str
    user_id: UUID | None = None
    conversation_id: UUID | None = None
    requested_depth: CognitiveDepth | None = None
    response_style: str = "structured"
    idempotency_key: str | None = None

    evidence: tuple[Evidence, ...] = ()
    conversation_summary: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    confirmed_user_goals: tuple[str, ...] = ()
    system_status: tuple[str, ...] = ()

    allow_long_term_memory: bool = True
    """是否允许本回合读写长期记忆。``False`` 时上下文不含记忆，
    也不产生经验记录。"""


@dataclass(frozen=True, slots=True)
class RoundOutcome:
    """一次认知回合的结果。"""

    cognitive_round_id: UUID
    state: RoundState
    depth: CognitiveDepth
    stop_reason: str | None
    response_text: str | None
    judgment: Judgment | None
    reflection: Reflection | None
    model_calls_used: int
    metacognitive_loops: int
    #: 触发本回合的 ``user.message.received`` 事件 id。
    #:
    #: 该事件发生在回合创建**之前**，因此不在回合的事件流里——
    #: 调用方需要它做关联时必须由本字段提供，而不是再去检索
    #: （按回合 id 检索是查不到的，那里根本没有这条事件）。
    trigger_event_id: UUID | None = None
    created: bool = True
    adjustments: tuple[str, ...] = ()
    skipped_steps: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """回合是否正常完成。"""
        return self.state is RoundState.COMPLETED


class CognitiveRuntime:
    """一次认知回合的执行者。"""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        provider: LLMProvider,
        prompts: PromptRegistry,
        memory_service: MemoryService,
        settings: Settings,
        round_service: CognitiveRoundService | None = None,
        artifacts: ArtifactService | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。
            provider: LLM Provider。
            prompts: Prompt 注册表。
            memory_service: 长期记忆服务（阶段 5 起含 PostgreSQL + pgvector 实现）。
            settings: 运行时配置。
            round_service: 回合服务；``None`` 时按工厂构造。
            artifacts: 产物记录服务；``None`` 时按工厂构造。
        """
        self._uow_factory = uow_factory
        self._provider = provider
        self._prompts = prompts
        self._memory_service = memory_service
        self._settings = settings
        # 🔴 模型标识**在这里解析一次**。
        #
        # 阶段 3 曾在 `_make_gateway` 里写成 `settings.llm_model or "mock-model-v1"`，
        # 结果只要没显式配 `llm_model`，网关就会把 "mock-model-v1" 发给
        # **真实供应商**——DeepSeek 直接返回 400。这个缺陷 Mock 永远测不出来，
        # 是阶段 4 的 live 测试第一次跑真实回合时抓到的。
        self._model = resolve_model(settings)
        self._rounds = round_service or CognitiveRoundService(uow_factory)
        self._artifacts = artifacts or ArtifactService(uow_factory)

    # ------------------------------------------------------------------
    # 依赖（供回合执行器使用）
    # ------------------------------------------------------------------

    @property
    def provider(self) -> LLMProvider:
        """LLM Provider。"""
        return self._provider

    @property
    def prompts(self) -> PromptRegistry:
        """Prompt 注册表。"""
        return self._prompts

    @property
    def uow_factory(self) -> UnitOfWorkFactory:
        """工作单元工厂。

        🔴 **给的是工厂而不是一个开着的工作单元。**
        事务边界必须由真正使用它的那段代码划定——把已开启的
        工作单元传出去，会让"这个读操作属于哪个事务"变成一个
        取决于调用顺序的问题。

        ⚠️ 暴露它是因为回合收尾需要**读回本回合自己的事件**
        （``origin_event_ids``，阶段 6.5 §二.9）。那是审计问题，
        只有事件存储能回答，而回合执行器手里的内存状态答不了。
        """
        return self._uow_factory

    @property
    def memory_service(self) -> MemoryService:
        """长期记忆服务。

        🔴 **这里给的是服务而不是仓储**：阶段 5 起记忆仓储属于工作单元
        （``uow.memories``），它的生命周期是一次事务。把仓储直接暴露出去，
        调用方就得自己管事务边界——而"自己管事务边界"正是
        记忆写入与事件写入分家的原因（ADR-0015 §5）。
        """
        return self._memory_service

    @property
    def settings(self) -> Settings:
        """运行时配置。"""
        return self._settings

    @property
    def model(self) -> str:
        """本次运行使用的模型标识（已按 Provider 解析过默认值）。"""
        return self._model

    @property
    def round_service(self) -> CognitiveRoundService:
        """回合服务（供 API 层查询状态时复用）。"""
        return self._rounds

    @property
    def artifact_service(self) -> ArtifactService:
        """产物记录服务。"""
        return self._artifacts

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def run_round(self, request: RoundRequest) -> RoundOutcome:
        """执行一次完整认知回合。

        🔴 **失败不会留下半成品**：所有写入都经工作单元，
        且回合失败时写入 ``cognitive_round.failed`` 事件并附上
        失败阶段与错误类别（不变量 20）。

        Args:
            request: 回合请求。

        Returns:
            回合结果。**异常路径也返回结果对象**（``state=FAILED``），
            而不是把异常抛给调用方——失败回合本身是**可查询的正常记录**，
            把它抛成异常反而会让调用方只看到"出错了"而丢掉诊断信息。
        """
        correlation_id = uuid4()
        user_event = await self._record_user_message(request, correlation_id)

        started = await self._rounds.start_round(
            created_by="cognitive_runtime",
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            trigger_event_id=user_event.id,
            depth_level=CognitiveDepth.D0,
            budget=CognitiveBudget.for_depth(_INITIAL_BUDGET_DEPTH),
            correlation_id=correlation_id,
            idempotency_key=request.idempotency_key,
            request_hash=_request_hash(request),
        )

        if not started.created:
            return await self._replay_outcome(started)

        execution = _RoundExecution(
            runtime=self,
            request=request,
            round_=started.round,
            scope=RoundScope(
                cognitive_round_id=started.round.id,
                correlation_id=correlation_id,
                user_id=request.user_id,
                conversation_id=request.conversation_id,
            ),
            user_event=user_event,
        )
        return await execution.execute()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _record_user_message(self, request: RoundRequest, correlation_id: UUID) -> Event:
        """记录 ``user.message.received`` 事件。

        这是每个回合的一号事件。关切与观察都以它为来源，
        因此"这件事为什么被处理"永远可以顺着 ``source_event_ids`` 追回来。
        """
        return await self._artifacts.record(
            scope=RoundScope(
                cognitive_round_id=None,
                correlation_id=correlation_id,
                user_id=request.user_id,
                conversation_id=request.conversation_id,
            ),
            event_type=EventType.USER_MESSAGE_RECEIVED,
            actor_id=str(request.user_id) if request.user_id else "anonymous",
            payload={
                "content": request.user_message,
                "requested_depth": (
                    request.requested_depth.value if request.requested_depth else None
                ),
            },
            sensitivity=SensitivityLevel.PERSONAL,
        )

    async def _replay_outcome(self, started: StartRoundResult) -> RoundOutcome:
        """幂等重放：从既有回合的事件流重建结果。"""
        round_ = started.round
        async with self._uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=round_.id)
        response_text = next(
            (
                str(event.payload.get("text"))
                for event in reversed(events)
                if event.event_type is EventType.RESPONSE_GENERATED
            ),
            None,
        )
        return RoundOutcome(
            cognitive_round_id=round_.id,
            state=round_.state,
            depth=round_.depth_level,
            stop_reason=round_.stop_reason,
            response_text=response_text,
            judgment=None,
            reflection=None,
            model_calls_used=round_.model_calls_used,
            metacognitive_loops=round_.metacognitive_loops,
            trigger_event_id=round_.trigger_event_id,
            created=False,
            adjustments=("幂等重放：返回既有回合，未重新执行认知流程",),
        )


def _request_hash(request: RoundRequest) -> str:
    """请求体哈希，用于识别"误用同一个幂等键"。"""
    payload = f"{request.user_id}|{request.conversation_id}|{request.user_message}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 单次回合的执行
# ---------------------------------------------------------------------------


@dataclass
class _ExecutionState:
    """回合执行过程中的可变状态。

    单独抽出来是因为它跨越所有阶段——把它们塞进方法参数会让
    每个阶段函数的签名长到无法阅读。
    """

    round_: CognitiveRound
    depth: CognitiveDepth
    concerns: list[Concern] = field(default_factory=list)
    inquiry: Inquiry | None = None
    observations: list[Observation] = field(default_factory=list)
    bundle: ContextBundle | None = None
    epistemic: EpistemicAnalysis | None = None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    analysis_payloads: dict[str, BaseModel] = field(default_factory=dict)
    judgment: Judgment | None = None
    reflection: Reflection | None = None
    plan: ResponsePlan | None = None
    response_text: str | None = None
    previous_judgment_conclusion: str | None = None
    analysis_signature: frozenset[str] = frozenset()
    previous_analysis_signature: frozenset[str] = frozenset()
    evidence_signature: frozenset[str] = frozenset()
    previous_evidence_signature: frozenset[str] = frozenset()
    adjustments: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class _RoundExecution:
    """一次回合的完整执行。"""

    def __init__(
        self,
        *,
        runtime: CognitiveRuntime,
        request: RoundRequest,
        round_: CognitiveRound,
        scope: RoundScope,
        user_event: Event,
    ) -> None:
        self._runtime = runtime
        self._request = request
        self._scope = scope
        self._round_id = scope.require_round_id()
        self._user_event = user_event
        self._state = _ExecutionState(round_=round_, depth=CognitiveDepth.D0)
        self._budget = BudgetTracker(
            CognitiveBudget.for_depth(_INITIAL_BUDGET_DEPTH),
            tail_reserve=_initial_tail_reserve(_INITIAL_BUDGET_DEPTH),
        )
        self._gateway = self._make_gateway()
        # 🔴 学习链路在这里接上。在此之前 `ExperienceBuilder` 在生产里
        # **没有任何调用者**，回合结束写的是硬编码负载——于是每一条经验
        # 都不可归因，模式发现永远沉默（见 `_close_round` 的文档）。
        self._experience_builder = ExperienceBuilder()

    # ------------------------------------------------------------------
    # 总入口
    # ------------------------------------------------------------------

    async def execute(self) -> RoundOutcome:
        """执行回合；异常路径转换为 FAILED 结果。"""
        stage = "triage"
        try:
            await self._triage()
            if not self._state.concerns:
                await self._finish_without_concern()
                return self._outcome()

            stage = "frame"
            await self._frame()
            stage = "retrieve"
            await self._retrieve()
            stage = "analyze"
            await self._analyze()
            stage = "deliberate"
            await self._deliberate()
            stage = "review"
            if not await self._review_loop():
                # 回合停在等待/挂起状态，不该再往下合成
                return self._outcome()
            stage = "synthesize"
            await self._synthesize()
            stage = "respond"
            await self._respond()
        except BudgetExhaustedError as exc:
            return await self._fail(
                stage=stage,
                category=ErrorType.PROCESS_ERROR,
                reason=f"认知预算不足：{exc.message}",
            )
        except StructuredOutputError as exc:
            return await self._fail(
                stage=stage,
                category=_category_for(stage),
                reason=f"模型输出无法解析：{exc.message}",
            )
        except ProviderError as exc:
            return await self._fail(
                stage=stage,
                category=ErrorType.PROCESS_ERROR,
                reason=f"模型调用失败：{exc.message}",
            )
        except AIPsiError as exc:
            return await self._fail(stage=stage, category=_category_for(stage), reason=exc.message)
        return self._outcome()

    # ------------------------------------------------------------------
    # TRIAGING
    # ------------------------------------------------------------------

    async def _triage(self) -> None:
        """观察与关切检测。"""
        await self._advance(RoundState.TRIAGING, "开始关切检测")

        observation = Observation(
            created_by="cognitive_runtime",
            user_id=self._request.user_id,
            source_type=SourceType.USER_MESSAGE,
            source_id=str(self._user_event.id),
            content=self._request.user_message,
        )
        self._state.observations.append(observation)
        await self._record(
            event_type=EventType.OBSERVATION_CREATED,
            actor_id="cognitive_runtime",
            causation_id=self._user_event.id,
            sensitivity=SensitivityLevel.PERSONAL,
            payload={"observation": observation.model_dump(mode="json")},
        )

        detector = ConcernDetector(self._gateway)
        outcome = await detector.detect(
            trigger_event=self._user_event,
            user_message=self._request.user_message,
            conversation_summary=self._request.conversation_summary,
            open_questions=self._request.open_questions,
            confirmed_user_goals=self._request.confirmed_user_goals,
            system_status=self._request.system_status,
            user_id=self._request.user_id,
            conversation_id=self._request.conversation_id,
            cognitive_round_id=self._round_id,
            correlation_id=self._scope.correlation_id,
        )
        self._state.concerns = list(outcome.value)
        for concern in self._state.concerns:
            await self._record(
                event_type=EventType.CONCERN_CREATED,
                actor_id="concern_detector",
                model_info=outcome.invocation,
                payload={"concern": concern.model_dump(mode="json")},
            )

    async def _finish_without_concern(self) -> None:
        """没有值得处理的关切时的收尾路径。

        🔴 **这是合法结论，不是失败。** 任务书 §9.1 明确允许关切检测
        输出零个关切——"这件事不值得启动认知"正是它要表达的意思。
        此时不消耗任何后续模型调用。
        """
        await self._advance(RoundState.SYNTHESIZING, "没有值得启动认知的关切")
        await self._advance(RoundState.RESPONDING, "无需渲染回答")
        await self._advance(
            RoundState.COMPLETED,
            "回合完成：没有值得处理的关切",
            stop_reason=StopReason.NO_CONCERN_DETECTED.value,
        )

    # ------------------------------------------------------------------
    # FRAMING
    # ------------------------------------------------------------------

    async def _frame(self) -> None:
        """问题框定与深度路由。"""
        await self._advance(RoundState.FRAMING, "开始问题框定")

        framer = InquiryFramer(self._gateway)
        outcome = await framer.frame(
            concern=self._state.concerns[0],
            user_message=self._request.user_message,
            known_observations=[item.content for item in self._state.observations],
            user_id=self._request.user_id,
            conversation_id=self._request.conversation_id,
            cognitive_round_id=self._round_id,
            correlation_id=self._scope.correlation_id,
        )
        framed = outcome.value

        routing = route_depth(
            DepthRoutingInput.from_signals(
                inquiry=framed.draft.question,
                signals=framed.signals,
                user_requested_depth=self._request.requested_depth,
                available_budget=self._budget.budget,
            )
        )
        self._state.depth = routing.depth
        self._rebudget(routing.depth)

        inquiry = to_domain_inquiry(
            framed,
            concern=self._state.concerns[0],
            depth=routing.depth,
        )
        self._state.inquiry = inquiry

        # 深度路由的结果与依据一并留痕——"为什么这次是 D3"必须可回答
        await self._advance(
            RoundState.RETRIEVING,
            f"深度定级为 {routing.depth.value}：{routing.reason}",
        )
        await self._record(
            event_type=EventType.INQUIRY_CREATED,
            actor_id="inquiry_framer",
            model_info=outcome.invocation,
            payload={
                "inquiry": inquiry.model_dump(mode="json"),
                "depth_routing": {
                    "depth": routing.depth.value,
                    "reason": routing.reason,
                    "requested_depth": (
                        routing.requested_depth.value if routing.requested_depth else None
                    ),
                    "degraded_from": (
                        routing.degraded_from.value if routing.degraded_from else None
                    ),
                },
            },
        )

    # ------------------------------------------------------------------
    # RETRIEVING
    # ------------------------------------------------------------------

    async def _retrieve(self) -> None:
        """上下文选择与组装。"""
        inquiry = self._require_inquiry()
        memories: tuple[Memory, ...] = ()
        if self._request.allow_long_term_memory:
            memories = tuple(
                await self._runtime.memory_service.retrieve(
                    user_id=self._request.user_id,
                    query=inquiry.question,
                    limit=self._budget.budget.max_retrieved_memories,
                )
            )

        builder = ContextBuilder(
            max_items=self._budget.budget.max_retrieved_memories,
            max_tokens=self._budget.budget.max_context_tokens,
        )
        self._state.bundle = builder.build(
            question=inquiry.question,
            observations=tuple(self._state.observations),
            memories=memories,
            evidence=self._request.evidence,
        )

        for item in self._request.evidence:
            await self._record(
                event_type=EventType.EVIDENCE_ATTACHED,
                actor_id="context_builder",
                payload={"evidence": item.model_dump(mode="json")},
                evidence_refs=(item.id,),
            )

        await self._advance(RoundState.ANALYZING, "上下文已就绪，开始分析")

    # ------------------------------------------------------------------
    # ANALYZING
    # ------------------------------------------------------------------

    async def _analyze(self) -> None:
        """认知分析模块。

        🔴 **保留额度必须在进入时就抬高，而不是在结束时。**

        阶段 4 跑真实回合时发现：元认知裁定 ``CHANGE_METHOD`` 会**再次**进入
        本方法，而此时上一次判断已经把保留额度降到了"只剩渲染"（1）。
        于是一轮可选分析刚好把额度花到 0，接下来的判断合成与回答渲染
        就都没有额度了——回合以 ``BudgetExhaustedError`` 失败，
        用户拿不到任何回答。

        正确做法是：只要还在分析阶段，就按"判断 + 元认知 + 渲染"三重保留。
        宁可少跑一个可选分析（跳过会被记录），也不能拿不出结论。
        """
        self._budget.set_tail_reserve(_initial_tail_reserve(self._state.depth))

        inquiry = self._require_inquiry()
        bundle = self._require_bundle()

        for spec in plan_for_depth(self._state.depth):
            if spec.state is not RoundState.ANALYZING:
                continue
            if spec.step is CognitiveStep.EPISTEMIC_ANALYSIS:
                self._state.epistemic = EpistemicAnalyzer().analyze(
                    bundle=bundle,
                    key_unknowns=inquiry.key_unknowns,
                    out_of_capability=inquiry.out_of_scope,
                )
                continue
            await self._run_analysis_step(spec, inquiry)

        self._state.evidence_signature = frozenset(str(item.id) for item in self._state.hypotheses)
        # 用**内容哈希**而不是对象身份：重跑一次的相同输出不该被算作
        # "新的推理路径"，否则反刍检测会被自己的重复输出骗过去。
        self._state.analysis_signature = frozenset(
            f"{key}:{hashlib.sha256(value.model_dump_json().encode('utf-8')).hexdigest()[:16]}"
            for key, value in self._state.analysis_payloads.items()
        )
        # 分析阶段结束，保留额度不变（判断合成是下一步，仍需为元认知与渲染留出）。

    async def _run_analysis_step(self, spec: StepSpec, inquiry: Inquiry) -> None:
        """执行一个 ANALYZING 阶段的**可选**模块。

        🔴 **可选模块失败不拖垮整个回合。**

        它们本来就是"预算不够就跳过"的步骤（见 MODULE_MATRIX），
        因此当模型调用失败——输出被截断、格式修不好、供应商抖动——
        正确的处理与预算不足一样：**跳过并记录**。

        任务书 §13.2 明确写了"真实 LLM 不可用时……可返回当前认知服务降级"，
        阶段 4 的验收条件也写着"解析异常不会污染状态"。
        丢掉整个回合（用户什么都拿不到）才是更糟的选择。

        ⚠️ **降级必须留痕。** 被跳过的步骤进入 ``skipped_steps``、
        并随终态事件落库；否则事后无法分辨"少做了一个分析"
        与"分析跑了但没产出"。

        ⚠️ 强制模块（判断合成、回答渲染）**不在此列**——
        它们失败就是回合失败，因为跳过它们的"降级"等于没有回答。
        """
        if not self._can_run_optional():
            self._state.skipped.append(f"{spec.step.value}（预算不足）")
            return

        try:
            await self._run_optional_module(spec, inquiry)
        except ProviderError as exc:
            # ProviderError 覆盖了"模型侧出了任何问题"：
            # 结构化输出失败、超时、限流、供应商不可用（含熔断）。
            self._state.skipped.append(f"{spec.step.value}（模型调用失败：{exc.code}）")
            self._state.adjustments.append(
                f"分析步骤 {spec.step.value} 因模型调用失败被跳过（{exc.code}）"
            )

    async def _run_optional_module(self, spec: StepSpec, inquiry: Inquiry) -> None:
        """执行一次可选分析模块（失败由调用方降级处理）。"""
        bundle = self._require_bundle()

        if spec.step is CognitiveStep.HYPOTHESIS_GENERATION:
            outcome = await HypothesisGenerator(self._gateway).generate(
                inquiry=inquiry,
                evidence_summaries=bundle.summarize_for_prompt(),
                max_hypotheses=self._budget.budget.max_hypotheses,
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.hypotheses = list(outcome.value)
            for hypothesis in outcome.value:
                await self._record(
                    event_type=EventType.HYPOTHESIS_CREATED,
                    actor_id="hypothesis_generator",
                    model_info=outcome.invocation,
                    payload={
                        "hypothesis": hypothesis.model_dump(mode="json"),
                        "notes": list(outcome.notes),
                    },
                )

        elif spec.step is CognitiveStep.LOGICAL_ANALYSIS:
            logical = await LogicalAnalyzer(self._gateway).analyze(
                question=inquiry.question,
                hypotheses=self._state.hypotheses,
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.analysis_payloads["logical"] = logical.value
            await self._record_analysis("logical", logical)

        elif spec.step is CognitiveStep.CAUSAL_ANALYSIS:
            causal = await CausalAnalyzer(self._gateway).analyze(
                question=inquiry.question,
                causal_claims=[item.statement for item in self._state.hypotheses],
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.analysis_payloads["causal"] = causal.value
            await self._record_analysis("causal", causal)

        elif spec.step is CognitiveStep.CONCEPT_ANALYSIS:
            concept = await ConceptAnalyzer(self._gateway).analyze(
                question=inquiry.question,
                concepts=inquiry.ambiguous_concepts,
                context=inquiry.scope,
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.analysis_payloads["concept"] = concept.value
            await self._record_analysis("concept", concept)
            for item in concept.value.concepts:
                await self._record(
                    event_type=EventType.CONCEPT_IDENTIFIED,
                    actor_id="concept_analyzer",
                    payload={"concept": item.model_dump(mode="json")},
                )

        elif spec.step is CognitiveStep.DIALECTICAL_ANALYSIS:
            dialectical = await DialecticalAnalyzer(self._gateway).analyze(
                question=inquiry.question,
                value_conflicts=inquiry.ambiguous_concepts,
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.analysis_payloads["dialectical"] = dialectical.value
            await self._record_analysis("dialectical", dialectical)

        elif spec.step is CognitiveStep.PHILOSOPHICAL_ANALYSIS:
            philosophical = await PhilosophicalAnalyzer(self._gateway).analyze(
                question=inquiry.question,
                factual_unknowns=inquiry.key_unknowns,
                user_id=self._request.user_id,
                conversation_id=self._request.conversation_id,
                cognitive_round_id=self._round_id,
                correlation_id=self._scope.correlation_id,
            )
            self._state.analysis_payloads["philosophical"] = philosophical.value
            await self._record_analysis("philosophical", philosophical)

    # ------------------------------------------------------------------
    # DELIBERATING
    # ------------------------------------------------------------------

    async def _deliberate(self) -> None:
        """假设评估与判断合成。"""
        await self._ensure_state(RoundState.DELIBERATING, "开始综合判断")

        inquiry = self._require_inquiry()
        bundle = self._require_bundle()

        if self._state.hypotheses:
            evaluation = HypothesisEvaluator().evaluate(
                hypotheses=self._state.hypotheses,
                evidence=self._request.evidence,
            )
            self._state.hypotheses = list(evaluation.hypotheses)
            await self._record(
                event_type=EventType.HYPOTHESIS_EVALUATED,
                actor_id="hypothesis_evaluator",
                payload={
                    "evaluations": [
                        {"hypothesis_id": str(item.id), "status": item.status.value}
                        for item in evaluation.hypotheses
                    ],
                    "supported": evaluation.supported_count,
                    "rejected": evaluation.rejected_count,
                    "unresolved": evaluation.unresolved_count,
                },
            )

        payloads = self._state.analysis_payloads
        outcome = await JudgmentSynthesizer(self._gateway).synthesize(
            inquiry=inquiry,
            bundle=bundle,
            hypotheses=self._state.hypotheses,
            logical=payloads.get("logical"),  # type: ignore[arg-type]
            causal=payloads.get("causal"),  # type: ignore[arg-type]
            dialectical=payloads.get("dialectical"),  # type: ignore[arg-type]
            philosophical=payloads.get("philosophical"),  # type: ignore[arg-type]
            user_id=self._request.user_id,
            conversation_id=self._request.conversation_id,
            cognitive_round_id=self._round_id,
            correlation_id=self._scope.correlation_id,
        )
        synthesized = outcome.value
        if self._state.judgment is not None:
            self._state.previous_judgment_conclusion = self._state.judgment.conclusion
        self._state.judgment = synthesized.judgment
        self._state.adjustments.extend(synthesized.adjustments)
        # 判断已产出：尾部只剩回答渲染
        self._budget.set_tail_reserve(_RENDER_ONLY_RESERVE)

        await self._record(
            event_type=EventType.JUDGMENT_CREATED,
            actor_id="judgment_synthesizer",
            model_info=outcome.invocation,
            payload={
                "judgment": synthesized.judgment.model_dump(mode="json"),
                "confidence_ceiling": synthesized.confidence_ceiling.value,
                "adjustments": list(synthesized.adjustments),
                # 分析结果没有独立的事件类型（任务书 §5.2 的 32 种里没有），
                # 因此随判断一起落库——它们是判断的直接输入，
                # 分开存放反而会让"这个判断基于什么"需要跨源拼装。
                "analyses": {key: value.model_dump(mode="json") for key, value in payloads.items()},
            },
        )

    # ------------------------------------------------------------------
    # METACOGNITIVE_REVIEW
    # ------------------------------------------------------------------

    async def _review_loop(self) -> bool:
        """元认知复核与循环。

        🔴 **循环永不超预算**：``start_metacognitive_loop()`` 与
        :meth:`BudgetTracker.can_afford` 共同保证这一点——
        循环轮数用完或预算不足时，规则层强制 STOP 进入合成（ADR-0008）。

        🔴 **即使不跑模型层，也必须走这一步。** 状态机不允许
        ``DELIBERATING → SYNTHESIZING`` 直达——"合成之前必须经过元认知复核"
        是状态机的明确意图。D0 因此进入本状态但**只跑规则层**
        （零模型调用），照样留下一条"为什么停下来"的决策记录（不变量 19）。

        Returns:
            ``True`` 表示回合应当继续进入合成；``False`` 表示回合已经
            停在等待或挂起状态，不应再往下走。
        """
        loop_index = 0
        while True:
            use_model = _has_metacognition(self._state.depth)
            if use_model and not self._can_run_optional():
                # 预算不足以再问一次模型，但**规则层永远可用**——
                # 它不花钱，并且照样能给出"为什么停下来"的记录。
                # 直接跳到 SYNTHESIZING 是不行的：状态机不允许
                # DELIBERATING → SYNTHESIZING 直达（见本方法的文档）。
                self._state.skipped.append("metacognition（预算不足，仅执行规则层复核）")
                await self._ensure_state(
                    RoundState.METACOGNITIVE_REVIEW,
                    "预算不足：只执行规则层元认知复核",
                )
                review = await self._run_review(
                    loop_index,
                    use_model=False,
                    rule_only_stop_reason=StopReason.BUDGET_CONSTRAINT.value,
                )
                self._state.reflection = review.reflection
                await self._advance(
                    review.next_state,
                    f"预算不足以执行模型层复核（{review.stop_reason}）",
                    stop_reason=review.stop_reason,
                )
                return True

            if use_model:
                self._budget.start_metacognitive_loop()
            await self._ensure_state(
                RoundState.METACOGNITIVE_REVIEW,
                (
                    f"第 {loop_index + 1} 次元认知复核"
                    if use_model
                    else f"规则层元认知复核（{self._state.depth.value} 不启用模型层）"
                ),
            )
            review = await self._run_review(
                loop_index,
                use_model=use_model,
                rule_only_stop_reason=StopReason.DIRECT_ANSWER.value,
            )
            self._state.reflection = review.reflection

            if review.next_state is RoundState.SYNTHESIZING:
                await self._advance(
                    review.next_state,
                    f"元认知决策：{review.decision.value}（{review.stop_reason}）",
                    stop_reason=review.stop_reason,
                )
                return True

            if review.next_state in {
                RoundState.DELIBERATING,
                RoundState.ANALYZING,
                RoundState.RETRIEVING,
            }:
                loop_index += 1
                # 更高成本的路径（重跑分析/重跑检索）是 CHANGE_METHOD 与
                # REQUEST_EVIDENCE 的语义，**不是默认路径**：
                # 预算不够时它们会被逐级跳过，最终仍由判断合成收尾。
                if review.next_state is RoundState.RETRIEVING:
                    await self._advance(RoundState.RETRIEVING, "元认知裁定重新检索")
                    await self._retrieve()
                if review.next_state in {RoundState.RETRIEVING, RoundState.ANALYZING}:
                    await self._ensure_state(RoundState.ANALYZING, "元认知裁定重新分析")
                    await self._analyze()
                await self._ensure_state(
                    RoundState.DELIBERATING,
                    f"元认知裁定继续分析（进入第 {loop_index + 1} 轮）",
                )
                await self._deliberate()
                continue

            # WAITING_FOR_EVIDENCE / SUSPENDED / FAILED：回合不进入合成
            await self._advance(
                review.next_state,
                f"元认知决策：{review.decision.value}（{review.stop_reason}）",
                stop_reason=review.stop_reason,
            )
            return False

    async def _run_review(
        self,
        loop_index: int,
        *,
        use_model: bool = True,
        rule_only_stop_reason: str | None = None,
    ) -> MetacognitiveReview:
        """执行一次元认知复核并记录事件。

        🔴 **"是否有新东西"是相对上一次复核而言的**，不是相对回合开始。
        因此这里在算完本轮指标之后立刻把当前签名记为"上一轮"——
        否则第二次复核会一直拿"回合开始时为空"作对照，
        于是每一轮都判定为"有新证据"，反刍检测永远不触发。
        """
        new_evidence = self._state.evidence_signature != self._state.previous_evidence_signature
        new_reasoning_path = (
            self._state.analysis_signature != self._state.previous_analysis_signature
        )
        self._state.previous_evidence_signature = self._state.evidence_signature
        self._state.previous_analysis_signature = self._state.analysis_signature

        outcome = await Metacognition(
            self._gateway,
            repeat_threshold=self._runtime.settings.repetition_threshold,
        ).review(
            MetacognitionRequest(
                cognitive_round_id=self._round_id,
                inquiry=self._require_inquiry(),
                judgment=self._require_judgment(),
                loop_index=loop_index,
                stop_condition_reached=stop_condition_reached(self._require_judgment()),
                new_evidence_present=new_evidence,
                new_reasoning_path_present=new_reasoning_path,
                previous_judgment_conclusion=self._state.previous_judgment_conclusion,
                user_message_summary=self._request.user_message,
            ),
            budget=self._budget,
            use_model=use_model,
            rule_only_stop_reason=rule_only_stop_reason,
            user_id=self._request.user_id,
            conversation_id=self._request.conversation_id,
            correlation_id=self._scope.correlation_id,
        )
        review = outcome.value
        await self._record(
            event_type=EventType.METACOGNITION_COMPLETED,
            actor_id="metacognition",
            model_info=outcome.invocation,
            payload={
                "reflection": review.reflection.model_dump(mode="json"),
                "decision": review.decision.value,
                "stop_reason": review.stop_reason,
                "rule_overrode_model": review.forced,
                "model_layer_used": use_model,
            },
        )
        return review

    # ------------------------------------------------------------------
    # SYNTHESIZING / RESPONDING
    # ------------------------------------------------------------------

    async def _synthesize(self) -> None:
        """回答规划（确定性，不消耗模型调用）。"""
        await self._ensure_state(RoundState.SYNTHESIZING, "开始规划回答")

        self._state.plan = ResponsePlanner().plan(
            judgment=self._require_judgment(),
            inquiry=self._require_inquiry(),
            hypotheses=self._state.hypotheses,
            depth=self._state.depth,
            response_style=self._request.response_style,
            reflection=self._state.reflection,
            conflicts=self._require_bundle().conflicting,
        )
        await self._advance(RoundState.RESPONDING, "开始渲染回答")

    async def _respond(self) -> None:
        """渲染回答并完成回合。"""
        await self._ensure_state(RoundState.RESPONDING, "开始渲染回答")

        outcome = await ResponseRenderer(self._gateway).render(
            plan=self._require_plan(),
            judgment=self._require_judgment(),
            user_id=self._request.user_id,
            conversation_id=self._request.conversation_id,
            cognitive_round_id=self._round_id,
            correlation_id=self._scope.correlation_id,
        )
        self._state.response_text = outcome.value
        await self._record(
            event_type=EventType.RESPONSE_GENERATED,
            actor_id="response_renderer",
            model_info=outcome.invocation,
            payload={
                "text": outcome.value,
                "plan": self._require_plan().model_dump(mode="json"),
                "judgment_id": str(self._require_judgment().id),
            },
        )

        stop_reason = (
            self._state.round_.stop_reason or StopReason.INQUIRY_STOP_CONDITION_SATISFIED.value
        )
        await self._advance(
            RoundState.COMPLETED,
            f"回答已生成（{stop_reason}）",
            stop_reason=stop_reason,
            diagnostics=self._diagnostics(),
        )
        await self._close_round()

    async def _close_round(self) -> None:
        """回合正常结束后的收尾：构建 ``Experience`` 记录。

        🔴 **阶段 6 起走真正的学习链路。**

        阶段 3 的实现手写事件负载，把 ``error_type`` 与
        ``attribution_confidence`` **硬编码**成 ``None`` / ``"very_low"``。
        那意味着生产里产出的每一条经验都不可归因，而
        :class:`~ai_psi.learning.pattern_detector.PatternDetector` 会把
        不可归因的经验全部过滤掉——于是"三次同类错误生成提案"这条
        验收条件在**跑起来的系统里永远不可能发生**。
        现在归因由 :class:`~ai_psi.learning.error_classifier.ErrorClassifier`
        按确定性判据做，理由随事件一起留档。

        ⚠️ **``applicable_conditions`` 与 ``counterexamples`` 刻意留空。**

        它们看着像是该从 ``Judgment.applicability`` 与
        ``strongest_counterarguments`` 填进来的——但后者是**模型输出**，
        填进去就等于让模型的措辞进入学习链路，并随事件永久留档
        （ADR-0018 §1 的边界）。本方法只填**结构信号**：
        情境签名、模块名、证据 id。
        """
        if not self._request.allow_long_term_memory:
            return
        judgment = self._state.judgment
        if judgment is None:
            # 没有判断就没有"当时判断得对不对"这回事，经验无从谈起。
            return

        record = RoundRecord(
            cognitive_round_id=self._round_id,
            judgment_id=judgment.id,
            situation_signature=_situation_signature(
                depth=self._state.depth,
                evidence_count=len(self._request.evidence),
                hypothesis_count=len(self._state.hypotheses),
            ),
            inquiry_type=(
                self._state.inquiry.expected_output_type.value
                if self._state.inquiry is not None
                else "unknown"
            ),
            # 🔴 **判断发生时**就已掌握的证据。它区分"当时判断错了"与
            # "当时信息本就不足"——少了它，系统会把所有后来被推翻的
            # 判断都记成错误，从而学到"这类问题要更保守"。
            evidence_ids=tuple(item.id for item in self._request.evidence),
            # 模块名（结构标签），不是模型的措辞
            strategy_used=tuple(sorted(self._state.analysis_signature)),
            # 🔴 本回合**走到这里为止**写入的全部事件。
            # 它是经验可被回放重建的锚点：没有它，"这条经验读的是哪几个
            # 事件"只能靠时间与 payload 形状去猜（阶段 6.5 §二.9）。
            origin_event_ids=await self._round_event_ids(),
            # 幂等键决定独立性分组：同一 key 的技术重试只算一次发生
            # （阶段 6.5 §二.12）。它来自回合本身，不是请求——
            # 重放一个已有回合时，请求是新的，发生还是同一次。
            idempotency_key=self._state.round_.idempotency_key,
        )
        experience, attribution = self._experience_builder.build(
            record=record, signals=self._error_signals()
        )

        payload = experience.model_dump(mode="json")
        payload["stop_reason"] = self._state.round_.stop_reason
        # 归因理由一并留档：只把 error_type 存下来，理由就丢了，
        # 而**不可解释的归因日后无法被推翻**。
        payload["attribution_reasons"] = list(attribution.reasons)

        await self._record(
            event_type=EventType.EXPERIENCE_CREATED,
            actor_id="cognitive_runtime",
            payload={"experience": payload},
        )

    async def _round_event_ids(self) -> tuple[UUID, ...]:
        """本回合到目前为止写入的事件 id（按写入序）。

        🔴 **它必须从事件流里读，不能从内存里的状态拼。**

        内存里能拿到的是"这一轮跑过哪些模块"，而 ``origin_event_ids``
        要回答的是"这条经验对应事件流里的哪几行"——那是审计问题，
        只有事件存储能回答。从内存拼出来的 id 集合在降级、跳过、
        元认知循环等路径上会与真实写入不重合，而那些恰好是
        最需要复盘的情形。

        ⚠️ 这是一次**只读**的事务，不参与经验写入的事务——
        读失败时应当让回合收尾失败，而不是拿一个残缺的锚点继续。
        """
        async with self._runtime.uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=self._round_id)
        return tuple(event.id for event in events)

    def _error_signals(self) -> ErrorSignals:
        """把本回合的可观察状态翻译成归因判据。

        🔴 **只翻译结构信号，不翻译任何文本。**

        ⚠️ ``budget_exhausted`` 恒为 ``False``，这是有意的：
        ``StopReason.BUDGET_CONSTRAINT`` 表示"元认知判定预算不足以
        再跑一轮"——**那是正常收尾，不是流程没跑完**
        （:class:`~ai_psi.cognition.metacognition.StopReason` 的文档原话）。
        真正的预算击穿走的是 ``_fail`` 那条路径，根本不经过本方法。
        把它当作"预算耗尽"上报，会让每一个受预算约束的正常回合
        都被归成过程错误，进而淹没整个模式发现。
        """
        reflection = self._state.reflection
        judgment = self._state.judgment
        if reflection is None:
            return ErrorSignals(
                round_state=self._state.round_.state,
                uncertainty_type=judgment.uncertainty_type if judgment else None,
                epistemic_action=judgment.recommended_epistemic_action if judgment else None,
            )

        return ErrorSignals(
            round_state=self._state.round_.state,
            scope_drift_detected=reflection.scope_drift_detected,
            unsupported_certainty_detected=reflection.unsupported_certainty_detected,
            missing_counterexample_detected=reflection.missing_counterexample_detected,
            high_confirmation_bias=reflection.confirmation_bias_risk.at_least(OrdinalLevel.HIGH),
            high_user_pleasing_bias=reflection.user_pleasing_bias_risk.at_least(OrdinalLevel.HIGH),
            uncertainty_type=judgment.uncertainty_type if judgment else None,
            epistemic_action=judgment.recommended_epistemic_action if judgment else None,
            # 反馈在回合结束后才到达，此刻还没有——它经由
            # `FeedbackService` 单独进入学习链路。
        )

    # ------------------------------------------------------------------
    # 失败路径
    # ------------------------------------------------------------------

    async def _fail(self, *, stage: str, category: ErrorType, reason: str) -> RoundOutcome:
        """把回合标记为失败。

        🔴 **失败回合必须可查询失败阶段与错误类别**（不变量 20）。
        同时保留已经写入的全部事件——失败不得销毁审计证据（任务书 §6.3）。
        """
        try:
            result = await self._runtime.round_service.fail_round(
                self._round_id,
                stage=stage,
                error_category=category,
                reason=reason,
            )
        except AIPsiError:
            # 状态机不允许从当前状态转移到 FAILED（例如已在终态）。
            # 此时回合已有明确结局，不再二次改写。
            return self._outcome()
        self._state.round_ = result.round
        return self._outcome()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    async def _advance(
        self,
        to_state: RoundState,
        reason: str,
        *,
        stop_reason: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        """推进状态机，并把最新的预算计数一并落库。

        🔴 **每一次转移都携带 ``model_calls_used``**。
        只在回合结束时记录一次的话，"循环中某一刻已经超支"
        就永远无法从数据里被发现。
        """
        result = await self._runtime.round_service.transition(
            self._round_id,
            to_state,
            reason=reason,
            actor_id="cognitive_runtime",
            stop_reason=stop_reason,
            model_calls_used=self._budget.model_calls_used,
            metacognitive_loops=self._budget.metacognitive_loops_used,
            budget=self._budget.budget,
            depth_level=self._state.depth,
            diagnostics=diagnostics,
        )
        self._state.round_ = result.round

    async def _ensure_state(self, target: RoundState, reason: str) -> None:
        """仅在当前不处于 ``target`` 时推进状态。

        元认知决策可以直接把回合送到 ``SYNTHESIZING``，
        此时后续阶段不应该再"推进一次"——那会变成一次非法转移。
        """
        if self._state.round_.state is target:
            return
        await self._advance(target, reason)

    async def _record(
        self,
        *,
        event_type: EventType,
        actor_id: str,
        payload: dict[str, object],
        model_info: object = None,
        causation_id: UUID | None = None,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        evidence_refs: tuple[UUID, ...] = (),
    ) -> Event:
        """记录一个认知产物事件。"""
        return await self._runtime.artifact_service.record(
            scope=self._scope,
            event_type=event_type,
            actor_id=actor_id,
            payload=dict(payload),
            model_info=model_info,  # type: ignore[arg-type]
            causation_id=causation_id,
            sensitivity=sensitivity,
            evidence_refs=evidence_refs,
        )

    async def _record_analysis(self, kind: str, outcome: ModuleOutcome[BaseModel]) -> None:
        """记录一次分析模块的产出与调用。

        🔴 **这一步不是可选的。** 逻辑/因果/辩证/哲理四个模块没有
        "领域对象"式的事件类型，若不为它们单独记录，
        这些调用的 ``model`` 与 ``prompt_version`` 就没有任何地方可查——
        不变量 18 会在这里出现一个看不见的缺口。
        """
        await self._record(
            event_type=EventType.COGNITION_ANALYSIS_COMPLETED,
            actor_id=f"{kind}_analyzer",
            model_info=outcome.invocation,
            payload={"analysis_kind": kind, "analysis": outcome.value.model_dump(mode="json")},
        )

    def _diagnostics(self) -> dict[str, object]:
        """回合结束时随终态事件一起落库的诊断信息。

        🔴 **降级必须留痕。** 跳过的步骤与对模型输出的改写
        如果只活在内存里的 ``RoundOutcome`` 上，
        回放与事后审计都看不到它们——那等于"降级发生了但没人知道"。
        """
        return {
            "skipped_steps": list(self._state.skipped),
            "adjustments": list(self._state.adjustments),
        }

    def _make_gateway(self) -> ModelGateway:
        """按当前预算构造模型网关。"""
        return ModelGateway(
            provider=self._runtime.provider,
            prompts=self._runtime.prompts,
            budget=self._budget,
            model=self._runtime.model,
            max_retries=self._runtime.settings.llm_max_retries,
            timeout_seconds=self._runtime.settings.llm_timeout_seconds,
        )

    def _rebudget(self, depth: CognitiveDepth) -> None:
        """把预算下调到深度对应的真实值。"""
        target = CognitiveBudget.for_depth(depth)
        if self._budget.model_calls_used > target.max_model_calls:
            # 已经花掉的钱超过了新预算——保留较宽的预算，
            # 而不是制造一个"预算比已花费还小"的非法回合。
            self._state.adjustments.append(
                f"深度 {depth.value} 的预算低于已消耗的调用数，保留较宽的预算以免超支"
            )
            return
        self._budget = self._budget.rebase(target)
        self._budget.set_tail_reserve(_initial_tail_reserve(depth))
        self._gateway = self._make_gateway()

    def _can_run_optional(self, calls: int = 1) -> bool:
        """可选模块是否还有预算（保留尾部额度）。"""
        return self._budget.can_afford(calls, keep_tail_reserve=True)

    def _outcome(self) -> RoundOutcome:
        """把内部状态转换为对外结果。"""
        return RoundOutcome(
            cognitive_round_id=self._round_id,
            state=self._state.round_.state,
            depth=self._state.depth,
            stop_reason=self._state.round_.stop_reason,
            response_text=self._state.response_text,
            judgment=self._state.judgment,
            reflection=self._state.reflection,
            model_calls_used=self._budget.model_calls_used,
            metacognitive_loops=self._budget.metacognitive_loops_used,
            trigger_event_id=self._user_event.id,
            adjustments=tuple(self._state.adjustments),
            skipped_steps=tuple(self._state.skipped),
        )

    def _require_inquiry(self) -> Inquiry:
        inquiry = self._state.inquiry
        if inquiry is None:  # pragma: no cover - 由阶段顺序保证
            msg = "认知问题尚未框定"
            raise RuntimeError(msg)
        return inquiry

    def _require_bundle(self) -> ContextBundle:
        bundle = self._state.bundle
        if bundle is None:  # pragma: no cover - 由阶段顺序保证
            msg = "上下文尚未构建"
            raise RuntimeError(msg)
        return bundle

    def _require_judgment(self) -> Judgment:
        judgment = self._state.judgment
        if judgment is None:  # pragma: no cover - 由阶段顺序保证
            msg = "判断尚未合成"
            raise RuntimeError(msg)
        return judgment

    def _require_plan(self) -> ResponsePlan:
        plan = self._state.plan
        if plan is None:  # pragma: no cover - 由阶段顺序保证
            msg = "回答方案尚未生成"
            raise RuntimeError(msg)
        return plan


def _has_metacognition(depth: CognitiveDepth) -> bool:
    """该深度的模块矩阵是否包含元认知。"""
    return any(spec.step is CognitiveStep.METACOGNITION for spec in MODULE_MATRIX[depth])


def _initial_tail_reserve(depth: CognitiveDepth) -> int:
    """该深度在分析阶段需要保留的尾部调用数。

    保留的是"回合结束前无论如何都要跑完的模型调用"：
    **判断合成 + 回答渲染**，以及（若启用）**一次元认知复核**。
    D0 不启用元认知，因此只保留 2。
    """
    mandatory = sum(1 for spec in MODULE_MATRIX[depth] if spec.kind is StepKind.MODEL_REQUIRED)
    metacognition = 1 if _has_metacognition(depth) else 0
    return mandatory + metacognition


def _situation_signature(
    *,
    depth: CognitiveDepth,
    evidence_count: int,
    hypothesis_count: int,
) -> str:
    """构造情境签名，用于判定"同类问题"。

    签名只包含**结构特征**，不含问题内容——
    这样"两个不同的关系推测问题"会被归为同一情境，
    而"关系推测"与"事实核验"不会被混为一谈。
    """
    evidence_bucket = "no_evidence" if evidence_count == 0 else "with_evidence"
    return f"{depth.value}|{evidence_bucket}|h{hypothesis_count}"


def _category_for(stage: str) -> ErrorType:
    """返回阶段对应的错误类别。

    🔴 映射表住在 :mod:`ai_psi.learning.error_classifier` 里，**只有一份**。

    阶段 3 这里曾有一份副本，而阶段 6 的归因也需要同一张表。
    两份各自维护的映射一旦漂移，"同一个失败在两个地方得到不同类别"
    就会发生，而且没有任何地方会报错——失败回合照常有类别、
    经验记录也照常有类别，只是它们对不上。
    """
    return STAGE_ERROR_CATEGORY.get(stage, ErrorType.UNKNOWN_ERROR)
