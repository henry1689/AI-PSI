"""提案状态机的**模型化测试**（阶段 6.5 §五.5）。

🔴 **它要抓的是"服务允许了一条转移表里没有的边"。**

回合状态机有一张显式的转移表（``cognition/state_machine.py``），
因此可以直接做属性测试（见 ``tests/property/test_state_machine_properties.py``）。
**提案状态机没有那张表**——它的合法性写在 `ProposalService` 各方法的
``allowed_from`` 参数里，分散在三处。

分散的后果是：加一个方法、改一个集合、或者在某个方法里漏掉一次检查，
都不会有任何东西发现。而"未经评估就能批准"这条不变量恰恰依赖它。

因此这里引入一个**独立的参照模型**：把允许的边用一份**手写的**表
写出来，再让随机操作序列同时打在模型与服务上，逐条比对。

⚠️ 参照模型必须**独立于被测实现**。从 `ProposalService` 的源码里
提取出 `allowed_from` 来构造模型，测的就是"实现等于它自己"——
那是一条恒真的测试，而它看起来与真的一条一模一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ai_psi.application.proposal_service import ProposalEvaluation, ProposalService
from ai_psi.domain.enums import ErrorType, EvaluationVerdict, ProposalStatus
from ai_psi.domain.exceptions import IllegalStateTransitionError
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 参照模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Edge:
    """一条合法的转移：(操作名, 目标状态)。"""

    operation: str
    target: ProposalStatus


#: 🔴 **手写的转移表——这是参照模型，不是从实现里抄来的。**

#:
#: 它与 `docs/state_machine.md` 描述的提案生命周期一一对应：
#:
#:   DRAFT / PENDING_EVALUATION --evaluate--> EVALUATED
#:   EVALUATED --approve--> APPROVED_FOR_MANUAL_TRIAL
#:   EVALUATED --reject--> REJECTED
#:
#: 两个终态没有出边。
_ALLOWED: dict[ProposalStatus, tuple[_Edge, ...]] = {
    ProposalStatus.DRAFT: (_Edge("evaluate", ProposalStatus.EVALUATED),),
    ProposalStatus.PENDING_EVALUATION: (_Edge("evaluate", ProposalStatus.EVALUATED),),
    ProposalStatus.EVALUATED: (
        _Edge("approve", ProposalStatus.APPROVED_FOR_MANUAL_TRIAL),
        _Edge("reject", ProposalStatus.REJECTED),
    ),
    ProposalStatus.REJECTED: (),
    ProposalStatus.APPROVED_FOR_MANUAL_TRIAL: (),
}

#: 模型认为可以发生转移的起点。
#:
#: 与服务里的常量**无关**——这里只写"业务上第一次评估能从哪来"。
_STARTABLE = (
    ProposalStatus.DRAFT,
    ProposalStatus.PENDING_EVALUATION,
)


def _model_step(status: ProposalStatus, operation: str) -> ProposalStatus | None:
    """模型判定：该操作在当前状态下能不能走通；能则给出目标状态。"""
    for edge in _ALLOWED[status]:
        if edge.operation == operation:
            return edge.target
    return None


# ---------------------------------------------------------------------------
# 被测系统
# ---------------------------------------------------------------------------


@dataclass
class _Subject:
    """一份最小的、可反复重建的被测装配。"""

    service: ProposalService
    uow_factory: Any

    async def new_proposal(self, status: ProposalStatus) -> ImprovementProposal:
        """把一个提案放到指定状态。

        ⚠️ **这里直接写仓储，不走学习链路。** 本文件测的是**状态机**，
        不是"提案从哪来"——后者有自己的端到端测试
        （``tests/scenarios/test_learning_chain.py``）。
        走链路会让每个 hypothesis 例子跑一次完整的三个回合，
        而它与本文件要回答的问题毫无关系。
        """
        proposal = ImprovementProposal(
            created_by="model_test",
            target_component="prompt:logical_analyzer",
            observed_problem="同类推理错误反复出现",
            error_class=ErrorType.REASONING_ERROR,
            proposed_change="检查该情境下的反例检查环节",
            expected_benefit="降低复发率",
            status=status,
        )
        async with self.uow_factory() as uow:
            await uow.proposals.add(proposal)
            await uow.commit()
        return proposal

    async def apply(self, proposal_id: Any, operation: str) -> ProposalStatus | None:
        """对服务执行一次操作。

        Returns:
            新的状态；操作被拒绝时返回 ``None``。
        """
        try:
            if operation == "evaluate":
                transition = await self.service.evaluate(
                    proposal_id,
                    evaluation=ProposalEvaluation(
                        verdict=EvaluationVerdict.IMPROVED, evidence=("历史回放 200 回合",)
                    ),
                )
            elif operation == "approve":
                transition = await self.service.approve_for_manual_trial(
                    proposal_id, approved_by="甲"
                )
            else:
                assert operation == "reject", operation
                transition = await self.service.reject(
                    proposal_id, rejected_by="乙", reason="代价大于收益"
                )
        except IllegalStateTransitionError:
            return None
        return transition.proposal.status


@pytest.fixture
def subject() -> _Subject:
    uow_factory = make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())
    return _Subject(service=ProposalService(uow_factory), uow_factory=uow_factory)


_OPERATIONS = st.sampled_from(["evaluate", "approve", "reject"])


@dataclass
class _Trace:
    """一次比对的结果，失败时用来给出可读的诊断。"""

    steps: list[tuple[ProposalStatus, str, ProposalStatus | None]] = field(default_factory=list)

    def describe(self) -> str:
        return "\n".join(
            f"  {status.value} --{op}--> "
            f"{'拒绝' if result is None else result.value}"
            for status, op, result in self.steps
        )


class TestTheServiceAgreesWithTheReferenceModel:
    """🔴 随机操作序列，逐条比对服务与参照模型。"""

    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        start=st.sampled_from(_STARTABLE),
        operations=st.lists(_OPERATIONS, min_size=1, max_size=6),
    )
    async def test_every_step_matches_the_model(
        self, subject: _Subject, start: ProposalStatus, operations: list[str]
    ) -> None:
        """每一步：模型说能走 ⟺ 服务真的走通了，且落到同一个状态。"""
        proposal = await subject.new_proposal(start)
        trace = _Trace()
        model_status = start

        for operation in operations:
            expected = _model_step(model_status, operation)
            actual = await subject.apply(proposal.id, operation)
            trace.steps.append((model_status, operation, actual))

            assert actual is expected, (
                f"第 {len(trace.steps)} 步与模型不一致："
                f"模型预期 {'拒绝' if expected is None else expected.value}，"
                f"服务给出 {'拒绝' if actual is None else actual.value}\n{trace.describe()}"
            )
            if expected is None:
                # 🔴 被拒绝的一步**必须什么都不改**——状态与版本都不能动。
                async with subject.uow_factory() as uow:
                    unchanged = await uow.proposals.get(proposal.id)
                assert unchanged is not None
                assert unchanged.status is model_status
            else:
                model_status = expected

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(operations=st.lists(_OPERATIONS, min_size=1, max_size=8))
    async def test_a_rejected_step_is_always_side_effect_free(
        self, subject: _Subject, operations: list[str]
    ) -> None:
        """🔴 被拒绝的操作**零副作用**（§五.2）。

        一次"报错但状态已经改了"的失败，在只看异常类型的测试下
        与一次干净的拒绝完全一样——而它会让评审看到一个
        与事件流对不上的状态。
        """
        proposal = await subject.new_proposal(ProposalStatus.DRAFT)
        current = ProposalStatus.DRAFT

        for operation in operations:
            async with subject.uow_factory() as uow:
                before_version = (await uow.proposals.get(proposal.id)).version

            accepted = await subject.apply(proposal.id, operation)

            async with subject.uow_factory() as uow:
                after = await uow.proposals.get(proposal.id)

            if accepted is None:
                assert after.status is current, (
                    f"{current.value} 下的 {operation} 被拒绝了，"
                    f"但状态变成了 {after.status.value}"
                )
                assert after.version == before_version
            else:
                # 🔴 走通的一步**恰好**推进一个版本——不多不少。
                # "推进两个版本"意味着这条路径写了两次，
                # 而事件流里只有一条审批记录。
                assert after.version == before_version + 1
                current = accepted


class TestTheModelItselfIsSound:
    """🔴 参照模型自己也要被检查——一个写错的模型会让上面全部恒真。

    如果模型说"从 ``DRAFT`` 可以 ``approve``"，而服务拒绝，
    上面那条用例会**立刻红**；但如果模型说"``REJECTED`` 可以再
    ``evaluate``"而服务也允许，两者一致地**错**，那条用例照样绿。

    因此这里对模型补三条独立的结构检查，它们来自业务规则而非实现。
    """

    def test_the_model_covers_every_status(self) -> None:
        """每一个状态都要在表里有位置——漏掉一个会 KeyError，
        而 KeyError 在随机测试里表现为"某个例子偶尔炸"，不是"模型不全"。"""
        assert set(_ALLOWED) == set(ProposalStatus)

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        """🔴 终态是**吸收态**。这是"批准之后不能反悔"的形式化。"""
        for status in (ProposalStatus.REJECTED, ProposalStatus.APPROVED_FOR_MANUAL_TRIAL):
            assert _ALLOWED[status] == ()

    def test_no_edge_points_at_draft(self) -> None:
        """🔴 没有回到 ``DRAFT`` 的边——"重新变回草案"是一种抹除历史的方式。"""
        for edges in _ALLOWED.values():
            assert all(edge.target is not ProposalStatus.DRAFT for edge in edges)

    def test_approval_is_only_reachable_from_evaluated(self) -> None:
        """🔴 **未经评估不能批准**——不变量 11 的核心，在模型里先成立。"""
        sources = [
            status
            for status, edges in _ALLOWED.items()
            if any(edge.operation == "approve" for edge in edges)
        ]
        assert sources == [ProposalStatus.EVALUATED]

    def test_the_model_has_no_edge_into_a_promoting_status(self) -> None:
        """🔴 模型里没有任何一条边指向"已生效"——因为枚举里没有那个值。"""
        forbidden = ("active", "enabled", "live", "deployed", "published")
        for edges in _ALLOWED.values():
            for edge in edges:
                assert not any(word in edge.target.value for word in forbidden)


class TestEveryEdgeIsActuallyReachable:
    """🔴 上一条方向是"服务 ⊆ 模型"，这一条是"模型 ⊆ 服务"。

    只做前者的后果：模型里写着一条边、服务其实从不允许它——
    两者不一致，而随机序列只要没走到那条边就不会发现。
    """

    @pytest.mark.parametrize(
        ("start", "operation", "target"),
        [
            (ProposalStatus.DRAFT, "evaluate", ProposalStatus.EVALUATED),
            (ProposalStatus.PENDING_EVALUATION, "evaluate", ProposalStatus.EVALUATED),
            (ProposalStatus.EVALUATED, "approve", ProposalStatus.APPROVED_FOR_MANUAL_TRIAL),
            (ProposalStatus.EVALUATED, "reject", ProposalStatus.REJECTED),
        ],
    )
    async def test_the_edge_really_exists(
        self,
        subject: _Subject,
        start: ProposalStatus,
        operation: str,
        target: ProposalStatus,
    ) -> None:
        proposal = await subject.new_proposal(start)
        assert await subject.apply(proposal.id, operation) is target

    async def test_the_model_has_no_extra_edges_within_a_status(
        self, subject: _Subject
    ) -> None:
        """模型对每个状态允许的操作集合，必须**恰好**是服务允许的那些。

        随机序列证明不了这一点：它只覆盖走过的路径。这条把每个
        状态的全部三个操作都试一遍——多一条会红，少一条也会红。
        """
        for status, edges in _ALLOWED.items():
            allowed_operations = {edge.operation for edge in edges}
            for operation in ("evaluate", "approve", "reject"):
                proposal = await subject.new_proposal(status)
                accepted = await subject.apply(proposal.id, operation) is not None
                assert accepted == (operation in allowed_operations), (
                    f"{status.value} 下的 {operation}："
                    f"模型说 {'可以' if operation in allowed_operations else '不可以'}，"
                    f"服务{'接受了' if accepted else '拒绝了'}"
                )
