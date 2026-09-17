"""Prompt 契约与注册表（任务书 §8.3）。

契约强制的内容：

* **任务名全局唯一**，与 :attr:`ModelInvocationInfo.task_name` 一致（不变量 18）；
* **语义版本号**，且 changelog 中必须有对应条目——改版本不写原因 = 无法追溯；
* **输入/输出 Schema**、**最大长度**、**测试样例**；
* ``rollout`` 字段存在但 V0.1 恒为 ``1.0``，**不自动灰度**（ADR-0005）。

模板文件按版本存放（``templates/<task>/<version>.md``），
**旧版本不删除**——历史回合的回放需要按原版本重放。

🔴 **模板里只允许一个占位符 ``{{input}}``。**
任何其他 ``{{...}}`` 都在注册表构造时报错。这不是洁癖：
多一个占位符就多一个"内容能出现在指令区"的入口，
而任务书 §17.2 要求内容只能作为数据进入。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_psi.domain.exceptions import ConfigurationError, DomainError
from ai_psi.prompts.payload import render_payload_block
from ai_psi.providers.base import LLMMessage, ModelConfig

__all__ = [
    "ChangelogEntry",
    "PromptContract",
    "PromptExample",
    "PromptRegistry",
    "estimate_tokens",
]

_SEMVER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")
_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SYSTEM_MARKER: Final[str] = "<!-- system -->"
_USER_MARKER: Final[str] = "<!-- user -->"

#: 输入长度的粗略估算：按 4 字符 ≈ 1 token。
#:
#: 🔴 这**不是**精确分词。它只用于在"提示词明显超长"时提前失败，
#: 而不是等到供应商返回错误。真正的成本核算靠 Provider 返回的 token 计数。
_CHARS_PER_TOKEN: Final[int] = 4


def estimate_tokens(text: str) -> int:
    """粗估文本 token 数。

    Args:
        text: 待估算文本。

    Returns:
        估算的 token 数，至少为 1。
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


class PromptExample(BaseModel):
    """一个测试样例，用于契约完整性与渲染测试。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class ChangelogEntry(BaseModel):
    """一条版本变更记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str = Field(min_length=1)
    change: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_version_format(self) -> ChangelogEntry:
        if not _SEMVER_PATTERN.match(self.version):
            msg = f"changelog 版本号必须形如 1.0.0，收到 {self.version!r}"
            raise ValueError(msg)
        return self


class PromptContract(BaseModel):
    """一次模型调用的完整契约（`docs/prompt_contracts.md` §2）。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
    )

    task_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""

    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = Field(
        default=None,
        description="结构化任务的输出 Schema；纯文本任务（渲染器）为 None",
    )

    max_input_tokens: int = Field(default=8000, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)

    examples: tuple[PromptExample, ...] = ()
    changelog: tuple[ChangelogEntry, ...] = ()

    rollout: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="灰度比例。V0.1 **恒为 1.0**，不自动灰度（ADR-0005）",
    )

    @model_validator(mode="after")
    def _check_version_format(self) -> PromptContract:
        if not _SEMVER_PATTERN.match(self.version):
            msg = f"Prompt 版本号必须形如 1.0.0，收到 {self.version!r}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_changelog_covers_version(self) -> PromptContract:
        """版本号变化必须伴随 changelog 条目，否则改动无从追溯。"""
        versions = {entry.version for entry in self.changelog}
        if self.version not in versions:
            msg = (
                f"Prompt {self.task_name!r} 的版本 {self.version} 在 changelog 中没有条目。"
                "改动提示词却不记录原因，等同于不可回滚的静默变更"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_rollout_is_pinned(self) -> PromptContract:
        """V0.1 不做灰度：rollout 必须恰好是 1.0。"""
        if self.rollout != 1.0:
            msg = f"V0.1 不支持灰度发布，rollout 必须为 1.0，收到 {self.rollout}（ADR-0005）"
            raise ValueError(msg)
        return self

    @property
    def is_text_task(self) -> bool:
        """是否为纯文本任务（无输出 Schema）。"""
        return self.output_model is None

    def template_relative_path(self) -> str:
        """模板在 ``templates/`` 下的相对路径。"""
        return f"{self.task_name}/{self.version}.md"


class PromptRegistry:
    """Prompt 契约的注册表与渲染器。"""

    def __init__(
        self,
        contracts: Iterable[PromptContract],
        *,
        template_root: Path | None = None,
    ) -> None:
        """初始化。

        Args:
            contracts: 全部契约。任务名必须唯一。
            template_root: 模板根目录；``None`` 表示使用包内资源。
                测试可指向临时目录，无需改动包内容。

        Raises:
            ConfigurationError: 任务名重复。
        """
        self._contracts: dict[str, PromptContract] = {}
        for contract in contracts:
            if contract.task_name in self._contracts:
                msg = f"Prompt 任务名重复：{contract.task_name!r}（任务名必须全局唯一）"
                raise ConfigurationError(msg)
            self._contracts[contract.task_name] = contract
        self._template_root = template_root
        self._template_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def contracts(self) -> Mapping[str, PromptContract]:
        """全部契约，按任务名索引。"""
        return dict(self._contracts)

    def task_names(self) -> tuple[str, ...]:
        """全部任务名（按字典序）。"""
        return tuple(sorted(self._contracts))

    def get(self, task_name: str) -> PromptContract:
        """按任务名取契约。

        Raises:
            ConfigurationError: 任务名未注册。
        """
        try:
            return self._contracts[task_name]
        except KeyError as exc:
            msg = f"未注册的 Prompt 任务名：{task_name!r}。已注册：{sorted(self._contracts)}"
            raise ConfigurationError(msg) from exc

    def has(self, task_name: str) -> bool:
        """任务名是否已注册。"""
        return task_name in self._contracts

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------

    def template_body(self, task_name: str) -> str:
        """读取并缓存模板正文。

        Raises:
            ConfigurationError: 模板文件不存在或结构非法。
        """
        if task_name in self._template_cache:
            return self._template_cache[task_name]

        contract = self.get(task_name)
        relative = contract.template_relative_path()
        if self._template_root is not None:
            path = self._template_root / relative
            if not path.is_file():
                msg = f"Prompt 模板不存在：{path}"
                raise ConfigurationError(msg)
            body = path.read_text(encoding="utf-8")
        else:
            resource = resources.files("ai_psi.prompts").joinpath("templates").joinpath(relative)
            if not resource.is_file():
                msg = f"Prompt 模板不存在：templates/{relative}"
                raise ConfigurationError(msg)
            body = resource.read_text(encoding="utf-8")

        self._validate_template(task_name, body)
        self._template_cache[task_name] = body
        return body

    @staticmethod
    def _validate_template(task_name: str, body: str) -> None:
        """校验模板结构。

        Raises:
            ConfigurationError: 缺少区段标记，或出现 ``{{input}}`` 以外的占位符。
        """
        if _SYSTEM_MARKER not in body or _USER_MARKER not in body:
            msg = f"Prompt 模板 {task_name!r} 必须同时包含 {_SYSTEM_MARKER} 与 {_USER_MARKER} 标记"
            raise ConfigurationError(msg)
        if body.index(_SYSTEM_MARKER) > body.index(_USER_MARKER):
            msg = f"Prompt 模板 {task_name!r} 的 {_SYSTEM_MARKER} 必须位于 {_USER_MARKER} 之前"
            raise ConfigurationError(msg)

        placeholders = set(_PLACEHOLDER_PATTERN.findall(body))
        unknown = placeholders - {"input"}
        if unknown:
            msg = (
                f"Prompt 模板 {task_name!r} 含未支持的占位符 {sorted(unknown)}。"
                "模板只允许 {{input}}——多一个占位符就多一个内容进入指令区的入口（§17.2）"
            )
            raise ConfigurationError(msg)
        if "input" not in placeholders:
            msg = f"Prompt 模板 {task_name!r} 必须包含 {{input}} 占位符"
            raise ConfigurationError(msg)

    def _split_template(self, task_name: str) -> tuple[str, str]:
        """把模板切成系统段与用户段。"""
        body = self.template_body(task_name)
        system_part, _, user_part = body.partition(_USER_MARKER)
        system_part = system_part.replace(_SYSTEM_MARKER, "")
        return system_part.strip(), user_part.strip()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, task_name: str, payload: BaseModel | dict[str, Any]) -> list[LLMMessage]:
        """把输入渲染为消息列表。

        Args:
            task_name: 任务名。
            payload: 输入对象或字典，**作为数据**进入数据块。

        Returns:
            系统消息 + 用户消息。

        Raises:
            ConfigurationError: 模板非法或任务未注册。
            DomainError: 渲染后的输入估算长度超过契约的 ``max_input_tokens``。
        """
        contract = self.get(task_name)
        system_part, user_part = self._split_template(task_name)
        block = render_payload_block(payload)
        user_content = user_part.replace("{{input}}", block)
        system_content = system_part.replace("{{input}}", block)

        rendered = [
            LLMMessage(role="system", content=system_content),
            LLMMessage(role="user", content=user_content),
        ]

        estimated = sum(estimate_tokens(message.content) for message in rendered)
        if estimated > contract.max_input_tokens:
            msg = (
                f"Prompt {task_name!r} 的估算输入长度 {estimated} 超过契约上限 "
                f"{contract.max_input_tokens}。这是上下文选择的问题，"
                "应当在 ContextBuilder 阶段裁剪，而不是把更长的提示词发给模型"
            )
            raise DomainError(msg, context={"task_name": task_name, "estimated_tokens": estimated})

        return rendered

    def model_config(
        self,
        task_name: str,
        *,
        model: str,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> ModelConfig:
        """按契约生成模型参数。

        Args:
            task_name: 任务名。
            model: 模型标识。
            temperature: 采样温度。
            timeout_seconds: 超时。

        Returns:
            该任务的模型参数（``max_output_tokens`` 取自契约）。
        """
        contract = self.get(task_name)
        return ModelConfig(
            model=model,
            temperature=temperature,
            max_output_tokens=contract.max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
