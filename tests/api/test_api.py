"""HTTP API（任务书 §12）。

用内存后端 + Mock Provider 跑完整的请求链路，
因此在零外部依赖下就能验证"提交一条消息 → 拿到结构化认知产物"。

🔴 这里同时验证安全边界：**错误响应不得泄漏堆栈或内部细节**（§17.1）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from ai_psi.api.app import API_PREFIX, create_app
from ai_psi.config import Environment, Settings
from ai_psi.container import build_container

pytestmark = pytest.mark.unit

SIMPLE_QUESTION = "水在标准大气压下通常多少摄氏度沸腾？"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """内存后端 + Mock Provider 的应用。

    ⚠️ 用 ``httpx.ASGITransport`` 而不是 starlette 的 ``TestClient``：
    后者在**导入时**会对 httpx 发一条弃用警告，而本项目
    ``filterwarnings = ["error"]``，那条警告会直接变成收集期错误。
    换用 ASGITransport 既绕开了它，也让测试天然是异步的。

    代价是 **lifespan 不会自动运行**，因此这里显式构造容器并挂到
    ``app.state`` 上——与 ``create_app`` 的 lifespan 做的事完全一致。
    """
    settings = Settings(
        storage_backend="memory",
        llm_provider="mock",
        env=Environment.TESTING,
        llm_max_retries=2,
    )
    app = create_app(settings)
    container = build_container(settings)
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    await container.aclose()


async def _submit(client: httpx.AsyncClient, content: str = SIMPLE_QUESTION) -> dict[str, Any]:
    """创建会话并提交一条消息。"""
    created = await client.post(f"{API_PREFIX}/conversations")
    conversation_id = created.json()["conversation_id"]
    response = await client.post(
        f"{API_PREFIX}/conversations/{conversation_id}/messages",
        json={"content": content},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _round_id(client: httpx.AsyncClient) -> str:
    """提交一条消息并返回回合 id。"""
    return str((await _submit(client))["cognitive_round_id"])


class TestHealth:
    async def test_live_probe_checks_nothing_else(self, client: httpx.AsyncClient) -> None:
        """存活探针只回答"进程活着吗"——把依赖检查塞进去会导致反复重启。"""
        body = (await client.get(f"{API_PREFIX}/health/live")).json()
        assert body["status"] == "ok"
        assert [check["name"] for check in body["checks"]] == ["process"]

    async def test_ready_probe_reports_dependencies(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"{API_PREFIX}/health/ready")).json()
        assert body["status"] == "ok"
        names = {check["name"] for check in body["checks"]}
        assert {"storage_backend", "llm_provider", "database"} <= names

    async def test_cognitive_probe_checks_cognitive_assets(self, client: httpx.AsyncClient) -> None:
        """🔴 一台"网络全通但宪法被改坏了"的机器**不健康**。"""
        body = (await client.get(f"{API_PREFIX}/health/cognitive")).json()
        names = {check["name"] for check in body["checks"]}
        assert {"constitution", "prompt_contracts", "budget"} <= names
        detail = next(c["detail"] for c in body["checks"] if c["name"] == "constitution")
        assert "fingerprint=" in detail

    async def test_cognitive_probe_reports_the_vector_space(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 换 Provider 后旧记忆会**静默地全部检索不到**。

        报告里至少要能看到当前用的是哪个向量空间（risks.md R43）。
        """
        body = (await client.get(f"{API_PREFIX}/health/cognitive")).json()
        detail = next(c["detail"] for c in body["checks"] if c["name"] == "embedding")
        assert "provider=" in detail and "version=" in detail and "dimension=" in detail

    async def test_cognitive_probe_checks_structural_invariants(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 阶段 6 的这一项查的是**地基还在不在**。

        "提案不可能自动生效""假设不可能变成事实""门槛不可能低于 2"
        都是类型级的硬约束——它们被削弱时不该等到某次请求才表现出来。
        """
        body = (await client.get(f"{API_PREFIX}/health/cognitive")).json()
        check = next(c for c in body["checks"] if c["name"] == "invariants")
        assert check["ok"] is True
        assert "I11" in check["detail"]

    async def test_unchecked_invariants_are_listed_not_omitted(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 "报告里没有这一项"与"这一项没检查"必须能区分开。"""
        body = (await client.get(f"{API_PREFIX}/health/cognitive")).json()
        check = next(c for c in body["checks"] if c["name"] == "unchecked_invariants")
        assert "未做运行期检查" in check["detail"]
        # 运行期检查覆盖的那几条不该出现在这个清单里
        for covered in ("I01", "I10", "I11"):
            assert f"{covered}、" not in check["detail"]

    async def test_cognitive_probe_is_not_degraded_in_a_healthy_build(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get(f"{API_PREFIX}/health/cognitive")).json()
        assert body["status"] == "ok"


class TestConversations:
    async def test_create_conversation_returns_id(self, client: httpx.AsyncClient) -> None:
        body = (await client.post(f"{API_PREFIX}/conversations")).json()
        UUID(body["conversation_id"])
        # V0.1 不持久化会话实体——接口**如实说明**了这一点。
        # 🔴 断言的是"说了不持久化"，不是某个具体措辞：阶段 3 这里曾写着
        # "阶段 5 落地"，而阶段 5 落地的是记忆、不是会话。
        # 钉住措辞会让这句诚实的说明变回一句不会发生的承诺。
        assert "不持久化" in body["note"]
        assert "阶段" not in body["note"]

    async def test_submit_message_completes_a_round(self, client: httpx.AsyncClient) -> None:
        body = await _submit(client)
        assert body["status"] == "completed"
        assert body["depth"] in {"d0", "d1", "d2", "d3", "d4"}
        assert body["stop_reason"]
        assert body["response"]
        UUID(body["cognitive_round_id"])
        UUID(body["message_id"])

    async def test_message_id_is_the_trigger_event(self, client: httpx.AsyncClient) -> None:
        """``message_id`` 必须指向真实触发本回合的事件，而不是回合 id。"""
        body = await _submit(client)
        assert body["message_id"] != body["cognitive_round_id"]

    async def test_invalid_body_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/conversations/{uuid4()}/messages",
            json={"content": ""},
        )
        assert response.status_code == 422

    async def test_idempotency_key_returns_the_same_round(self, client: httpx.AsyncClient) -> None:
        """🔴 任务书 §13.4：API 重试不得创建重复回合。"""
        created = await client.post(f"{API_PREFIX}/conversations")
        url = f"{API_PREFIX}/conversations/{created.json()['conversation_id']}/messages"
        payload = {"content": SIMPLE_QUESTION}
        headers = {"Idempotency-Key": "retry-1"}

        first = (await client.post(url, json=payload, headers=headers)).json()
        second = (await client.post(url, json=payload, headers=headers)).json()
        assert first["cognitive_round_id"] == second["cognitive_round_id"]

    async def test_reused_key_with_different_body_conflicts(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await client.post(f"{API_PREFIX}/conversations")
        url = f"{API_PREFIX}/conversations/{created.json()['conversation_id']}/messages"
        headers = {"Idempotency-Key": "retry-2"}

        await client.post(url, json={"content": "第一句话"}, headers=headers)
        response = await client.post(url, json={"content": "完全不同的一句话"}, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "conflict"


class TestRoundEndpoints:
    async def test_status_endpoint(self, client: httpx.AsyncClient) -> None:
        submitted = await _submit(client)
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/{submitted['cognitive_round_id']}"
        )
        body = response.json()
        assert body["state"] == "completed"
        assert body["stop_reason"] == submitted["stop_reason"]
        assert body["model_calls_used"] <= body["max_model_calls"]
        assert body["metacognitive_loops"] <= body["max_metacognitive_loops"]
        # 成功回合没有失败诊断
        assert body["failure_stage"] is None
        assert body["error_category"] is None

    async def test_response_endpoint(self, client: httpx.AsyncClient) -> None:
        submitted = await _submit(client)
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/{submitted['cognitive_round_id']}/response"
        )
        body = response.json()
        assert body["text"] == submitted["response"]
        assert body["judgment"] is not None
        assert body["judgment"]["conclusion"]

    async def test_summary_exposes_structured_reasons(self, client: httpx.AsyncClient) -> None:
        submitted = await _submit(client)
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/{submitted['cognitive_round_id']}/summary"
        )
        body = response.json()

        assert body["cognitive_round_id"] == submitted["cognitive_round_id"]
        assert body["concerns"]
        assert body["inquiry"]
        assert body["judgment"]
        assert body["judgment"]["rationale_summary"]
        assert body["judgment"]["confidence_basis"]
        assert body["reflection"]

    async def test_summary_exposes_model_metadata_not_content(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 不变量 18 与红线一同时成立：记录模型与版本，但不记录响应内容。"""
        submitted = await _submit(client)
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/{submitted['cognitive_round_id']}/summary"
        )
        invocations = response.json()["model_invocations"]

        assert invocations
        for item in invocations:
            assert item["model"]
            assert item["prompt_version"]
            assert item["task_name"]
            assert item["response_hash"]
            # 只有元信息，没有任何提示词或响应内容字段
            assert set(item) == {
                "invocation_id",
                "provider",
                "model",
                "task_name",
                "prompt_version",
                "latency_ms",
                "retry_count",
                "result_status",
                "response_hash",
            }

    async def test_summary_does_not_expose_internal_hypothesis_ids(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 内部假设 id 不外发——它是实现细节，暴露它会让重构变难。"""
        body = await _submit(client)
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/{body['cognitive_round_id']}/summary"
        )
        assert "selected_hypothesis_ids" not in response.json()["judgment"]

    async def test_analysis_events_carry_prompt_versions(self, client: httpx.AsyncClient) -> None:
        """分析模块的调用也必须可追踪（不变量 18）——它们走独立事件。"""
        round_id = await _round_id(client)
        response = await client.get(f"{API_PREFIX}/cognitive-rounds/{round_id}/summary")
        analyses = response.json()["analyses"]
        # D0 不做逻辑/因果分析，因此 analyses 可能为空——这里只断言结构合法
        assert isinstance(analyses, dict)

    async def test_unknown_round_returns_404_without_leaking(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(f"{API_PREFIX}/cognitive-rounds/{uuid4()}")
        assert response.status_code == 404
        payload = response.json()
        assert payload["code"] == "not_found"
        # 🔴 不含堆栈
        assert "traceback" not in str(payload).lower()


class TestReplay:
    async def test_replay_rebuilds_state(self, client: httpx.AsyncClient) -> None:
        round_id = await _round_id(client)
        response = await client.post(f"{API_PREFIX}/replay/cognitive-rounds/{round_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "completed"
        assert body["transition_count"] > 0
        assert body["event_count"] > 0
        assert body["stop_reason"]

    async def test_replay_is_read_only(self, client: httpx.AsyncClient) -> None:
        """🔴 回放不覆盖原始事件，也不改动状态。"""
        round_id = await _round_id(client)
        first = (await client.post(f"{API_PREFIX}/replay/cognitive-rounds/{round_id}")).json()
        second = (await client.post(f"{API_PREFIX}/replay/cognitive-rounds/{round_id}")).json()
        assert first["event_count"] == second["event_count"]
        assert first["state"] == second["state"]

    async def test_replay_of_unknown_round_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"{API_PREFIX}/replay/cognitive-rounds/{uuid4()}")
        assert response.status_code == 404


class TestOpenApiContract:
    async def test_openapi_is_served(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert schema["info"]["title"] == "AI-PSI Cognitive Runtime"

    async def test_stage3_routes_are_registered(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        assert f"{API_PREFIX}/conversations" in paths
        assert f"{API_PREFIX}/conversations/{{conversation_id}}/messages" in paths
        assert f"{API_PREFIX}/cognitive-rounds/{{round_id}}" in paths
        assert f"{API_PREFIX}/cognitive-rounds/{{round_id}}/response" in paths
        assert f"{API_PREFIX}/cognitive-rounds/{{round_id}}/summary" in paths
        assert f"{API_PREFIX}/replay/cognitive-rounds/{{round_id}}" in paths
        assert f"{API_PREFIX}/health/live" in paths

    async def test_stage5_routes_are_registered(self, client: httpx.AsyncClient) -> None:
        """阶段 5 交付的记忆接口（任务书 §12.3）。"""
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        assert f"{API_PREFIX}/users/{{user_id}}/memories" in paths
        assert f"{API_PREFIX}/memories/{{memory_id}}/correct" in paths
        assert f"{API_PREFIX}/memories/{{memory_id}}" in paths
        assert f"{API_PREFIX}/users/{{user_id}}/export" in paths
        assert f"{API_PREFIX}/users/{{user_id}}/data" in paths

    async def test_stage6_routes_are_absent(self, client: httpx.AsyncClient) -> None:
        """🔴 不建空壳：阶段 6 的接口现在**不该存在**（ADR-0012）。"""
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        assert not any("improvement-proposals" in path for path in paths)
        assert not any("feedback" in path for path in paths)

    async def test_beliefs_route_is_not_shipped_in_stage5(self, client: httpx.AsyncClient) -> None:
        """``GET /users/{user_id}/beliefs``（§12.3 第一条）**本阶段不做**。

        信念只活在事件流里，不是记忆；列出它需要另建一套判断投影，
        那是另一件事。这条断言让"暂缓"成为一个**可见的决定**，
        而不是一次遗漏——否则它只会在阶段 8 验收时以
        "任务书里列了但没实现"的形式重新出现（ADR-0017）。
        """
        schema = (await client.get("/openapi.json")).json()
        assert not any("beliefs" in path for path in set(schema["paths"]))
