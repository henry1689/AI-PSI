"""改进提案路由（任务书 §12.4）。

用内存后端跑完整请求链路：登记提案 → 评估 → 批准 / 驳回。

🔴 本文件同时验证一件**不存在**的东西：
整个 OpenAPI 里没有任何一条路径能让提案生效（不变量 11）。
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
from ai_psi.domain.enums import ErrorType
from ai_psi.domain.improvement_proposals import ImprovementProposal

pytestmark = pytest.mark.unit


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """内存后端 + Mock Provider。"""
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


async def _seed(
    client: httpx.AsyncClient,
    *,
    error_class: ErrorType = ErrorType.REASONING_ERROR,
    experiences: int = 3,
) -> UUID:
    """直接经服务写入一条 DRAFT 提案，返回它的 id。

    ⚠️ §12.4 **没有**创建提案的接口——提案由学习链路生成，
    不由客户端提交。这里走的是服务层，不是绕过被测代码。
    """
    container: Any = client._transport.app.state.container  # type: ignore[attr-defined]
    proposal = ImprovementProposal(
        created_by="test",
        target_component="prompt:logical_analyzer",
        observed_problem="同类推理错误反复出现",
        error_class=error_class,
        proposed_change="检查该情境下的反例检查环节",
        expected_benefit="降低复发率",
        supporting_experience_ids=[uuid4() for _ in range(experiences)],
    )
    await container.proposal_service.create(proposal)
    return proposal.id


async def _evaluate(client: httpx.AsyncClient, proposal_id: UUID, **body: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "verdict": "improved",
        "evidence": ["历史回放 200 回合"],
        "actor_id": "reviewer",
    }
    payload.update(body)
    return await client.post(
        f"{API_PREFIX}/improvement-proposals/{proposal_id}/evaluate", json=payload
    )


class TestListAndRead:
    async def test_empty_list(self, client: httpx.AsyncClient) -> None:
        body = (await client.get(f"{API_PREFIX}/improvement-proposals")).json()
        assert body["count"] == 0
        assert body["proposals"] == []

    async def test_lists_a_seeded_proposal(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        body = (await client.get(f"{API_PREFIX}/improvement-proposals")).json()
        assert body["count"] == 1
        assert body["proposals"][0]["id"] == str(proposal_id)

    async def test_list_filters_by_status(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)

        drafts = (
            await client.get(f"{API_PREFIX}/improvement-proposals", params={"status": "draft"})
        ).json()
        evaluated = (
            await client.get(f"{API_PREFIX}/improvement-proposals", params={"status": "evaluated"})
        ).json()
        assert drafts["count"] == 0
        assert evaluated["count"] == 1

    async def test_list_rejects_an_unknown_status(self, client: httpx.AsyncClient) -> None:
        response = await client.get(
            f"{API_PREFIX}/improvement-proposals", params={"status": "已生效"}
        )
        assert response.status_code == 422

    async def test_get_by_id(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        body = (await client.get(f"{API_PREFIX}/improvement-proposals/{proposal_id}")).json()
        assert body["id"] == str(proposal_id)
        assert body["status"] == "draft"
        assert body["error_class"] == "reasoning_error"

    async def test_unknown_proposal_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"{API_PREFIX}/improvement-proposals/{uuid4()}")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    async def test_view_exposes_the_evidence_count(self, client: httpx.AsyncClient) -> None:
        """条数对应不变量 10 的门槛——评审要看的正是这个数字。"""
        proposal_id = await _seed(client, experiences=3)
        body = (await client.get(f"{API_PREFIX}/improvement-proposals/{proposal_id}")).json()
        assert body["supporting_experience_count"] == 3

    async def test_view_never_claims_it_can_become_active(self, client: httpx.AsyncClient) -> None:
        """🔴 不变量 11：这个字段恒为 false，而且要**看得见**。"""
        proposal_id = await _seed(client)
        body = (await client.get(f"{API_PREFIX}/improvement-proposals/{proposal_id}")).json()
        assert body["can_become_active"] is False


class TestEvaluate:
    async def test_evaluation_moves_to_evaluated(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        response = await _evaluate(client, proposal_id)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "evaluated"
        assert body["version"] == 2
        UUID(body["audit_event_id"])

    async def test_inconclusive_is_accepted(self, client: httpx.AsyncClient) -> None:
        """「看不出」是一个真实的结论，不是失败。"""
        proposal_id = await _seed(client)
        response = await _evaluate(client, proposal_id, verdict="inconclusive")
        assert response.status_code == 200

    async def test_unknown_verdict_is_rejected(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        assert (await _evaluate(client, proposal_id, verdict="还行")).status_code == 422

    async def test_unknown_proposal_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await _evaluate(client, uuid4())).status_code == 404


class TestApprovalRequiresEvaluation:
    async def test_approving_a_draft_is_refused(self, client: httpx.AsyncClient) -> None:
        """🔴 未经评估的批准等于凭印象拍板——返回 409 并说明原因。"""
        proposal_id = await _seed(client)
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/approve-for-manual-trial",
            json={"approved_by": "评审"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "illegal_state_transition"

    async def test_rejecting_a_draft_is_refused(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/reject",
            json={"rejected_by": "评审", "reason": "不感兴趣"},
        )
        assert response.status_code == 409

    async def test_approval_succeeds_after_evaluation(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)

        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/approve-for-manual-trial",
            json={"approved_by": "评审", "note": "试验范围限于该情境"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved_for_manual_trial"
        assert body["can_become_active"] is False

    async def test_approved_status_is_terminal(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)
        await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/approve-for-manual-trial",
            json={"approved_by": "评审"},
        )

        body = (await client.get(f"{API_PREFIX}/improvement-proposals/{proposal_id}")).json()
        assert body["is_terminal"] is True

    async def test_a_terminal_proposal_cannot_be_rejected_afterwards(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 否则会得到一条**同时**被批准和驳回的提案。"""
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)
        await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/approve-for-manual-trial",
            json={"approved_by": "评审"},
        )

        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/reject",
            json={"rejected_by": "另一个评审", "reason": "我觉得不行"},
        )
        assert response.status_code == 409


class TestReject:
    async def test_rejection_requires_a_reason(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/reject",
            json={"rejected_by": "评审", "reason": ""},
        )
        assert response.status_code == 422

    async def test_rejection_after_evaluation(self, client: httpx.AsyncClient) -> None:
        proposal_id = await _seed(client)
        await _evaluate(client, proposal_id)

        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{proposal_id}/reject",
            json={"rejected_by": "评审", "reason": "对照指标预计会退化"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"

    async def test_unknown_proposal_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"{API_PREFIX}/improvement-proposals/{uuid4()}/reject",
            json={"rejected_by": "评审", "reason": "不存在的东西"},
        )
        assert response.status_code == 404


class TestNoPromotionSurface:
    """🔴 不变量 11：整个 API 面上都没有让提案生效的入口。"""

    _FORBIDDEN = ("activate", "promote", "publish", "deploy", "apply", "enable")

    async def test_no_route_could_make_a_proposal_effective(
        self, client: httpx.AsyncClient
    ) -> None:
        paths = set((await client.get("/openapi.json")).json()["paths"])
        offending = [
            path
            for path in paths
            if "improvement-proposals" in path
            and any(word in path.lower() for word in self._FORBIDDEN)
        ]
        assert offending == []

    async def test_stage6_routes_are_all_registered(self, client: httpx.AsyncClient) -> None:
        paths = set((await client.get("/openapi.json")).json()["paths"])
        assert f"{API_PREFIX}/improvement-proposals" in paths
        assert f"{API_PREFIX}/improvement-proposals/{{proposal_id}}" in paths
        assert f"{API_PREFIX}/improvement-proposals/{{proposal_id}}/evaluate" in paths
        assert (
            f"{API_PREFIX}/improvement-proposals/{{proposal_id}}/approve-for-manual-trial" in paths
        )
        assert f"{API_PREFIX}/improvement-proposals/{{proposal_id}}/reject" in paths

    async def test_request_schemas_forbid_extras(self, client: httpx.AsyncClient) -> None:
        schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
        for name in ("EvaluateProposalRequest", "ApproveProposalRequest", "RejectProposalRequest"):
            assert schemas[name]["additionalProperties"] is False, name
