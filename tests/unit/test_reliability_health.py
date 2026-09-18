"""认知健康度（任务书 §12.5、§13.2）。

🔴 本模块要回答的问题是"哪些检查**没有被做**"。

一台机器可以网络全通、数据库连得上、进程响应正常，同时宪法被改坏了、
提案门槛被调到了 1。就绪探针会全部报绿——而它绿得毫无意义。
因此这里的每条断言都在问同一件事：**这份报告会不会替一个坏掉的地基背书？**
"""

from __future__ import annotations

import inspect

import pytest

from ai_psi.cognition.constitution import INVARIANTS
from ai_psi.reliability import health as health_module
from ai_psi.reliability.health import (
    STATUS_DEGRADED,
    STATUS_OK,
    HealthDimension,
    budget_dimension,
    constitution_dimension,
    embedding_dimension,
    invariant_dimension,
    prompt_contract_dimension,
    provider_dimension,
    report,
)
from ai_psi.reliability.invariants import RUNTIME_CHECKED_INVARIANTS

pytestmark = pytest.mark.unit

ALL_IDS = tuple(item.invariant_id for item in INVARIANTS)


def _ok(name: str = "dim") -> HealthDimension:
    return HealthDimension(name=name, ok=True, detail="d")


def _bad(name: str = "dim") -> HealthDimension:
    return HealthDimension(name=name, ok=False, detail="d")


class TestReport:
    def test_all_healthy_is_ok(self) -> None:
        result = report([_ok()], all_invariant_ids=ALL_IDS)
        assert result.status == STATUS_OK
        assert result.ok is True

    def test_one_failure_degrades_the_whole_report(self) -> None:
        """🔴 任一维度不健康即整体 degraded——"大部分正常"不是一个状态。"""
        result = report([_ok("a"), _bad("b")], all_invariant_ids=ALL_IDS)
        assert result.status == STATUS_DEGRADED
        assert result.ok is False

    def test_empty_dimension_list_is_vacuously_ok(self) -> None:
        assert report([], all_invariant_ids=ALL_IDS).ok is True

    def test_unchecked_invariants_are_the_complement(self) -> None:
        result = report([_ok()], all_invariant_ids=ALL_IDS)
        assert set(result.unchecked_invariants) == set(ALL_IDS) - set(RUNTIME_CHECKED_INVARIANTS)

    def test_unchecked_invariants_are_sorted_and_deduplicated(self) -> None:
        result = report([_ok()], all_invariant_ids=(*ALL_IDS, *ALL_IDS))
        assert list(result.unchecked_invariants) == sorted(set(result.unchecked_invariants))

    def test_checked_invariants_are_not_listed_as_unchecked(self) -> None:
        """🔴 一个做了运行期检查的不变量，不该同时出现在"未检查"里。"""
        result = report([_ok()], all_invariant_ids=ALL_IDS)
        assert not (set(result.unchecked_invariants) & RUNTIME_CHECKED_INVARIANTS)

    def test_dimension_lookup(self) -> None:
        result = report([_ok("constitution"), _bad("budget")], all_invariant_ids=ALL_IDS)
        assert result.dimension("budget") is not None
        assert result.dimension("budget").ok is False  # type: ignore[union-attr]
        assert result.dimension("没有这个维度") is None

    def test_report_is_frozen(self) -> None:
        import dataclasses

        result = report([_ok()], all_invariant_ids=ALL_IDS)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = STATUS_DEGRADED  # type: ignore[misc]


class TestConstitutionDimension:
    def test_fingerprint_makes_it_healthy(self) -> None:
        dimension = constitution_dimension(fingerprint="abc123")
        assert dimension.ok is True
        assert "abc123" in dimension.detail

    def test_empty_fingerprint_is_unhealthy(self) -> None:
        assert constitution_dimension(fingerprint="").ok is False

    def test_detail_says_what_changing_it_means(self) -> None:
        """指纹变了意味着宪法被改过——排查时第一个要问的就是这个。"""
        assert "修改宪法" in constitution_dimension(fingerprint="abc").detail


class TestPromptContractDimension:
    def test_registered_contracts_are_healthy(self) -> None:
        dimension = prompt_contract_dimension(task_names=["a", "b"])
        assert dimension.ok is True
        assert "2" in dimension.detail

    def test_no_contract_is_unhealthy(self) -> None:
        """一个任务契约都没有，说明装配漏了——不是"暂时没用到"。"""
        assert prompt_contract_dimension(task_names=[]).ok is False

    def test_names_are_listed(self) -> None:
        dimension = prompt_contract_dimension(task_names=["zeta", "alpha"])
        assert "alpha" in dimension.detail and "zeta" in dimension.detail


class TestBudgetDimension:
    def test_reasonable_configuration_is_healthy(self) -> None:
        dimension = budget_dimension(repetition_threshold=0.8, model_call_ceilings=[3, 5, 9])
        assert dimension.ok is True

    def test_zero_threshold_is_unhealthy(self) -> None:
        """0 会让每一轮都被判为重复。"""
        assert budget_dimension(repetition_threshold=0.0).ok is False

    def test_threshold_above_one_is_unhealthy(self) -> None:
        assert budget_dimension(repetition_threshold=1.5).ok is False

    def test_threshold_of_exactly_one_is_allowed(self) -> None:
        """边界值落在能用的一侧：1.0 意味着"只有完全相同才算重复"。"""
        assert budget_dimension(repetition_threshold=1.0).ok is True

    def test_zero_call_ceiling_is_unhealthy(self) -> None:
        """0 意味着什么分析都跑不了。"""
        assert budget_dimension(repetition_threshold=0.8, model_call_ceilings=[0, 5]).ok is False

    def test_negative_call_ceiling_is_unhealthy(self) -> None:
        assert budget_dimension(repetition_threshold=0.8, model_call_ceilings=[-1]).ok is False

    def test_problems_are_named_in_the_detail(self) -> None:
        dimension = budget_dimension(repetition_threshold=0.0)
        assert "0.0" in dimension.detail

    def test_ceilings_are_reported_when_present(self) -> None:
        assert (
            "调用上限" in budget_dimension(repetition_threshold=0.8, model_call_ceilings=[3]).detail
        )


class TestEmbeddingDimension:
    def test_configured_provider_is_healthy(self) -> None:
        dimension = embedding_dimension(provider="local_hashing", version="1.0.0", dimension=512)
        assert dimension.ok is True

    def test_zero_dimension_is_unhealthy(self) -> None:
        assert embedding_dimension(provider="p", version="v", dimension=0).ok is False

    def test_empty_version_is_unhealthy(self) -> None:
        assert embedding_dimension(provider="p", version="", dimension=512).ok is False

    def test_report_names_the_vector_space(self) -> None:
        """🔴 换 Provider 后旧记忆**会静默地全部检索不到**。

        报告里至少要能看到当前用的是哪个向量空间（risks.md R43）。
        """
        detail = embedding_dimension(
            provider="local_hashing", version="1.0.0", dimension=512
        ).detail
        assert "local_hashing" in detail and "1.0.0" in detail and "512" in detail


class TestProviderDimension:
    def test_ok_status_is_healthy(self) -> None:
        assert provider_dimension(status=STATUS_OK, detail="closed").ok is True

    def test_degraded_status_is_unhealthy(self) -> None:
        """🔴 熔断打开时状态是 degraded——它不是"某个维度降级"，它就是降级。"""
        dimension = provider_dimension(status=STATUS_DEGRADED, detail="熔断打开")
        assert dimension.ok is False
        assert "熔断" in dimension.detail


class TestInvariantDimension:
    def test_intact_guarantees_are_healthy(self) -> None:
        dimension = invariant_dimension()
        assert dimension.ok is True

    def test_detail_names_what_is_checked(self) -> None:
        """报告要说清这一项**覆盖了哪几条**，而不是笼统的"完好"。"""
        detail = invariant_dimension().detail
        assert all(item in detail for item in sorted(RUNTIME_CHECKED_INVARIANTS))

    def test_a_broken_guarantee_is_surfaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ai_psi.reliability import invariants as selfcheck

        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)
        dimension = invariant_dimension()
        assert dimension.ok is False
        assert "I10" in dimension.detail


class TestHealthModuleStaysDependencyFree:
    """🔴 "启动时做一次自检"要求本模块在没有完整运行时时也能用。"""

    def test_it_does_not_import_the_container(self) -> None:
        source = inspect.getsource(health_module)
        assert "ai_psi.container" not in source
        assert "Container" not in source

    def test_no_dimension_builder_takes_a_runtime_object(self) -> None:
        builders = (
            constitution_dimension,
            prompt_contract_dimension,
            budget_dimension,
            embedding_dimension,
            provider_dimension,
            invariant_dimension,
        )
        for builder in builders:
            for parameter in inspect.signature(builder).parameters.values():
                assert parameter.kind is not inspect.Parameter.VAR_KEYWORD
                assert parameter.annotation is not None, builder.__name__
