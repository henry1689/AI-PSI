"""F 组：可复现性清单（阶段 7 · S3）。

这一组回答两个问题：

1. **摘要敏不敏感**——语义变了它必须变，排版变了它必须不变；
2. **身份诚不诚实**——取不到就说取不到，不编一个值填进去。

🔴 全部是纯单元测试：不连数据库、不调 Provider、不发网络。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation import assertions as assertions_module
from ai_psi.evaluation.assertions import (
    ASSERTIONS,
    AssertionSpec,
    ExpectedKind,
    registry_digest,
)
from ai_psi.evaluation.loader import load_dataset
from ai_psi.evaluation.manifest import (
    MANIFEST_SCHEMA_VERSION,
    CodeIdentity,
    EvaluationIdentity,
    PromptIdentity,
    ProviderIdentity,
    ReproducibilityManifest,
    RuntimeIdentity,
    StorageIdentity,
    collect_code_identity,
    collect_provider_identity,
    collect_runtime_identity,
    compare_manifests,
    dataset_digest,
    prompt_identity,
    prompt_versions_from_invocations,
    storage_identity,
)
from ai_psi.evaluation.serialization import dumps

pytestmark = pytest.mark.unit

#: 一条可通过加载器校验的最小案例。``{...}`` 由测试填。
_CASE_TEMPLATE = """\
schema_version: 1
case_type: cognitive_behavior
case_id: {case_id}
category: simple_fact
intent: 清单测试用的合成案例
stimulus:
  input: {user_input}
  user_context: {{}}
  requested_depth: null
expectations:
  required:
    - assertion: final_state
      expected: completed
  forbidden:
    - assertion: analysis_module_ran
      expected: philosophical
"""


def _write_dataset(tmp_path: Path, files: dict[str, str]) -> Path:
    """把若干 YAML 文件写成一个数据集目录。"""
    root = tmp_path / "datasets"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _case_text(case_id: str = "case-001", user_input: str = "一个合成问题。") -> str:
    return _CASE_TEMPLATE.format(case_id=case_id, user_input=user_input)


def _manifest(**overrides: Any) -> ReproducibilityManifest:
    """一份"处处正常"的清单，测试按需覆盖字段。

    用关键字参数覆盖**嵌套**字段（如 ``dataset_digest``）而不是整个子模型，
    免得每条用例都要重建六个对象。
    """
    defaults: dict[str, Any] = {
        "commit_sha": "a" * 40,
        "working_tree_clean": True,
        "package_version": "0.1.0",
        "case_schema_version": 1,
        "dataset_digest": "sha256:dataset",
        "dataset_case_count": 10,
        "assertion_registry_digest": "sha256:registry",
        "execution_mode": "in_memory",
        "prompt_digest": "sha256:prompts",
        "provider_name": "mock",
        "model_id": "mock-model-v1",
        "configuration_digest": "sha256:config",
        "python_version": "3.13.2",
        "storage_backend": "memory",
        "alembic_revision": None,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
    }
    defaults.update(overrides)
    return ReproducibilityManifest(
        manifest_schema_version=defaults["manifest_schema_version"],
        code=CodeIdentity(
            commit_sha=defaults["commit_sha"],
            working_tree_clean=defaults["working_tree_clean"],
            package_version=defaults["package_version"],
        ),
        evaluation=EvaluationIdentity(
            case_schema_version=defaults["case_schema_version"],
            dataset_digest=defaults["dataset_digest"],
            dataset_case_count=defaults["dataset_case_count"],
            assertion_registry_digest=defaults["assertion_registry_digest"],
            execution_mode=defaults["execution_mode"],
        ),
        prompts=PromptIdentity(versions={}, digest=defaults["prompt_digest"]),
        provider=ProviderIdentity(
            provider_name=defaults["provider_name"],
            model_id=defaults["model_id"],
            deterministic=defaults["provider_name"] == "mock",
            network_allowed=False,
            parameters={},
            configuration_digest=defaults["configuration_digest"],
        ),
        runtime=RuntimeIdentity(
            python_version=defaults["python_version"],
            ai_psi_version=defaults["package_version"],
        ),
        storage=StorageIdentity(
            backend=defaults["storage_backend"],
            alembic_revision=defaults["alembic_revision"],
        ),
    )


class TestDatasetDigest:
    """A 组：数据集摘要基于**语义**，不基于字节。"""

    def test_an_identical_dataset_gives_the_same_digest(self, tmp_path: Path) -> None:
        first = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        second = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": _case_text()}))
        assert dataset_digest(first.cases) == dataset_digest(second.cases)

    def test_yaml_comments_do_not_change_the_digest(self, tmp_path: Path) -> None:
        """🔴 注释不是评测语义。改注释不该让数据集变成"另一个数据集"。"""
        with_comment = "# 这一行是注释\n" + _case_text() + "\n# 尾部注释\n"
        plain = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        commented = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": with_comment}))
        assert dataset_digest(plain.cases) == dataset_digest(commented.cases)

    def test_yaml_key_order_does_not_change_the_digest(self, tmp_path: Path) -> None:
        """YAML 对象的键序不承诺稳定；按字面哈希会得到假差异。"""
        reordered = (
            "case_type: cognitive_behavior\n"
            "case_id: case-001\n"
            "schema_version: 1\n"
            "intent: 清单测试用的合成案例\n"
            "category: simple_fact\n"
            "stimulus:\n"
            "  user_context: {}\n"
            "  requested_depth: null\n"
            "  input: 一个合成问题。\n"
            "expectations:\n"
            "  forbidden:\n"
            "    - expected: philosophical\n"
            "      assertion: analysis_module_ran\n"
            "  required:\n"
            "    - expected: completed\n"
            "      assertion: final_state\n"
        )
        plain = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        shuffled = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": reordered}))
        assert dataset_digest(plain.cases) == dataset_digest(shuffled.cases)

    def test_indentation_does_not_change_the_digest(self, tmp_path: Path) -> None:
        wider = (
            _case_text()
            .replace("  input:", "      input:")
            .replace("  user_context:", "      user_context:")
            .replace("  requested_depth:", "      requested_depth:")
        )
        plain = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        spaced = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": wider}))
        assert dataset_digest(plain.cases) == dataset_digest(spaced.cases)

    def test_a_semantic_change_changes_the_digest(self, tmp_path: Path) -> None:
        before = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        after = load_dataset(
            _write_dataset(tmp_path / "b", {"c.yaml": _case_text(user_input="换了一个问题。")})
        )
        assert dataset_digest(before.cases) != dataset_digest(after.cases)

    def test_adding_a_case_changes_the_digest(self, tmp_path: Path) -> None:
        one = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        two = load_dataset(
            _write_dataset(
                tmp_path / "b",
                {"c.yaml": _case_text(), "d.yaml": _case_text(case_id="case-002")},
            )
        )
        assert dataset_digest(one.cases) != dataset_digest(two.cases)

    def test_removing_a_case_changes_the_digest(self, tmp_path: Path) -> None:
        both = load_dataset(
            _write_dataset(
                tmp_path / "a",
                {"c.yaml": _case_text(), "d.yaml": _case_text(case_id="case-002")},
            )
        )
        one = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": _case_text()}))
        assert dataset_digest(both.cases) != dataset_digest(one.cases)

    def test_changing_an_expectation_changes_the_digest(self, tmp_path: Path) -> None:
        """改期望值是**契约**变化，必须被发现。"""
        relaxed = _case_text().replace("expected: philosophical", "expected: causal")
        before = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        after = load_dataset(_write_dataset(tmp_path / "b", {"c.yaml": relaxed}))
        assert dataset_digest(before.cases) != dataset_digest(after.cases)

    def test_file_layout_does_not_change_the_digest(self, tmp_path: Path) -> None:
        """案例放在哪个子目录不是语义——加载顺序按 ``case_id``。"""
        flat = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        nested = load_dataset(_write_dataset(tmp_path / "b", {"deep/er/c.yaml": _case_text()}))
        assert dataset_digest(flat.cases) == dataset_digest(nested.cases)

    def test_the_digest_carries_no_absolute_path(self, tmp_path: Path) -> None:
        """摘要里不能出现机器路径——它会让同一份数据集在两台机器上不同。"""
        dataset = load_dataset(_write_dataset(tmp_path / "a", {"c.yaml": _case_text()}))
        digest = dataset_digest(dataset.cases)
        assert str(tmp_path) not in digest
        assert digest.startswith("sha256:")

    def test_the_case_count_matches_the_loaded_cases(self, tmp_path: Path) -> None:
        dataset = load_dataset(
            _write_dataset(
                tmp_path / "a",
                {"c.yaml": _case_text(), "d.yaml": _case_text(case_id="case-002")},
            )
        )
        assert len(dataset.cases) == 2


class TestAssertionRegistryDigest:
    """B 组：断言摘要覆盖**判定语义**。"""

    def test_the_digest_is_stable(self) -> None:
        assert registry_digest() == registry_digest()

    def test_the_digest_is_not_empty(self) -> None:
        assert registry_digest().startswith("sha256:")

    def test_insertion_order_does_not_matter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """🔴 注册表按名排序后再拼，因此字典的插入顺序不影响摘要。"""
        baseline = registry_digest()
        reversed_registry = {name: ASSERTIONS[name] for name in sorted(ASSERTIONS, reverse=True)}
        monkeypatch.setattr(assertions_module, "ASSERTIONS", reversed_registry)
        assert registry_digest() == baseline

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                lambda spec: replace(spec, semantics_version="999"), id="semantics_version"
            ),
            pytest.param(lambda spec: replace(spec, observed_from="别的地方"), id="observed_from"),
            # ⚠️ 取值必须与 ``final_state`` 的**默认**不同，否则这条用例什么也没改，
            # "摘要没变"就成了必然——那样测的是空气。
            pytest.param(lambda spec: replace(spec, allows_forbidden=False), id="allows_forbidden"),
            pytest.param(lambda spec: replace(spec, allows_required=False), id="allows_required"),
            pytest.param(lambda spec: replace(spec, single_valued=False), id="single_valued"),
            pytest.param(
                lambda spec: replace(spec, expected_kind=ExpectedKind.BOOLEAN),
                id="expected_kind",
            ),
            pytest.param(
                lambda spec: replace(spec, allowed_values=("only-this",)), id="allowed_values"
            ),
            pytest.param(
                lambda spec: replace(spec, applies_to=frozenset({"replay"})), id="applies_to"
            ),
        ],
    )
    def test_a_semantic_field_change_changes_the_digest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[AssertionSpec], AssertionSpec],
    ) -> None:
        """🔴 逐字段验证：**每一个**参与判定的字段都必须在摘要里。

        任何一个漏掉，都会让"改了判定逻辑却显示同一个契约"变成可能。

        ⚠️ 用**变更函数**而不是 ``(字段名, 取值)`` 对：后者要
        ``replace(spec, **{name: value})``，而动态键让 mypy 无法校验，
        只能靠一条 ``type: ignore`` 糊过去——那恰好是本切片要避免的东西。
        """
        baseline = registry_digest()
        monkeypatch.setitem(ASSERTIONS, "final_state", mutate(ASSERTIONS["final_state"]))
        assert registry_digest() != baseline

    def test_a_renamed_assertion_changes_the_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import replace

        baseline = registry_digest()
        spec = ASSERTIONS["final_state"]
        monkeypatch.setitem(ASSERTIONS, "final_state", replace(spec, name="renamed"))
        assert registry_digest() != baseline

    def test_the_digest_carries_no_memory_address(self) -> None:
        """🔴 用函数对象当契约等于说"每次运行的契约都不一样"。"""
        assert "0x" not in registry_digest()

    def test_every_spec_declares_a_semantics_version(self) -> None:
        for name, spec in ASSERTIONS.items():
            assert spec.semantics_version.strip(), name


class TestPromptIdentity:
    """C 组：只记**实际用到**的 Prompt。"""

    def test_only_versions_with_a_task_name_are_kept(self) -> None:
        invocations: list[dict[str, object]] = [
            {"task_name": "judgment_synthesizer", "prompt_version": "1.0.0"},
            {"task_name": "concern_detector", "prompt_version": "1.1.0"},
            {"prompt_version": "9.9.9"},  # 缺 task_name
            {"task_name": "no_version"},  # 缺 prompt_version
        ]
        assert prompt_versions_from_invocations(invocations) == {
            "concern_detector": "1.1.0",
            "judgment_synthesizer": "1.0.0",
        }

    def test_key_order_does_not_change_the_digest(self) -> None:
        first = prompt_identity({"b_task": "1.0.0", "a_task": "2.0.0"})
        second = prompt_identity({"a_task": "2.0.0", "b_task": "1.0.0"})
        assert first.digest == second.digest
        # 键本身也被稳定排序，便于人读
        assert list(first.versions) == ["a_task", "b_task"]

    def test_a_version_change_changes_the_digest(self) -> None:
        before = prompt_identity({"judgment_synthesizer": "1.0.0"})
        after = prompt_identity({"judgment_synthesizer": "1.0.1"})
        assert before.digest != after.digest

    def test_adding_an_actually_used_component_changes_the_digest(self) -> None:
        before = prompt_identity({"judgment_synthesizer": "1.0.0"})
        after = prompt_identity({"judgment_synthesizer": "1.0.0", "metacognition": "1.0.0"})
        assert before.digest != after.digest

    def test_an_unused_component_never_enters_the_list(self) -> None:
        """🔴 清单说的是"这次运行用了什么"，不是"仓库里有什么"。"""
        identity = prompt_identity({"judgment_synthesizer": "1.0.0"})
        assert "philosophical_analyzer" not in identity.versions

    def test_the_digest_carries_no_prompt_body(self) -> None:
        identity = prompt_identity({"judgment_synthesizer": "1.0.0"})
        assert "你把" not in identity.digest
        assert set(identity.versions.values()) == {"1.0.0"}


class TestProviderIdentity:
    """D 组：白名单配置身份。"""

    def test_a_mock_provider_is_recorded_as_deterministic(self) -> None:
        identity = collect_provider_identity(
            provider_name="mock",
            model_id="mock-model-v1",
            parameters={"json_mode": True, "max_retries": 2},
        )
        assert identity.provider_name == "mock"
        assert identity.deterministic is True
        assert identity.network_allowed is False

    def test_a_real_provider_is_not_marked_deterministic(self) -> None:
        identity = collect_provider_identity(
            provider_name="deepseek", model_id="deepseek-chat", parameters={}
        )
        assert identity.deterministic is False
        # ⚠️ 即便描述了一个真实 Provider，也**没有**允许网络——S3 不发请求。
        assert identity.network_allowed is False

    def test_a_behaviour_parameter_changes_the_digest(self) -> None:
        before = collect_provider_identity(
            provider_name="mock", model_id="m", parameters={"max_retries": 2}
        )
        after = collect_provider_identity(
            provider_name="mock", model_id="m", parameters={"max_retries": 3}
        )
        assert before.configuration_digest != after.configuration_digest

    def test_the_model_changes_the_digest(self) -> None:
        before = collect_provider_identity(provider_name="mock", model_id="a", parameters={})
        after = collect_provider_identity(provider_name="mock", model_id="b", parameters={})
        assert before.configuration_digest != after.configuration_digest

    def test_key_order_does_not_change_the_digest(self) -> None:
        before = collect_provider_identity(
            provider_name="mock",
            model_id="m",
            parameters={"max_retries": 2, "json_mode": True},
        )
        after = collect_provider_identity(
            provider_name="mock",
            model_id="m",
            parameters={"json_mode": True, "max_retries": 2},
        )
        assert before.configuration_digest == after.configuration_digest

    def test_a_secret_shaped_field_is_rejected_outright(self) -> None:
        """🔴 **白名单之外一律报错，不静默丢弃。**

        静默丢弃会让摘要看起来"覆盖了那个字段"，而其实没有。
        """
        with pytest.raises(KeyError, match="白名单"):
            collect_provider_identity(
                provider_name="mock",
                model_id="m",
                parameters={"api_key": "sk-should-never-be-here"},
            )

    def test_the_secret_never_reaches_the_serialized_form(self) -> None:
        """即便调用方在别处配了密钥，身份里也不该出现它。"""
        identity = collect_provider_identity(
            provider_name="mock", model_id="m", parameters={"max_retries": 2}
        )
        rendered = dumps(identity.model_dump(mode="json"))
        assert "sk-" not in rendered
        assert "api_key" not in rendered


class TestCodeAndRuntimeIdentity:
    """E 组：Git 与运行时身份。"""

    def test_the_repository_yields_a_full_sha(self) -> None:
        """🔴 必须是**完整** SHA——短 SHA 不足以定位一个代码状态。"""
        identity = collect_code_identity()
        assert identity.commit_sha is not None
        assert len(identity.commit_sha) == 40
        assert all(character in "0123456789abcdef" for character in identity.commit_sha)

    def test_a_directory_without_git_yields_no_sha(self, tmp_path: Path) -> None:
        """🔴 无 ``.git`` 时**如实报不可用**，不编一个 SHA 填进去。"""
        identity = collect_code_identity(tmp_path)
        assert identity.commit_sha is None
        assert identity.working_tree_clean is None

    def test_the_package_version_is_recorded(self) -> None:
        identity = collect_code_identity()
        assert identity.package_version
        assert identity.package_version != "unknown"

    def test_the_runtime_version_has_a_stable_shape(self) -> None:
        identity = collect_runtime_identity()
        parts = identity.python_version.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)
        assert identity.ai_psi_version == collect_code_identity().package_version

    def test_the_canonical_identity_has_no_python_version(self) -> None:
        """🔴 补丁版本是运行环境的属性，不是结果语义的属性。"""
        assert "python_version" not in _manifest().canonical_identity()

    def test_the_canonical_identity_has_no_working_tree_flag(self) -> None:
        """它属于"能不能当基线"的判定，不属于"结果是什么"。"""
        assert "working_tree_clean" not in _manifest().canonical_identity()

    def test_the_canonical_identity_has_no_absolute_path(self) -> None:
        rendered = repr(_manifest().canonical_identity())
        assert ":\\" not in rendered
        assert "/home/" not in rendered


class TestStorageIdentity:
    """F 组：存储身份。"""

    def test_memory_mode_has_no_revision(self) -> None:
        identity = storage_identity(backend="memory", alembic_revision=None)
        assert identity.backend == "memory"
        assert identity.alembic_revision is None

    def test_postgres_mode_records_the_revision(self) -> None:
        identity = storage_identity(backend="postgresql", alembic_revision="b7f1c9d4e2a3")
        assert identity.backend == "postgresql"
        assert identity.alembic_revision == "b7f1c9d4e2a3"

    def test_a_revision_change_changes_the_canonical_identity(self) -> None:
        before = _manifest(storage_backend="postgresql", alembic_revision="rev-a")
        after = _manifest(storage_backend="postgresql", alembic_revision="rev-b")
        assert before.canonical_identity() != after.canonical_identity()

    def test_no_database_name_can_enter_the_identity(self) -> None:
        """🔴 存储身份里只有 backend 与 revision——没有库名、没有 URL。

        评测库的名字每次运行都不同（专用、可丢弃），把它写进身份
        等于说"两次运行的身份永远不同"。
        """
        identity = storage_identity(backend="postgresql", alembic_revision="rev")
        rendered = dumps(identity.model_dump(mode="json"))
        for forbidden in ("ai_psi_eval_test", "postgresql://", "localhost", "password"):
            assert forbidden not in rendered


class TestComparability:
    """G 组：身份契约比较。**返回原因，不只返回布尔。**"""

    def test_identical_manifests_are_comparable(self) -> None:
        result = compare_manifests(_manifest(), _manifest())
        assert result.comparable is True
        assert result.reasons == ()

    @pytest.mark.parametrize(
        ("overrides", "expected_reason"),
        [
            ({"dataset_digest": "sha256:other"}, "dataset_differs"),
            ({"assertion_registry_digest": "sha256:other"}, "assertion_registry_differs"),
            ({"prompt_digest": "sha256:other"}, "prompt_versions_differ"),
            ({"provider_name": "deepseek"}, "provider_differs"),
            ({"model_id": "other-model"}, "model_differs"),
            ({"configuration_digest": "sha256:other"}, "provider_configuration_differs"),
            ({"execution_mode": "postgres_http"}, "execution_mode_differs"),
            ({"storage_backend": "postgresql"}, "storage_backend_differs"),
            ({"alembic_revision": "other-rev"}, "migration_revision_differs"),
            ({"commit_sha": "b" * 40}, "code_revision_differs"),
            ({"working_tree_clean": False}, "dirty_worktree"),
            ({"commit_sha": None}, "identity_unavailable"),
        ],
    )
    def test_each_difference_yields_its_own_reason(
        self, overrides: dict[str, Any], expected_reason: str
    ) -> None:
        result = compare_manifests(_manifest(), _manifest(**overrides))
        assert result.comparable is False
        assert expected_reason in result.reasons

    def test_manifest_schema_difference_is_reported(self) -> None:
        result = compare_manifests(_manifest(), _manifest(manifest_schema_version=99))
        assert "manifest_schema_differs" in result.reasons

    def test_case_schema_difference_is_reported(self) -> None:
        result = compare_manifests(_manifest(), _manifest(case_schema_version=99))
        assert "manifest_schema_differs" in result.reasons

    def test_missing_identity_is_never_comparable(self) -> None:
        """🔴 身份根本没有采集到时，**不得**判成可比较。"""
        assert compare_manifests(None, _manifest()).comparable is False
        assert compare_manifests(_manifest(), None).comparable is False
        assert compare_manifests(None, None).reasons == ("identity_unavailable",)

    def test_an_unknown_sha_is_never_comparable(self) -> None:
        """🔴 两个都取不到 SHA，也**不是**"一致"——那是两个未知。"""
        result = compare_manifests(_manifest(commit_sha=None), _manifest(commit_sha=None))
        assert result.comparable is False
        assert "identity_unavailable" in result.reasons

    def test_a_dirty_tree_is_never_comparable(self) -> None:
        result = compare_manifests(_manifest(working_tree_clean=False), _manifest())
        assert result.comparable is False
        assert "dirty_worktree" in result.reasons

    def test_reasons_is_a_list_not_just_a_boolean(self) -> None:
        """🔴 一个 ``False`` 没法告诉调用方该去查哪一项。"""
        result = compare_manifests(
            _manifest(), _manifest(dataset_digest="x", model_id="y", execution_mode="z")
        )
        assert len(result.reasons) >= 3
        assert set(result.reasons) >= {"dataset_differs", "model_differs", "execution_mode_differs"}

    def test_a_clean_pair_from_the_same_commit_is_comparable(self) -> None:
        """同一提交、同一契约 → 可比较（这是最常见的正确场景）。"""
        result = compare_manifests(_manifest(commit_sha="c" * 40), _manifest(commit_sha="c" * 40))
        assert result.comparable is True


class TestDeterminism:
    """H 组：清单自身的确定性。"""

    def test_two_serializations_are_byte_identical(self) -> None:
        manifest = _manifest()
        assert dumps(manifest.model_dump(mode="json")) == dumps(manifest.model_dump(mode="json"))

    def test_the_canonical_identity_is_byte_identical_across_instances(self) -> None:
        assert dumps(_manifest().canonical_identity()) == dumps(_manifest().canonical_identity())

    def test_the_manifest_rejects_unknown_fields(self) -> None:
        """🔴 严格模型：多一个没人读的字段是"这份清单说了什么"的常见答案。"""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            ReproducibilityManifest.model_validate(
                {**_manifest().model_dump(mode="json"), "surprise": 1}
            )

    def test_the_runtime_identity_is_not_in_the_canonical_subset(self) -> None:
        canonical = dumps(_manifest(python_version="3.12.0").canonical_identity())
        assert "3.12.0" not in canonical
