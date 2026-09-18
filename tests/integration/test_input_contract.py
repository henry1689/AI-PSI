"""输入契约：**同一组断言，跑在内存与 PostgreSQL 两个后端上**（阶段 6.5 §三.8–9）。

🔴 **本文件要回答的问题：同一个非法请求，两个后端给出的答复一样吗？**

在阶段 6.5 之前，这个问题的答案是"不一定"——因为**没有任何一个测试
同时覆盖这两个维度**：

* ``tests/api/`` 全部用 ``storage_backend="memory"``；
* ``tests/integration/`` 全部直接调服务层，不过 HTTP。

两个维度各自被覆盖，交集为空。而阶段 6 真的出过一次由此产生的缺陷：
``actor_id`` 超过数据库列宽（``varchar(128)``）时，内存后端返回 201、
PostgreSQL 后端返回 **500**——**同一个 HTTP 契约，两个后端两个结果**。

## 判据：不是"返回 4xx"，是"返回**同一个** 4xx"

每条用例同时断言三件事：

1. 状态码是 4xx（不是 500——用户输入不该被报成服务端故障）；
2. 响应体里有**机器可读的** ``code``（而 FastAPI 默认的 422 没有）；
3. 两个后端给出的状态码与 ``code`` **一致**。

第 3 条由 ``@pytest.mark.parametrize("backend", ...)`` 保证同一段代码
跑两遍，而不是把两个后端的输出放在一起比——后者需要在一台机器上
同时活着两个应用，复杂且更容易写错。

## 与服务层的关系

边界挡住的输入**不应该**到达服务层；但服务层自己也要能处理它们
（§三.4：专用领域异常，不用裸 ``ValueError``）。
两条防线各有自己的测试：边界在这里，服务层在
``tests/unit/test_feedback_service.py`` 与 ``test_proposal_service.py``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ai_psi.api.app import API_PREFIX, create_app
from ai_psi.config import Environment, Settings
from ai_psi.container import build_container

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: 只由 Unicode 空白或不可见格式字符组成的输入。
#:
#: 🔴 ``​`` 是这里最要紧的一条：``str.isspace()`` 对它返回
#: ``False``，因此 ``str_strip_whitespace`` 与 ``min_length=1``
#: **都拦不住它**。它会让一条"看起来是空的"记录落进只追加的事件流。
BLANK_INPUTS = [" ", "\t\n", "　", " ", "​", "﻿"]


@pytest.fixture(params=["memory", "postgres"])
async def client(
    request: pytest.FixtureRequest,
    test_settings: Settings,
    clean_tables: None,
) -> AsyncIterator[httpx.AsyncClient]:
    """按后端参数化的 HTTP 客户端。

    Args:
        request: 当前参数（``memory`` / ``postgres``）。
        test_settings: 指向**测试库**的配置（集成夹具已保证库存在且迁移到 head）。
        clean_tables: 每个用例前清库。

    Yields:
        直接打到应用上的异步客户端。
    """
    del clean_tables
    settings = test_settings.model_copy(
        update={
            "storage_backend": request.param,
            "llm_provider": "mock",
            "env": Environment.TESTING,
        }
    )
    app = create_app(settings)
    container = build_container(settings)
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    await container.aclose()


async def _new_round(client: httpx.AsyncClient) -> str:
    """跑一个真实回合，返回它的 id。"""
    created = await client.post(f"{API_PREFIX}/conversations")
    conversation_id = created.json()["conversation_id"]
    response = await client.post(
        f"{API_PREFIX}/conversations/{conversation_id}/messages",
        json={"content": "水在标准大气压下通常多少摄氏度沸腾？"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["cognitive_round_id"])


def _assert_client_error(
    response: httpx.Response, *, expected_status: int | None = None
) -> dict[str, Any]:
    """断言这是一次**结构化的**客户端错误。

    🔴 三件事一起看，缺一不可：

    * 状态码在 4xx（用户输入不该被报成 500）；
    * 有 ``code``（客户端要能机器处理，而不是去解析英文消息）；
    * ``code`` 不是 ``internal_error``（那条路径说明异常穿到了兜底处理器）。

    Returns:
        响应体。
    """
    assert 400 <= response.status_code < 500, (
        f"用户输入被报成了 {response.status_code}：{response.text}"
    )
    if expected_status is not None:
        assert response.status_code == expected_status, response.text
    body: dict[str, Any] = response.json()
    assert "code" in body, f"响应体缺少机器可读的 code：{body}"
    assert body["code"] != "internal_error", body
    return body


class TestBlankInputsAreRejectedOnBothBackends:
    """🔴 纯空白输入在**两个后端**都必须被结构化地拒绝。"""

    @pytest.mark.parametrize("blank", BLANK_INPUTS)
    async def test_blank_message_content(self, client: httpx.AsyncClient, blank: str) -> None:
        """空白消息不该启动一个认知回合——那会白花一次预算。"""
        created = await client.post(f"{API_PREFIX}/conversations")
        conversation_id = created.json()["conversation_id"]
        response = await client.post(
            f"{API_PREFIX}/conversations/{conversation_id}/messages",
            json={"content": blank},
        )
        _assert_client_error(response, expected_status=422)

    @pytest.mark.parametrize("blank", BLANK_INPUTS)
    async def test_blank_feedback_content(self, client: httpx.AsyncClient, blank: str) -> None:
        round_id = await _new_round(client)
        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
            json={"feedback_type": "correction", "content": blank},
        )
        _assert_client_error(response, expected_status=422)

    @pytest.mark.parametrize("blank", BLANK_INPUTS)
    async def test_blank_rejection_reason(self, client: httpx.AsyncClient, blank: str) -> None:
        """可见性断言：**看不见理由的驳回**要能在 Schema 层被挡住。

        ⚠️ 这里不构造提案——那需要走完整的学习链路。本条只验证
        "空白的 reason 连 Schema 都过不去"，而"过不去之后不会把
        错误状态也一起掩盖掉"由 ``test_api_proposals.py`` 覆盖。
        """
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/00000000-0000-0000-0000-000000000001/reject",
            json={"rejected_by": "reviewer", "reason": blank},
        )
        _assert_client_error(response, expected_status=422)


class TestOverLongInputsAreRejectedOnBothBackends:
    """🔴 长度上限必须**在边界上**挡住，而不是在数据库列宽上。"""

    async def test_over_long_actor_id(self, client: httpx.AsyncClient) -> None:
        """这条用例来自一次真实缺陷（见模块文档）。

        内存后端不关心列宽，PostgreSQL 会抛
        ``value too long for type character varying(128)``——
        同一个请求两个后端给出 201 与 500 两种结果。
        """
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/00000000-0000-0000-0000-000000000001/reject",
            json={"rejected_by": "x" * 500, "reason": "不符合验收条件"},
        )
        _assert_client_error(response, expected_status=422)

    async def test_over_long_message(self, client: httpx.AsyncClient) -> None:
        created = await client.post(f"{API_PREFIX}/conversations")
        conversation_id = created.json()["conversation_id"]
        response = await client.post(
            f"{API_PREFIX}/conversations/{conversation_id}/messages",
            json={"content": "长" * 20_000},
        )
        _assert_client_error(response, expected_status=422)

    async def test_over_long_feedback(self, client: httpx.AsyncClient) -> None:
        """反馈正文会进**只追加**的事件表，还会进记忆——必须有上限。

        没有上限的文本字段不是"宽松"，是一条存储耗尽通道：
        一个请求就能往一个删不掉的地方写任意多字节。
        """
        round_id = await _new_round(client)
        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
            json={"feedback_type": "correction", "content": "长" * 20_000},
        )
        _assert_client_error(response, expected_status=422)


class TestMalformedInputsAreRejectedOnBothBackends:
    """非法枚举、非法 UUID、缺字段——一律 422 且带机器可读的 code。"""

    async def test_illegal_enum_value(self, client: httpx.AsyncClient) -> None:
        round_id = await _new_round(client)
        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
            json={"feedback_type": "NOT_A_REAL_TYPE", "content": "有内容"},
        )
        body = _assert_client_error(response, expected_status=422)
        assert any("feedback_type" in item for item in body.get("fields", []))

    async def test_illegal_uuid_in_path(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/not-a-uuid/feedback",
            json={"feedback_type": "correction", "content": "有内容"},
        )
        _assert_client_error(response, expected_status=422)

    async def test_unknown_field_is_rejected(self, client: httpx.AsyncClient) -> None:
        """🔴 ``extra="forbid"`` 是契约的一部分，两个后端都要守。

        静默接受未知字段会让客户端以为它传的参数生效了——
        而服务端从头到尾没看过那个字段。
        """
        created = await client.post(f"{API_PREFIX}/conversations")
        conversation_id = created.json()["conversation_id"]
        response = await client.post(
            f"{API_PREFIX}/conversations/{conversation_id}/messages",
            json={"content": "有效内容", "made_up_field": 1},
        )
        _assert_client_error(response, expected_status=422)


class TestNotFoundIsConsistentOnBothBackends:
    """不存在的资源在两个后端都必须是 404，且带 code。"""

    async def test_feedback_on_a_missing_round(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/00000000-0000-0000-0000-0000000000ff/feedback",
            json={"feedback_type": "correction", "content": "有内容"},
        )
        body = _assert_client_error(response, expected_status=404)
        assert body["code"] == "not_found"

    async def test_reading_a_missing_round(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{API_PREFIX}/cognitive-rounds/00000000-0000-0000-0000-0000000000ff"
        )
        _assert_client_error(response, expected_status=404)
