"""反馈路由（任务书 §12.2）。

用内存后端 + Mock Provider 跑完整请求链路：
提交消息 → 拿到回合 → 对回合给反馈 → 看记忆里发生了什么。
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

CORRECTION_TEXT = "我说的适应是改变方法，不是放弃原则"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """内存后端 + Mock Provider。"""
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


async def _round_for(client: httpx.AsyncClient, user_id: UUID) -> str:
    """跑一个带归属用户的完整回合，返回回合 id。

    走 HTTP 创建会话再提交消息，与真实调用路径一致——
    这样"反馈挂在一个真实回合上"这件事是被验证过的，
    而不是靠夹具手工塞一行进存储。
    """
    created = await client.post(f"{API_PREFIX}/conversations")
    conversation_id = created.json()["conversation_id"]
    response = await client.post(
        f"{API_PREFIX}/conversations/{conversation_id}/messages",
        json={"content": "一个人应该坚持自我，还是适应环境？", "user_id": str(user_id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["cognitive_round_id"])


async def _feedback(client: httpx.AsyncClient, round_id: str, **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "feedback_type": "correction",
        "content": CORRECTION_TEXT,
    }
    payload.update(body)
    return await client.post(
        f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
        json=payload,
    )


class TestFeedbackEndpoint:
    async def test_feedback_is_accepted(self, client: httpx.AsyncClient) -> None:
        round_id = await _round_for(client, uuid4())
        response = await _feedback(client, round_id)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["round_id"] == round_id
        assert body["feedback_type"] == "correction"
        UUID(body["audit_event_id"])

    async def test_memory_effect_is_always_reported(self, client: httpx.AsyncClient) -> None:
        """🔴 "什么都没做"必须能被读出来，而不能与"记下来了"长得一样。"""
        round_id = await _round_for(client, uuid4())
        body = (await _feedback(client, round_id)).json()

        assert body["memory_effect"] == "none"
        assert body["memory_written"] is False
        assert body["memory_id"] is None
        assert body["reasons"]

    async def test_unknown_round_is_404(self, client: httpx.AsyncClient) -> None:
        response = await _feedback(client, str(uuid4()))
        assert response.status_code == 404
        payload = response.json()
        assert payload["code"] == "not_found"
        # 🔴 不含堆栈
        assert "traceback" not in str(payload).lower()

    async def test_empty_content_is_rejected(self, client: httpx.AsyncClient) -> None:
        round_id = await _round_for(client, uuid4())
        assert (await _feedback(client, round_id, content="")).status_code == 422

    @pytest.mark.parametrize("blank", ["   ", "\t\n", " "])
    async def test_whitespace_content_is_rejected_not_a_500(
        self, client: httpx.AsyncClient, blank: str
    ) -> None:
        """🔴 **用户输入造成的 500 一律是缺陷。**

        ``min_length=1`` 挡不住 ``"   "``：它过得了 schema，然后在
        "要求更新记忆"的分支里撞出 `Memory.content` 的去空白校验失败——
        那是一个 pydantic `ValidationError`，不是领域异常，
        于是一个填错内容的请求被报成 **服务端故障**。
        """
        round_id = await _round_for(client, uuid4())
        for payload in ({}, {"allow_memory_update": True}):
            response = await _feedback(client, round_id, content=blank, **payload)
            assert response.status_code == 422, response.text
            assert "internal_error" not in response.text

    async def test_whitespace_content_never_reaches_the_write_policy(
        self, client: httpx.AsyncClient
    ) -> None:
        """连"策略对它说了什么"都不该发生——它在 schema 层就被挡住了。"""
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        await _feedback(client, round_id, content="   ", allow_memory_update=True)

        listed = (await client.get(f"{API_PREFIX}/users/{user_id}/memories")).json()
        assert listed["count"] == 0

    async def test_unknown_feedback_type_is_rejected(self, client: httpx.AsyncClient) -> None:
        round_id = await _round_for(client, uuid4())
        response = await _feedback(client, round_id, feedback_type="表扬")
        assert response.status_code == 422

    async def test_unknown_field_is_rejected(self, client: httpx.AsyncClient) -> None:
        round_id = await _round_for(client, uuid4())
        assert (await _feedback(client, round_id, mood="开心")).status_code == 422


class TestMemoryUpdateThroughTheAPI:
    async def test_correction_can_update_memory(self, client: httpx.AsyncClient) -> None:
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        body = (await _feedback(client, round_id, allow_memory_update=True)).json()

        assert body["memory_effect"] == "written"
        assert body["memory_written"] is True
        assert body["memory_id"] is not None

        listed = (await client.get(f"{API_PREFIX}/users/{user_id}/memories")).json()
        assert listed["count"] == 1
        assert listed["memories"][0]["content"] == CORRECTION_TEXT

    async def test_the_written_memory_is_scoped_to_the_round_owner(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 不变量 14：另一个用户的检索结果里不该出现它。"""
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        await _feedback(client, round_id, allow_memory_update=True)

        other = (await client.get(f"{API_PREFIX}/users/{uuid4()}/memories")).json()
        assert other["count"] == 0

    async def test_agreement_never_updates_memory(self, client: httpx.AsyncClient) -> None:
        """🔴 赞同说的是「你做得对不对」，不是「事实是什么」。"""
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        body = (
            await _feedback(
                client,
                round_id,
                feedback_type="agreement",
                content="同意",
                allow_memory_update=True,
            )
        ).json()

        assert body["memory_effect"] == "not_eligible"
        assert body["memory_written"] is False
        listed = (await client.get(f"{API_PREFIX}/users/{user_id}/memories")).json()
        assert listed["count"] == 0

    async def test_forbidden_content_is_stopped_by_the_policy(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 反馈**不是**绕过记忆红线的入口（任务书 §10.3）。"""
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        body = (
            await _feedback(
                client,
                round_id,
                content="他最近确诊了抑郁症",
                allow_memory_update=True,
            )
        ).json()

        assert body["memory_effect"] == "rejected_by_policy"
        assert body["memory_written"] is False
        listed = (await client.get(f"{API_PREFIX}/users/{user_id}/memories")).json()
        assert listed["count"] == 0

    async def test_rejected_feedback_is_still_recorded(self, client: httpx.AsyncClient) -> None:
        """🔴 被拒的写入同样是审计信息。

        "系统曾经想记住什么但被挡住了"与"系统记住了什么"一样重要。
        """
        round_id = await _round_for(client, uuid4())
        body = (
            await _feedback(
                client,
                round_id,
                content="他最近确诊了抑郁症",
                allow_memory_update=True,
            )
        ).json()
        UUID(body["audit_event_id"])

    async def test_repeating_the_correction_is_reported_as_duplicate(
        self, client: httpx.AsyncClient
    ) -> None:
        user_id = uuid4()
        round_id = await _round_for(client, user_id)
        await _feedback(client, round_id, allow_memory_update=True)
        second = (await _feedback(client, round_id, allow_memory_update=True)).json()

        assert second["memory_effect"] == "duplicate"
        listed = (await client.get(f"{API_PREFIX}/users/{user_id}/memories")).json()
        assert listed["count"] == 1


class TestOpenApiContract:
    async def test_feedback_route_is_registered(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        assert f"{API_PREFIX}/cognitive-rounds/{{round_id}}/feedback" in set(schema["paths"])

    async def test_the_two_stage6_groups_are_separate(self, client: httpx.AsyncClient) -> None:
        """反馈与提案是两组接口，路径上不互相依赖（ADR-0012）。"""
        schema = (await client.get("/openapi.json")).json()
        paths = set(schema["paths"])
        assert f"{API_PREFIX}/cognitive-rounds/{{round_id}}/feedback" in paths
        assert f"{API_PREFIX}/improvement-proposals" in paths

    async def test_request_schema_forbids_extras(self, client: httpx.AsyncClient) -> None:
        schema = (await client.get("/openapi.json")).json()
        request = schema["components"]["schemas"]["FeedbackRequest"]
        assert request["additionalProperties"] is False
