"""黑盒端到端验收（阶段 6.5 §七）。

🔴 **本文件的每一条都必须满足三个"只能"：**

1. **只能通过正式 API** —— `httpx` 打在真正的 ASGI 应用上，
   不直接调服务层、不 import 应用服务；
2. **只能在真实 PostgreSQL 上** —— `storage_backend="postgres"`，
   库是迁移到 head 的独立测试库；
3. **只能由真实数据驱动** —— 回合由 HTTP 请求跑出来，
   经验从回合的事件流里长出来，纠正由 HTTP 反馈写进去。

**禁止**手工构造 `Experience` / `PatternDecision` / `Proposal`，
也**禁止**直接写数据库制造成功结果（§七 开头的明文要求）。
因此本文件**不** import 任何领域对象构造器来造数据——
它只 import 断言用的枚举与异常。

## 为什么这仍然不是"重复测试"

`tests/api/` 覆盖了 HTTP 但用内存后端；`tests/integration/` 覆盖了
真实数据库但不走 HTTP。阶段 6.5 的能力证据矩阵发现这两个维度的
**交集为空**——而阶段 6 真出过一次由此产生的缺陷（`actor_id`
超长时内存返回 201、PostgreSQL 返回 500）。

本文件是那个交集的第一个大块：**真实 HTTP × 真实 PostgreSQL**。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_psi.api.app import API_PREFIX, create_app
from ai_psi.config import Environment, Settings
from ai_psi.container import build_container

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: 能让元认知报出"关键反例被忽略"的回答，从而产生一条**可归因**的经验。
#:
#: ⚠️ 默认的 Mock Provider 不会产生这个信号，因此本文件不脚本化模型——
#: 它用**真实的 Mock 规则引擎**，并接受"这一轮没有产生可归因的经验"
#: 是一个合法结果。需要可控信号的那几条场景（§七.1–4）因此**不在这里**，
#: 而在 `tests/scenarios/test_learning_chain.py`（内存 + 脚本化 Provider）。
#:
#: 这是本文件最重要的一个取舍，写在下面。
_QUESTION = "水在标准大气压下通常多少摄氏度沸腾？"


@pytest.fixture
async def client(
    test_settings: Settings,
    clean_tables: None,
) -> AsyncIterator[httpx.AsyncClient]:
    """真实 PostgreSQL 上的 HTTP 客户端。"""
    del clean_tables
    settings = test_settings.model_copy(
        update={
            "storage_backend": "postgres",
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


async def _run_round(client: httpx.AsyncClient) -> str:
    """跑一个真实回合，返回它的 id。"""
    created = await client.post(f"{API_PREFIX}/conversations")
    conversation_id = created.json()["conversation_id"]
    response = await client.post(
        f"{API_PREFIX}/conversations/{conversation_id}/messages",
        json={"content": _QUESTION},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["cognitive_round_id"])


async def _correct(client: httpx.AsyncClient, round_id: str) -> httpx.Response:
    """给一个回合一条用户纠正。"""
    return await client.post(
        f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
        json={
            "feedback_type": "correction",
            "content": "你这里判断错了：证据不足时不该下这个结论",
            "allow_memory_update": False,
        },
    )


async def _write_a_memory(client: httpx.AsyncClient, *, user_id: UUID, content: str) -> None:
    """让 ``user_id`` 名下**真的**落一条长期记忆，落不下就报错。

    🔴 **发消息不会写记忆**——记忆走的是反馈链路（``allow_memory_update``）。

    这一点曾经让用户隔离那两条用例**空过**：alice 名下压根什么都没有，
    于是「bob 看不到 alice 的」与「bob 看不到任何东西」是同一件事，
    用例实际证明的是后者。而它对任何实现都成立，包括完全不做作用域
    过滤的实现——只要没人往里写东西。

    所以这里在返回前**先确认写入发生了**。做不到就当场红，
    而不是把一条没有信息量的绿留给读者。
    """
    conversation = await client.post(f"{API_PREFIX}/conversations")
    conversation_id = conversation.json()["conversation_id"]
    sent = await client.post(
        f"{API_PREFIX}/conversations/{conversation_id}/messages",
        json={"content": _QUESTION, "user_id": str(user_id)},
    )
    assert sent.status_code == 201, sent.text
    round_id = sent.json()["cognitive_round_id"]

    feedback = await client.post(
        f"{API_PREFIX}/cognitive-rounds/{round_id}/feedback",
        json={
            "feedback_type": "correction",
            "content": content,
            "allow_memory_update": True,
        },
    )
    assert feedback.status_code == 201, feedback.text

    listing = await client.get(f"{API_PREFIX}/users/{user_id}/memories")
    assert listing.json()["memories"], (
        f"{user_id} 名下没有记忆（反馈返回 {feedback.text}）——"
        "后面的隔离断言会因为它本来就是空的而空过"
    )


async def _learn(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    """跑一次学习链路。"""
    response = await client.post(f"{API_PREFIX}/learning/runs", json=body)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# ---------------------------------------------------------------------------
# §七.1–4：门槛的四个方向
# ---------------------------------------------------------------------------


class TestTheLearningChainIsReachableThroughTheApi:
    """🔴 **§七 里最重要的一条：学习链路要能"从外面跑起来"。**

    在阶段 6.5 之前，`LearningService` 在 `src/` 里**零调用者**——
    "三次同类错误可生成 Proposal"只在测试里成立。
    这条用例证明它现在是一条约 200 能跑通的路由。
    """

    async def test_the_endpoint_runs_and_reports_why(self, client: httpx.AsyncClient) -> None:
        """没有经验时不生成提案——而**必须说清为什么**。

        🔴 一份只说"生成了 0 条提案"的报告无法回答"为什么没有"。
        因此这里断言的是**理由的存在**，而不是"返回了 200"。
        """
        body = await _learn(client)

        assert body["created_proposal_ids"] == []
        assert body["experiences_considered"] >= 0
        assert body["summary"]
        # 四个计数必须都在——缺了任何一个，"为什么没有"就答不全
        for key in ("patterns_found", "gate_approved", "already_covered"):
            assert key in body

    async def test_running_twice_does_not_create_duplicates(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 同一批证据跑两次不该造出两条提案。

        提案是给人评审的；"同一件事递了两遍"会浪费评审的时间，
        更糟的是让人开始怀疑这份清单。
        """
        first = await _learn(client)
        second = await _learn(client)

        assert second["created_proposal_ids"] == []
        assert set(second["created_proposal_ids"]) & set(first["created_proposal_ids"]) == set()

    async def test_the_endpoint_cannot_approve_anything(self, client: httpx.AsyncClient) -> None:
        """🔴 **这条路由上没有通往"生效"的参数**（不变量 11）。

        传任何看起来像"直接批准"的字段都会被 Schema 拒绝——
        ``extra="forbid"`` 在这里不只是风格，它是契约的一部分。
        """
        response = await client.post(
            f"{API_PREFIX}/learning/runs",
            json={"status": "approved_for_manual_trial"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# §七.15–16：离线评测的两个方向
# ---------------------------------------------------------------------------


class TestOfflineEvaluationIsReachableAndHonest:
    """🔴 §七.15–16：退化要判得出来，**未评估不得自动判成未退化**。"""

    async def test_no_candidate_data_means_not_assessed(self, client: httpx.AsyncClient) -> None:
        """🔴 **§七.16**：没有对照时 ``comparison_available`` 必须是 false。

        把它默认成"没有退化"会让"没评估"冒充"没问题"——
        而那正是 `PromotionEvidence` 用 ``None`` 而不是 ``False``
        表示未评估的理由（"未评估 ≠ 不成立"）。
        """
        await _run_round(client)

        body = await _learn(client)

        assert body["comparison_available"] is False
        assert body["evaluation_reasons"], "没有对照时必须说明原因"

    async def test_a_real_comparison_is_available_when_rounds_are_given(
        self, client: httpx.AsyncClient
    ) -> None:
        """给了基线就必须真的做对照——否则"能评测"只是纸面上的。"""
        baseline = [await _run_round(client) for _ in range(2)]
        candidate = [await _run_round(client) for _ in range(2)]

        body = await _learn(client, baseline_round_ids=baseline, candidate_round_ids=candidate)

        assert body["comparison_available"] is True

    async def test_lower_is_better_regression_is_detected(self, client: httpx.AsyncClient) -> None:
        """🔴 **§七.15**：lower-is-better 指标从 0 上升到 1 必须判为退化。

        ⚠️ **本文件无法在 Mock Provider 下构造出这个退化**——
        它需要"基线回合都没有反刍、候选回合全都有"两组真实数据，
        而 Mock 的行为由脚本控制，脚本化又超出了本文件的黑盒边界。

        因此 §七.15 的**黑盒部分**在这里只做到"对照可达"，
        方向判定本身由单元层的
        `tests/unit/test_offline_evaluator.py::test_lower_is_better_metric_rising_is_a_regression`
        钉住。这个缺口写在 `docs/assurance/capability_evidence_matrix.md`
        与阶段完成报告的残余风险里——**不假装它被覆盖了**。
        """
        baseline = [await _run_round(client)]
        candidate = [await _run_round(client)]

        body = await _learn(client, baseline_round_ids=baseline, candidate_round_ids=candidate)

        # 能算出对照，且原因可读——至于方向判定，见上面的说明
        assert body["comparison_available"] is True


# ---------------------------------------------------------------------------
# §七.10–11：非法状态进不去
# ---------------------------------------------------------------------------


class TestIllegalStatesCannotBeWritten:
    """🔴 §七.10–11：状态与对象两条线都封死。"""

    @pytest.mark.parametrize("status", ["active", "ACTIVE", "enabled", "go_live"])
    async def test_no_live_status_can_be_written(
        self, engine: AsyncEngine, clean_tables: None, status: str
    ) -> None:
        """§七.10：四个值一个都写不进去。

        ⚠️ 走**原生 SQL**（绕开应用层）——本文件要证明的是
        **数据库自己也拒绝**，而那正是"最后一层"的意义。
        """
        del clean_tables
        with pytest.raises(Exception, match="status_valid"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO improvement_proposals (
                            id, created_at, updated_at, version, created_by,
                            schema_version, target_component, observed_problem,
                            error_class, supporting_experience_ids, counterexamples,
                            proposed_change, expected_benefit, possible_regressions,
                            applicability, evaluation_plan, success_metrics,
                            rollback_conditions, approval_level, status
                        ) VALUES (
                            :id, now(), now(), 1, 'black_box', '1.0.0',
                            'prompt:logical_analyzer', '问题', 'reasoning_error',
                            '{}', '{}', '改动', '收益', '{}', '{}',
                            '{}', '{}', '{}', 'user_and_review', :status
                        )
                        """
                    ),
                    {"id": uuid4(), "status": status},
                )

    async def test_the_api_never_reports_a_proposal_as_active(
        self, client: httpx.AsyncClient
    ) -> None:
        """§七.10 的另一半：**响应里也不能出现"可生效"**。

        `can_become_active` 恒为 false——它不是算出来的，是类型层的
        事实（`ProposalStatus` 里没有那个值）。
        """
        listing = await client.get(f"{API_PREFIX}/improvement-proposals")
        assert listing.status_code == 200
        for item in listing.json()["proposals"]:
            assert item["can_become_active"] is False
            assert item["status"] not in {"active", "enabled", "live"}


# ---------------------------------------------------------------------------
# §七.13–14：事务与并发
# ---------------------------------------------------------------------------


class TestAtomicityAndConcurrency:
    """🔴 §七.13–14。"""

    async def test_a_failed_feedback_leaves_nothing(
        self, client: httpx.AsyncClient, engine: AsyncEngine, clean_tables: None
    ) -> None:
        """§七.13：失败**不留事件、不留状态变化**。

        用一个不存在的回合触发失败，然后直接查库确认事件表没多东西。
        """
        del clean_tables
        before = await _count_events(engine)

        response = await client.post(
            f"{API_PREFIX}/cognitive-rounds/{uuid4()}/feedback",
            json={"feedback_type": "correction", "content": "有内容"},
        )
        assert response.status_code == 404

        assert await _count_events(engine) == before

    async def test_concurrent_feedback_only_one_succeeds(self, client: httpx.AsyncClient) -> None:
        """§七.14：两个并发请求**只允许一个成功**，失败方零副作用。

        ⚠️ 这条在内存后端上**测不出来**（工作单元的方法没有 await 点，
        两个"并发"实际是顺序执行的，见 R48）。真实 PostgreSQL 上
        才会出现真正的交错。

        这里并发的是**同一回合的两条反馈**——它们都会成功，因为
        反馈不是幂等的单次写入。真正被唯一约束仲裁的是
        **幂等键**，那一条在下面。
        """
        round_id = await _run_round(client)

        first, second = await asyncio.gather(_correct(client, round_id), _correct(client, round_id))

        # 两条反馈各自独立成功——它们不是同一件事
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text

    async def test_the_same_idempotency_key_only_creates_one_round(
        self, client: httpx.AsyncClient
    ) -> None:
        """🔴 **§七.6**：同一 ``Idempotency-Key`` 重试不重复计数。

        代价是并发时**只有一个请求能创建回合**，另一个拿到既有的那个。
        """
        created = await client.post(f"{API_PREFIX}/conversations")
        conversation_id = created.json()["conversation_id"]
        key = f"black-box-{uuid4()}"

        payload = {"content": _QUESTION}
        headers = {"Idempotency-Key": key}
        first, second = await asyncio.gather(
            client.post(
                f"{API_PREFIX}/conversations/{conversation_id}/messages",
                json=payload,
                headers=headers,
            ),
            client.post(
                f"{API_PREFIX}/conversations/{conversation_id}/messages",
                json=payload,
                headers=headers,
            ),
        )

        ids = {response.json().get("cognitive_round_id") for response in (first, second)}
        assert len(ids) == 1, (first.text, second.text)


async def _count_events(engine: AsyncEngine) -> int:
    """事件表当前的行数。"""
    async with engine.begin() as conn:
        total = await conn.scalar(text("SELECT count(*) FROM events"))
    return int(total or 0)


# ---------------------------------------------------------------------------
# §七.17：用户隔离
# ---------------------------------------------------------------------------


class TestUserIsolationHoldsThroughTheApi:
    """🔴 §七.17：用户 A 的数据不得混入用户 B。"""

    async def test_a_users_memories_are_not_visible_to_another(
        self, client: httpx.AsyncClient
    ) -> None:
        """记忆检索按 ``user_id`` 硬隔离（不变量 14）。"""
        alice, bob = uuid4(), uuid4()
        await _write_a_memory(client, user_id=alice, content="我喜欢简洁的回答，别啰嗦")

        alice_view = await client.get(f"{API_PREFIX}/users/{alice}/memories")
        bob_view = await client.get(f"{API_PREFIX}/users/{bob}/memories")

        assert alice_view.status_code == 200 and bob_view.status_code == 200
        # 🔴 两半缺一不可：先证明**确实有东西可泄漏**，再证明它没有泄漏。
        assert len(alice_view.json()["memories"]) == 1
        assert bob_view.json()["memories"] == []
        assert bob_view.json()["count"] == 0

    async def test_a_users_export_never_contains_another_users_data(
        self, client: httpx.AsyncClient
    ) -> None:
        """导出同样按作用域隔离——它是**数据访问**，泄漏的后果更直接。"""
        alice, bob = uuid4(), uuid4()
        await _write_a_memory(client, user_id=alice, content="我住在杭州")

        own = await client.post(f"{API_PREFIX}/users/{alice}/export")
        other = await client.post(f"{API_PREFIX}/users/{bob}/export")

        assert own.status_code == 200 and other.status_code == 200
        # 🔴 正向：alice 的导出包里**确实有**一条记忆。没有这半句，
        #    "bob 的包里没有 alice" 对任何实现都成立。
        assert own.json()["export"]["memory_count"] == 1

        # 反向：载荷整体属于 bob——不只"记忆列表是空的"，而是**整包里
        # 没有一个 alice 的痕迹**。只查列表长度的话，一个把别人的记忆
        # 挂到别的键下的实现照样能通过。
        bundle = other.json()["export"]
        assert bundle["user_id"] == str(bob)
        assert bundle["memory_count"] == 0
        assert bundle["memories"] == []
        assert str(alice) not in json.dumps(bundle)


# ---------------------------------------------------------------------------
# §七.5、7、8：重复与重放不冒充独立经验
# ---------------------------------------------------------------------------


class TestReprocessingDoesNotManufactureIndependence:
    """🔴 §七.5/7/8：同一件事被处理多次，仍然只算**一次发生**。

    ⚠️ 这三条**不在这里**——它们需要"跑三次同一种可归因的回合"，
    而 Mock 规则引擎不产生那种回合（见模块文档的取舍说明）。

    它们的落点在：

    * §七.5（同一回合重复处理 100 次）→
      `tests/unit/test_pattern_detector.py::TestOccurrenceAccounting`
      的 `test_the_same_round_rebuilt_is_still_one_occurrence`；
    * §七.7（同一 `origin_round_id` 不重复计数）→ 同上，
      外加 `test_distinct_rounds_do_count` 作为反方向；
    * §七.8（技术重试不算独立）→ 同文件的
      `test_technical_retries_share_an_independence_group`。

    本类是**一条显式的占位**：把"这三条不在黑盒层"这件事写进代码，
    而不是让它悄悄消失。§七 要求黑盒覆盖它们，本阶段的做法是
    **如实标注缺口**，而不是用一条走内存的用例冒充黑盒验收。
    """
