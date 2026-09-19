"""Golden Case 的执行器抽象与 PostgreSQL / HTTP 实现（阶段 7 · S2）。

## 为什么要抽象出"执行器"

S1a 的执行路径是"直接调 :meth:`CognitiveRuntime.run_round`"——它证明了
案例契约与断言判定是对的，但**没有证明正式 HTTP 路径能跑**。
S2 要的是后者：经过 FastAPI 路由、正式请求模型、正式应用服务、正式组合根，
把回合真正写进 PostgreSQL。

把"跑一个回合"抽成 :class:`CaseExecutor` 之后，断言判定与结果序列化
两个切片**一行都不用改**——它们本来就不该知道回合是怎么跑起来的。

## 两条路径的观测必须同口径

:class:`CaseObservation` 的字段是断言读的东西。无论走内存还是走 HTTP，
同一个字段必须来自**同一个语义位置**（终态来自回合状态、调用数来自
预算计数、分析模块来自 ``cognition.analysis.completed``……）。
因此 HTTP 执行器是把三个正式只读接口的响应**映射**进同一个结构，
而不是另定义一套"HTTP 版观测"。

## 这个模块不发真实网络请求

``httpx.ASGITransport`` 把请求直接交给 ASGI 应用，不经过 socket。
配合 Mock Provider，整条链路零外部依赖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from ai_psi.api.app import API_PREFIX, create_app
from ai_psi.api.schemas import (
    RoundStatusResponse,
    RoundSummaryResponse,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from ai_psi.config import Settings
from ai_psi.container import Container
from ai_psi.evaluation.assertions import CaseObservation
from ai_psi.evaluation.isolation import require_mock_provider
from ai_psi.evaluation.manifest import prompt_versions_from_invocations
from ai_psi.evaluation.models import GoldenCase

__all__ = [
    "CaseExecution",
    "CaseExecutor",
    "EvaluationExecutionError",
    "HttpEvaluationExecutor",
    "open_http_evaluation_executor",
]


class EvaluationExecutionError(RuntimeError):
    """执行一条案例时失败。

    🔴 调用方（:class:`~ai_psi.evaluation.runner.GoldenRunner`）会把它
    记成**结构化失败**：断言一律标为不可观测且不通过，绝不伪造观测值。
    """


@dataclass(frozen=True, slots=True)
class CaseExecution:
    """一次执行的产物。

    ``observation`` 为 ``None`` 表示**观测不到**（回合没跑起来）——
    它与"观测到空值"是两件事，断言层对此有专门处理。
    """

    cognitive_round_id: UUID | None
    observation: CaseObservation | None
    #: 本回合**实际用到**的 Prompt 版本（任务名 → 语义版本）。
    #:
    #: 🔴 它来自不变量 18 要求的模型调用记录，不是"读代码猜的"。
    #: 一个 D0 的案例不会经过哲学分析，因此这里天然只有它真走过的那几个。
    prompt_versions: Mapping[str, str] = field(default_factory=dict)


class CaseExecutor(Protocol):
    """执行一条 Golden Case。

    实现方只需要回答两件事：用的是哪个 Provider、这条案例跑出了什么。
    """

    @property
    def provider_name(self) -> str:
        """本次执行使用的 Provider 名。"""
        ...

    async def execute(self, case: GoldenCase) -> CaseExecution:
        """执行一条案例。

        Raises:
            EvaluationExecutionError: 执行失败。**不要吞掉**——
                调用方需要它来产出结构化失败。
        """
        ...


# ---------------------------------------------------------------------------
# 正式 HTTP 路径
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _running_application(app: FastAPI) -> AsyncIterator[Container]:
    """驱动 ASGI ``lifespan`` 协议的 startup / shutdown。

    🔴 **``httpx.ASGITransport`` 不会触发 lifespan。** 它只把 HTTP scope
    交给应用，而本项目的组合根（``build_container``）恰恰是在 lifespan 里
    构造的（``api/app.py``）。跳过 lifespan 等于绕开正式装配路径，
    拿到的就不是"正式 HTTP 路径"的证据了。

    这里按 ASGI 规范手动驱动那两个消息：发 ``lifespan.startup``、
    等 ``lifespan.startup.complete``；结束时发 ``lifespan.shutdown``、
    等 ``lifespan.shutdown.complete``。

    Args:
        app: ASGI 应用。

    Yields:
        应用启动后装配出来的容器。
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    startup_done = asyncio.Event()
    shutdown_done = asyncio.Event()
    failures: list[str] = []

    async def receive() -> MutableMapping[str, Any]:
        return await inbox.get()

    # ⚠️ 参数类型必须是 MutableMapping 而不是 dict：ASGI 的 ``Send``
    # 协议声明的是 MutableMapping，而 Callable 的参数是**逆变**的，
    # 收窄成 dict 会让这个函数无法作为 Send 传入。
    async def send(message: MutableMapping[str, Any]) -> None:
        kind = message["type"]
        if kind == "lifespan.startup.complete":
            startup_done.set()
        elif kind == "lifespan.startup.failed":
            failures.append(str(message.get("message", "")))
            startup_done.set()
        elif kind == "lifespan.shutdown.complete":
            shutdown_done.set()

    scope: dict[str, Any] = {
        "type": "lifespan",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
    }
    task = asyncio.create_task(app(scope, receive, send))
    await inbox.put({"type": "lifespan.startup"})
    await startup_done.wait()
    if failures:
        msg = f"应用启动失败：{failures[0]}"
        raise EvaluationExecutionError(msg)

    container = app.state.container
    if not isinstance(container, Container):
        msg = "应用启动后没有装配出 Container——HTTP 路径无法继续"
        raise EvaluationExecutionError(msg)
    try:
        yield container
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await shutdown_done.wait()
        await task


class HttpEvaluationExecutor:
    """通过**正式 HTTP 路径**执行 Golden Case。

    走的路：ASGI 应用 → FastAPI 路由 → 正式请求模型 → 正式应用服务 →
    正式组合根 → Mock Provider → PostgreSQL 仓储。
    """

    def __init__(self, *, client: httpx.AsyncClient, container: Container) -> None:
        """初始化。

        Args:
            client: 指向 ASGI 应用的 HTTP 客户端。
            container: 应用启动后装配出来的容器（**只用于读取身份**）。
        """
        self._client = client
        self._container = container

    @property
    def provider_name(self) -> str:
        """本次执行使用的 Provider 名。"""
        return str(self._container.provider.name)

    async def execute(self, case: GoldenCase) -> CaseExecution:
        """通过正式 HTTP 路由执行一条案例。

        Raises:
            EvaluationExecutionError: 案例的刺激无法被 HTTP 请求模型表达，
                或某个接口没有返回预期状态。
        """
        self._require_expressible(case)

        request = SubmitMessageRequest(
            content=case.stimulus.input,
            user_id=uuid4(),
            requested_depth=case.stimulus.requested_depth,
        )
        submit = await self._client.post(
            f"{API_PREFIX}/conversations/{uuid4()}/messages",
            json=request.model_dump(mode="json"),
        )
        if submit.status_code != 201:
            msg = f"提交消息返回 {submit.status_code}，期望 201"
            raise EvaluationExecutionError(msg)
        accepted = SubmitMessageResponse.model_validate(submit.json())
        round_id = accepted.cognitive_round_id

        summary = RoundSummaryResponse.model_validate(
            await self._get_json(f"{API_PREFIX}/cognitive-rounds/{round_id}/summary")
        )
        # ⚠️ 状态端点的路径**就是** ``/{round_id}`` 本身，没有 ``/status`` 后缀
        # （见 ``api/routes/cognitive_rounds.py``）。多写一段会得到 404——
        # 而这类错误只有真的走 HTTP 路由时才暴露得出来：
        # 内存路径直接调运行时，根本碰不到路由表。
        status = RoundStatusResponse.model_validate(
            await self._get_json(f"{API_PREFIX}/cognitive-rounds/{round_id}")
        )

        return CaseExecution(
            cognitive_round_id=round_id,
            observation=_observation_from(accepted, summary, status),
            # 正式摘要里的模型调用记录就是"实际用到了哪些 Prompt"的证据。
            prompt_versions=prompt_versions_from_invocations(
                view.model_dump(mode="json") for view in summary.model_invocations
            ),
        )

    async def _get_json(self, path: str) -> dict[str, Any]:
        """读一个正式只读接口，返回它的 JSON 对象。

        Raises:
            EvaluationExecutionError: 状态码不是 200，或响应不是 JSON 对象。
        """
        response = await self._client.get(path)
        if response.status_code != 200:
            msg = f"读取 {path} 返回 {response.status_code}，期望 200"
            raise EvaluationExecutionError(msg)
        payload = response.json()
        if not isinstance(payload, dict):
            msg = f"读取 {path} 得到的是 {type(payload).__name__}，期望 JSON 对象"
            raise EvaluationExecutionError(msg)
        return payload

    def _require_expressible(self, case: GoldenCase) -> None:
        """确认这条案例的刺激能被 HTTP 请求模型表达。

        🔴 **不做静默丢弃。** ``SubmitMessageRequest`` 没有承载
        ``conversation_summary`` / ``open_questions`` / ``confirmed_user_goals`` /
        ``system_status`` 的字段。一条用了这些上下文的案例若被"照跑不误"，
        得到的是"输入不完整但看起来通过了"的结果——那比失败更糟。

        Raises:
            EvaluationExecutionError: 案例使用了请求模型表达不了的上下文。
        """
        context = case.stimulus.user_context
        populated = [
            name
            for name, values in (
                ("conversation_summary", context.conversation_summary),
                ("open_questions", context.open_questions),
                ("confirmed_user_goals", context.confirmed_user_goals),
                ("system_status", context.system_status),
            )
            if values
        ]
        if populated:
            msg = (
                f"案例 {case.case_id} 使用了 HTTP 请求模型无法表达的上下文 {populated}；"
                "本切片不修改生产 API，因此拒绝执行而不是静默丢弃这些输入"
            )
            raise EvaluationExecutionError(msg)


def _observation_from(
    accepted: SubmitMessageResponse,
    summary: RoundSummaryResponse,
    status: RoundStatusResponse,
) -> CaseObservation:
    """把三个正式响应映射成可断言的观测。

    🔴 **每个字段都对应 ``CaseObservation`` 里的同一语义位置**，
    与内存路径（``runner.observation_of``）逐字段对齐——否则同一份案例
    在两条路径上会因为口径不同而得到不同的结论。

    Args:
        accepted: ``POST /messages`` 的响应（终态与回答）。
        summary: ``GET /summary`` 的响应（判断、假设、分析模块）。
        status: ``GET /status`` 的响应（预算消耗与元认知循环数）。

    Returns:
        观测。
    """
    judgment = summary.judgment
    return CaseObservation(
        state=accepted.status.value,
        depth=accepted.depth.value,
        stop_reason_present=accepted.stop_reason is not None,
        response_present=accepted.response is not None,
        response_text=accepted.response,
        judgment_present=judgment is not None,
        model_calls_used=status.model_calls_used,
        metacognitive_loops=status.metacognitive_loops,
        confidence_band=None if judgment is None else judgment.confidence_band.value,
        epistemic_action=(
            None if judgment is None else judgment.recommended_epistemic_action.value
        ),
        unresolved_unknown_count=(None if judgment is None else len(judgment.unresolved_unknowns)),
        counterargument_count=(
            None if judgment is None else len(judgment.strongest_counterarguments)
        ),
        hypothesis_count=len(summary.hypotheses),
        analysis_kinds=tuple(sorted(summary.analyses)),
    )


@asynccontextmanager
async def open_http_evaluation_executor(
    settings: Settings,
) -> AsyncIterator[HttpEvaluationExecutor]:
    """装配正式应用、驱动 lifespan，返回走 HTTP 的执行器。

    🔴 **装配后二次确认 Provider 是 Mock。** 配置里写着 ``mock`` 与
    现在跑的真是 Mock 是两件事，而后者才是结果可复现的前提
    （与 S1a 的 ``build_mock_runtime`` 同一条理由）。

    Args:
        settings: 指向**专用评测库**的配置。

    Yields:
        可用的 HTTP 执行器。

    Raises:
        EvaluationExecutionError: 装配出来的 Provider 不是 Mock。
    """
    app = create_app(settings)
    async with _running_application(app) as container:
        require_mock_provider(container.provider.name)
        transport = httpx.ASGITransport(app=app)
        # base_url 是个不可路由的占位符：ASGITransport 直接交给应用，
        # 不会解析它，因此不存在"其实发了网络请求"的可能。
        async with httpx.AsyncClient(
            transport=transport, base_url="http://evaluation.invalid"
        ) as client:
            yield HttpEvaluationExecutor(client=client, container=container)
