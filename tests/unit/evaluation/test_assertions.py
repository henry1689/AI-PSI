"""B 组：断言注册表与执行器（阶段 7 · S1a）。

这一组测试回答的是：**一条期望是怎么被判定的，以及判定结果里留下了什么。**
"""

from __future__ import annotations

import pytest

from ai_psi.evaluation.assertions import (
    ANALYSIS_KINDS,
    ASSERTIONS,
    CaseObservation,
    assertion_names,
    evaluate,
    spec_for,
)

pytestmark = pytest.mark.unit


def _observation(**overrides: object) -> CaseObservation:
    """一个"处处正常"的观测，测试按需覆盖其中几项。"""
    base: dict[str, object] = {
        "state": "completed",
        "depth": "d0",
        "stop_reason_present": True,
        "response_present": True,
        "response_text": "关于「…」，当前能够给出的只是暂定看法",
        "judgment_present": True,
        "model_calls_used": 4,
        "metacognitive_loops": 0,
        "confidence_band": "low",
        "epistemic_action": "answer_with_caveat",
        "unresolved_unknown_count": 1,
        "counterargument_count": 0,
        "hypothesis_count": 0,
        "analysis_kinds": (),
    }
    base.update(overrides)
    return CaseObservation.model_validate(base)


class TestRegistryShape:
    """注册表本身的性质。"""

    def test_registry_is_not_empty(self) -> None:
        assert len(ASSERTIONS) > 0

    def test_names_are_sorted_and_unique(self) -> None:
        names = assertion_names()
        assert names == tuple(sorted(set(names)))

    def test_every_spec_documents_its_observed_source(self) -> None:
        """🔴 每条断言都必须能回答"observed 值从哪来"。"""
        for name, spec in ASSERTIONS.items():
            assert spec.name == name
            assert spec.observed_from.strip(), name
            assert spec.detail.strip(), name

    def test_every_spec_applies_to_cognitive_behavior(self) -> None:
        """S1a 只有一种 case_type；没有适用的 case_type 的断言不该在注册表里。"""
        for spec in ASSERTIONS.values():
            assert spec.applies_to == frozenset({"cognitive_behavior"})

    def test_analysis_kinds_match_the_runtime(self) -> None:
        """``ANALYSIS_KINDS`` 与实际会出现的 ``analysis_kind`` 必须一致。

        做法是**跑一次真实 D4 回合**（最深的档位会启用全部分析模块），
        而不是去源码里搜字符串——搜字符串证明的是"源码里没有那个词"。
        """
        import asyncio

        from ai_psi.application.cognitive_runtime import RoundRequest
        from ai_psi.evaluation.runner import build_mock_runtime

        runtime = build_mock_runtime()

        async def _run() -> None:
            outcome = await runtime.runtime.run_round(
                RoundRequest(user_message="自由意志与决定论能否同时成立？")
            )
            kinds = await _analysis_kinds(runtime, outcome.cognitive_round_id)
            assert kinds, "D4 回合没有执行任何分析模块——观测口径需要重新确认"
            assert set(kinds) <= set(ANALYSIS_KINDS), (
                f"实际出现了注册表里没有的 analysis_kind：{set(kinds) - set(ANALYSIS_KINDS)}"
            )

        asyncio.run(_run())


async def _analysis_kinds(runtime: object, round_id: object) -> tuple[str, ...]:
    """读一个回合真实执行的分析模块。"""
    from ai_psi.cognition.projection import project_artifacts
    from ai_psi.domain.enums import EventType

    factory = runtime.uow_factory  # type: ignore[attr-defined]
    async with factory() as uow:
        events = await uow.events.read_stream(cognitive_round_id=round_id)
    artifacts = project_artifacts(events)
    return tuple(
        sorted(
            {
                str(record.payload["analysis_kind"])
                for record in artifacts.of_type(EventType.COGNITION_ANALYSIS_COMPLETED)
                if "analysis_kind" in record.payload
            }
        )
    )


class TestEvaluation:
    """判定的行为。"""

    def test_required_keeps_the_real_observed_value_when_passing(self) -> None:
        result = evaluate("depth_level", "required", "d0", _observation(depth="d0"))
        assert result.passed is True
        assert result.observed == "d0"

    def test_required_keeps_the_real_observed_value_when_failing(self) -> None:
        """🔴 失败必须能诊断：只写 ``passed=false`` 的失败是没用的。"""
        result = evaluate("depth_level", "required", "d2", _observation(depth="d0"))
        assert result.passed is False
        assert result.observed == "d0"
        assert "d0" in result.detail

    def test_forbidden_passes_when_the_expectation_does_not_hold(self) -> None:
        result = evaluate("analysis_module_ran", "forbidden", "philosophical", _observation())
        assert result.passed is True

    def test_forbidden_fails_when_the_expectation_holds(self) -> None:
        result = evaluate(
            "analysis_module_ran",
            "forbidden",
            "philosophical",
            _observation(analysis_kinds=("philosophical",)),
        )
        assert result.passed is False
        assert result.observed == ["philosophical"]

    def test_unobservable_is_never_an_automatic_pass(self) -> None:
        """回合没有判断时，置信档位不可观测——**判为不通过**，不是通过。"""
        result = evaluate(
            "confidence_band_at_most",
            "required",
            "moderate",
            _observation(judgment_present=False, confidence_band=None),
        )
        assert result.passed is False
        assert result.observed is None
        assert "不可观测" in result.detail

    def test_unobservable_forbidden_also_fails(self) -> None:
        """forbidden 也不能因为"读不到"而通过——那正是伪造观测的入口。"""
        result = evaluate(
            "unresolved_unknowns_present",
            "forbidden",
            True,
            _observation(judgment_present=False, unresolved_unknown_count=None),
        )
        assert result.passed is False
        assert result.observed is None

    def test_unobservable_response_text_is_not_a_pass(self) -> None:
        result = evaluate(
            "response_contains", "required", "标准大气压", _observation(response_text=None)
        )
        assert result.passed is False
        assert result.observed is None

    def test_response_contains_is_a_string_presence_check(self) -> None:
        passed = evaluate(
            "response_contains",
            "required",
            "暂定看法",
            _observation(response_text="…当前能够给出的只是暂定看法"),
        )
        assert passed.passed is True
        assert passed.observed is True
        failed = evaluate("response_contains", "required", "标准大气压", _observation())
        assert failed.passed is False
        assert failed.observed is False

    @pytest.mark.parametrize(
        ("name", "expected", "overrides", "should_pass"),
        [
            ("depth_at_most", "d1", {"depth": "d0"}, True),
            ("depth_at_most", "d1", {"depth": "d2"}, False),
            ("depth_at_least", "d2", {"depth": "d2"}, True),
            ("depth_at_least", "d2", {"depth": "d1"}, False),
            ("model_calls_at_most", 4, {"model_calls_used": 4}, True),
            ("model_calls_at_most", 4, {"model_calls_used": 5}, False),
            ("model_calls_at_least", 6, {"model_calls_used": 6}, True),
            ("metacognitive_loops_at_most", 0, {"metacognitive_loops": 1}, False),
            ("hypothesis_count_at_most", 0, {"hypothesis_count": 1}, False),
            ("hypothesis_count_at_least", 1, {"hypothesis_count": 2}, True),
            ("confidence_band_at_most", "moderate", {"confidence_band": "low"}, True),
            ("confidence_band_at_most", "moderate", {"confidence_band": "high"}, False),
            ("epistemic_action", "answer", {"epistemic_action": "answer"}, True),
            ("stop_reason_present", True, {"stop_reason_present": False}, False),
            ("final_state", "completed", {"state": "failed"}, False),
        ],
    )
    def test_each_assertion_judges_as_documented(
        self,
        name: str,
        expected: str | int | bool,
        overrides: dict[str, object],
        *,
        should_pass: bool,
    ) -> None:
        result = evaluate(name, "required", expected, _observation(**overrides))
        assert result.passed is should_pass, f"{name}: {result.detail}"

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知的断言模式"):
            evaluate("final_state", "maybe", "completed", _observation())

    def test_unknown_assertion_raises(self) -> None:
        with pytest.raises(KeyError):
            spec_for("looks_thoughtful")


class TestApplicability:
    """断言只对声明过的 case_type 生效。"""

    def test_assertion_not_applicable_to_case_type_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """构造一条只适用于别的 case_type 的断言，确认加载期会拒绝它。

        做法是**改注册表里那一条的 applies_to**，而不是等将来真的出现
        第二种 case_type——那时这条校验已经被测过了。
        """
        from dataclasses import replace

        from ai_psi.evaluation.models import GoldenCase

        spec = ASSERTIONS["final_state"]
        monkeypatch.setitem(
            ASSERTIONS, "final_state", replace(spec, applies_to=frozenset({"replay"}))
        )
        case = {
            "schema_version": 1,
            "case_type": "cognitive_behavior",
            "case_id": "x-001",
            "category": "simple_fact",
            "intent": "x",
            "stimulus": {"input": "x"},
            "expectations": {"required": [{"assertion": "final_state", "expected": "completed"}]},
        }
        with pytest.raises(Exception, match="不适用于"):
            GoldenCase.model_validate(case)
