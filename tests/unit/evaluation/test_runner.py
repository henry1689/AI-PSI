"""C 组：Mock 执行器（阶段 7 · S1a）。

这一组测试回答的是：**案例是怎么被执行的，以及边界在哪。**
其中三条（不联网 / 不需要凭据 / 不建数据库）是**隔离边界**的钉子——
它们必须能在"有人把评测接到真实环境上"时变红。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_psi.evaluation.loader import load_dataset
from ai_psi.evaluation.runner import GoldenRunner, build_mock_runtime, deterministic_settings

pytestmark = pytest.mark.unit

CaseDict = dict[str, Any]
WriteDataset = Callable[..., Path]


@pytest.fixture(scope="module")
def runner() -> GoldenRunner:
    """一个共享的 Mock 执行器（装配一次即可，装配本身不依赖案例）。"""
    return GoldenRunner(build_mock_runtime())


class TestExecution:
    """单条与多条案例的执行。"""

    async def test_a_passing_case(
        self, runner: GoldenRunner, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        dataset = load_dataset(write_dataset(case_dict))
        result = await runner.run_case(dataset.cases[0])
        assert result.passed is True
        assert result.failure_reason is None
        assert result.observation is not None
        assert result.observation.state == "completed"
        assert result.cognitive_round_id is not None

    async def test_a_failing_case_keeps_its_observation(
        self, runner: GoldenRunner, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """失败时**必须**留下真实观测，否则这条失败无法诊断。"""
        case_dict["expectations"] = {
            "required": [{"assertion": "depth_level", "expected": "d4"}],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        dataset = load_dataset(write_dataset(case_dict))
        result = await runner.run_case(dataset.cases[0])
        assert result.passed is False
        assert result.observation is not None
        # 断言的是"真实观测被保留"，而不是某个具体档位——
        # 具体档位属于案例的期望，不属于这条基础设施测试。
        assert result.observation.depth != "d4"
        failed = [a for a in result.assertions if not a.passed]
        assert failed[0].name == "depth_level"
        assert failed[0].observed == result.observation.depth
        assert result.failure_reason is not None
        assert "depth_level" in result.failure_reason

    async def test_one_failure_does_not_hide_the_others(
        self, runner: GoldenRunner, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """一条炸掉不该带走整个数据集——其余案例必须照样有结果。"""
        good = copy.deepcopy(case_dict)
        bad = copy.deepcopy(case_dict)
        bad["case_id"] = "fixture-bad"
        bad["expectations"] = {
            "required": [{"assertion": "depth_level", "expected": "d4"}],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        root = write_dataset(good)
        write_dataset(bad, subdir="bad")
        dataset = load_dataset(root)

        result = await runner.run_dataset(dataset.cases)
        assert result.total == 2
        assert result.passed == 1
        assert result.failed == 1
        assert result.passed_overall is False
        statuses = {case.case_id: case.passed for case in result.cases}
        assert statuses == {"fixture-001": True, "fixture-bad": False}

    async def test_summary_counts_add_up(
        self, runner: GoldenRunner, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        root = write_dataset(case_dict)
        for index in range(3):
            case = copy.deepcopy(case_dict)
            case["case_id"] = f"fixture-10{index}"
            write_dataset(case, subdir=f"d{index}")
        result = await runner.run_dataset(load_dataset(root).cases)
        assert result.total == result.passed + result.failed
        assert result.passed_overall is True


class TestExecutionFailures:
    """回合本身抛异常时的行为。"""

    async def test_an_exception_is_recorded_not_swallowed_as_a_pass(
        self,
        runner: GoldenRunner,
        case_dict: CaseDict,
        write_dataset: WriteDataset,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """🔴 抛异常**不等于**通过，也不等于"观测到不成立"。"""

        async def _boom(*_: object, **__: object) -> None:
            msg = "人为制造的回合失败"
            raise RuntimeError(msg)

        monkeypatch.setattr(runner._runtime.runtime, "run_round", _boom)
        dataset = load_dataset(write_dataset(case_dict))
        result = await runner.run_case(dataset.cases[0])

        assert result.passed is False
        assert result.observation is None
        assert result.failure_reason is not None
        assert "RuntimeError" in result.failure_reason
        assert result.failure_detail is not None
        assert "人为制造的回合失败" in result.failure_detail
        # 断言**不评估**：没有观测就没有"通过"，也不伪造 observed=False。
        assert result.assertions
        assert all(not a.passed for a in result.assertions)
        assert all(a.observed is None for a in result.assertions)
        assert all("不可观测" in a.detail for a in result.assertions)


class TestIsolationBoundaries:
    """隔离边界的钉子。每一条都对应一个"如果有人接错环境就变红"。"""

    def test_no_network_request_is_made(
        self, case_dict: CaseDict, write_dataset: WriteDataset, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任何一次真实 HTTP 请求都会让这条测试失败。"""
        import asyncio

        async def _forbid(self: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
            msg = f"评测不应发起网络请求：{request.method} {request.url}"
            raise AssertionError(msg)

        monkeypatch.setattr(httpx.AsyncClient, "send", _forbid)
        runner = GoldenRunner(build_mock_runtime())
        dataset = load_dataset(write_dataset(case_dict))
        result = asyncio.run(runner.run_case(dataset.cases[0]))
        assert result.passed is True

    def test_no_real_credentials_are_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """评测配置跑在 Mock + 内存上，且**这台机器上的环境变量改不动它**。

        做法是把环境变量设成"如果被读到就会去连真实供应商/数据库"的值，
        再断言拿到的仍然是 mock + memory。这比断言某个内部属性更贴近
        真正要防的事：评测结果不该取决于跑它的机器。
        """
        monkeypatch.setenv("AI_PSI_LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("AI_PSI_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("AI_PSI_DEEPSEEK_API_KEY", "should-never-be-read")
        settings = deterministic_settings()
        assert settings.llm_provider == "mock"
        assert settings.storage_backend == "memory"

    def test_no_database_engine_is_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把"建数据库引擎"变成硬失败：内存后端不该碰它。"""

        def _forbid(*_: object, **__: object) -> None:
            msg = "评测不应创建数据库引擎"
            raise AssertionError(msg)

        monkeypatch.setattr("ai_psi.container.create_async_engine", _forbid)
        runtime = build_mock_runtime()
        assert runtime.provider_name == "mock"

    def test_a_non_mock_provider_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置被改成真实 Provider 时必须**显式失败**，不能静默继续。"""

        def _deepseek() -> str:
            return "deepseek"

        monkeypatch.setattr(
            "ai_psi.evaluation.runner.deterministic_settings",
            lambda: _settings_with_provider("deepseek"),
        )
        with pytest.raises(RuntimeError, match="deepseek"):
            build_mock_runtime()


def _settings_with_provider(name: str) -> Any:
    from ai_psi.config import Settings

    return Settings(
        _env_file=None,
        storage_backend="memory",
        llm_provider=name,
        llm_model="whatever",
    )
