"""记忆相关路由（任务书 §12.3）。

覆盖五条路由的完整链路：列出 → 纠正 → 删除 → 导出 → 全部删除。
用内存后端 + 本地向量 Provider 跑，零外部依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from ai_psi.api.app import API_PREFIX, create_app
from ai_psi.application.memory_service import MemoryService
from ai_psi.config import Environment, Settings
from ai_psi.container import build_container
from ai_psi.domain.enums import MemoryStatus, MemoryType, SensitivityLevel
from ai_psi.domain.memories import Memory
from ai_psi.memory.write_policy import MemoryWriteProposal

pytestmark = pytest.mark.unit


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """内存后端 + Mock Provider + 本地向量。"""
    settings = Settings(
        storage_backend="memory",
        llm_provider="mock",
        env=Environment.TESTING,
    )
    app = create_app(settings)
    container = build_container(settings)
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    await container.aclose()


async def _seed(client: httpx.AsyncClient, content: str, user_id: Any) -> Any:
    """直接写一条 ACTIVE 记忆（绕过写入策略，路由测试关心的不是策略）。"""
    container: Any = client._transport.app.state.container  # type: ignore[attr-defined]
    memory = Memory(
        created_by="test",
        user_id=user_id,
        memory_type=MemoryType.USER_PREFERENCE,
        content=content,
        sensitivity=SensitivityLevel.PERSONAL,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        status=MemoryStatus.ACTIVE,
        embedding_version=container.embeddings.version,
    )
    async with container.uow_factory() as uow:
        await uow.memories.add(memory)
        await uow.commit()
    return memory


class TestListMemories:
    async def test_lists_only_the_requested_user(self, client: httpx.AsyncClient) -> None:
        """🔴 用户 A 的列表里不出现用户 B 的记忆。"""
        mine, theirs = uuid4(), uuid4()
        await _seed(client, "我的私人偏好", mine)
        await _seed(client, "他的私人偏好", theirs)

        response = await client.get(f"{API_PREFIX}/users/{mine}/memories")
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["memories"][0]["content"] == "我的私人偏好"

    async def test_empty_list_for_a_stranger(self, client: httpx.AsyncClient) -> None:
        await _seed(client, "我的私人偏好", uuid4())
        response = await client.get(f"{API_PREFIX}/users/{uuid4()}/memories")
        assert response.json()["count"] == 0

    async def test_inactive_memories_are_hidden_by_default(self, client: httpx.AsyncClient) -> None:
        user = uuid4()
        memory = await _seed(client, "旧偏好", user)
        await client.delete(f"{API_PREFIX}/memories/{memory.id}", params={"user_id": str(user)})

        assert (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()["count"] == 0
        with_inactive = await client.get(
            f"{API_PREFIX}/users/{user}/memories", params={"include_inactive": True}
        )
        assert with_inactive.json()["count"] == 1
        assert with_inactive.json()["memories"][0]["status"] == "deleted"

    async def test_never_exposes_the_embedding_version(self, client: httpx.AsyncClient) -> None:
        """向量版本是检索实现的内部约定，不外发。"""
        user = uuid4()
        await _seed(client, "我的偏好", user)
        body = (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()
        assert "embedding_version" not in body["memories"][0]

    async def test_flags_conflicts_within_the_result_set(self, client: httpx.AsyncClient) -> None:
        """冲突要被**呈现**，而不是被排序掩盖（任务书 §5.11）。"""
        user = uuid4()
        first = await _seed(client, "用户喜欢简洁回答", user)
        second = await _seed(client, "用户喜欢详细回答", user)
        container: Any = client._transport.app.state.container  # type: ignore[attr-defined]
        async with container.uow_factory() as uow:
            await uow.memories.save(
                second.bumped(contradicts_ids=[first.id]), expected_version=second.version
            )
            await uow.commit()

        body = (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()
        assert all(item["conflicts_within_results"] for item in body["memories"])


class TestCorrectMemory:
    async def test_correction_creates_a_new_version(self, client: httpx.AsyncClient) -> None:
        """🔴 不变量 5：纠正生成新版本，不就地覆盖。"""
        user = uuid4()
        memory = await _seed(client, "用户喜欢非常详细的回答", user)

        response = await client.post(
            f"{API_PREFIX}/memories/{memory.id}/correct",
            json={"user_id": str(user), "new_content": "用户喜欢简洁回答"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["corrected_memory_id"] == str(memory.id)
        assert body["superseded_status"] == "superseded"
        assert body["replacement_status"] == "active"
        assert len(body["audit_event_ids"]) == 2

        listed = (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()
        assert [item["content"] for item in listed["memories"]] == ["用户喜欢简洁回答"]

    async def test_correcting_another_users_memory_is_404(self, client: httpx.AsyncClient) -> None:
        """🔴 404 而不是 403：说"无权限"本身就泄漏了"这条记录存在"。"""
        memory = await _seed(client, "我的偏好", uuid4())
        response = await client.post(
            f"{API_PREFIX}/memories/{memory.id}/correct",
            json={"user_id": str(uuid4()), "new_content": "改动"},
        )
        assert response.status_code == 404

    async def test_unknown_memory_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/memories/{uuid4()}/correct",
            json={"user_id": str(uuid4()), "new_content": "改动"},
        )
        assert response.status_code == 404

    async def test_empty_content_is_rejected(self, client: httpx.AsyncClient) -> None:
        memory = await _seed(client, "我的偏好", uuid4())
        response = await client.post(
            f"{API_PREFIX}/memories/{memory.id}/correct",
            json={"user_id": str(uuid4()), "new_content": ""},
        )
        assert response.status_code == 422


class TestDeleteMemory:
    async def test_delete_marks_it_deleted(self, client: httpx.AsyncClient) -> None:
        user = uuid4()
        memory = await _seed(client, "我的偏好", user)
        response = await client.delete(
            f"{API_PREFIX}/memories/{memory.id}", params={"user_id": str(user)}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

    async def test_deleted_memory_is_gone_from_the_list(self, client: httpx.AsyncClient) -> None:
        """🔴 不变量 15：删除必须作用于检索索引，而不只是改个状态。"""
        user = uuid4()
        memory = await _seed(client, "我的偏好", user)
        await client.delete(f"{API_PREFIX}/memories/{memory.id}", params={"user_id": str(user)})
        assert (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()["count"] == 0

    async def test_delete_of_another_users_memory_is_404(self, client: httpx.AsyncClient) -> None:
        memory = await _seed(client, "我的偏好", uuid4())
        response = await client.delete(
            f"{API_PREFIX}/memories/{memory.id}", params={"user_id": str(uuid4())}
        )
        assert response.status_code == 404

    async def test_missing_user_scope_is_rejected(self, client: httpx.AsyncClient) -> None:
        """``user_id`` **没有默认值**：调用方必须显式写出作用域。

        一个可以省略的作用域参数，迟早在某条调用路径上被省略——
        而那条路径上的后果是把别人的记忆当成自己的。
        """
        memory = await _seed(client, "我的偏好", uuid4())
        response = await client.delete(f"{API_PREFIX}/memories/{memory.id}")
        assert response.status_code == 422


class TestExportUserData:
    async def test_export_contains_the_version_chain(self, client: httpx.AsyncClient) -> None:
        user = uuid4()
        memory = await _seed(client, "用户喜欢非常详细的回答", user)
        await client.post(
            f"{API_PREFIX}/memories/{memory.id}/correct",
            json={"user_id": str(user), "new_content": "用户喜欢简洁回答"},
        )

        response = await client.post(f"{API_PREFIX}/users/{user}/export")
        assert response.status_code == 200
        export = response.json()["export"]
        contents = [record["content"] for record in export["memories"]]
        assert "用户喜欢简洁回答" in contents
        assert "用户喜欢非常详细的回答" in contents

    async def test_export_leaves_an_audit_event(self, client: httpx.AsyncClient) -> None:
        body = (await client.post(f"{API_PREFIX}/users/{uuid4()}/export")).json()
        assert body["audit_event_id"]


class TestDeleteUserData:
    async def test_deletes_everything_for_that_user(self, client: httpx.AsyncClient) -> None:
        mine, theirs = uuid4(), uuid4()
        for index in range(3):
            await _seed(client, f"我的偏好 {index}", mine)
        await _seed(client, "他的偏好", theirs)

        response = await client.delete(f"{API_PREFIX}/users/{mine}/data")
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 3
        assert (await client.get(f"{API_PREFIX}/users/{mine}/memories")).json()["count"] == 0
        # 别人的记忆不受影响
        assert (await client.get(f"{API_PREFIX}/users/{theirs}/memories")).json()["count"] == 1


class TestProposeThroughTheService:
    """写入路径本身由单元测试覆盖；这里只确认它在 API 层可达。"""

    async def test_service_is_wired_into_the_container(self, client: httpx.AsyncClient) -> None:
        container: Any = client._transport.app.state.container  # type: ignore[attr-defined]
        assert isinstance(container.memory_service, MemoryService)

        user = uuid4()
        outcome = await container.memory_service.propose(
            proposal=MemoryWriteProposal(
                user_id=user,
                memory_type=MemoryType.USER_PREFERENCE,
                content="用户偏好简洁回答",
                sensitivity=SensitivityLevel.PERSONAL,
                user_confirmed=True,
            )
        )
        assert outcome.written
        listed = (await client.get(f"{API_PREFIX}/users/{user}/memories")).json()
        assert listed["count"] == 1
