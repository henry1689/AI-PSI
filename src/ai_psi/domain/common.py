"""领域对象通用基类与共享值类型。

本模块是全部领域对象的依赖根，**必须保持零项目内依赖**
（架构规则 1：`domain/` 只依赖 pydantic 与标准库）。

核心内容：

* :data:`UtcDatetime` / :data:`UtcDatetimeOptional` —— 强制 UTC 且 tz-aware 的时间类型；
* :class:`EntityMetadata` —— 除 ``Event`` 外所有领域对象的基类（ADR-0006）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Final, Self
from uuid import UUID, uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "INVISIBLE_CHARACTERS",
    "SCHEMA_VERSION_V1",
    "EntityMetadata",
    "UtcDatetime",
    "UtcDatetimeOptional",
    "is_blank",
    "utc_now",
]

#: 领域对象结构的初始版本号。结构发生不兼容变更时递增。
SCHEMA_VERSION_V1 = "1.0.0"

#: 会被当作"空白"处理的**不可见格式字符**（Unicode 类别 ``Cf``）。
#:
#: 🔴 **``str.strip()`` 不认识它们。**
#:
#: ``"​".isspace()`` 是 ``False``——零宽空格在 Unicode 里
#: 属于格式字符（``Cf``），不属于空白（``White_Space``）。
#: 于是一个只由零宽字符组成的字符串，长度不为 0、``strip()`` 之后
#: 也不为空，能一路穿过 ``min_length=1``，被当成一条**有内容的**记录
#: 落进只追加的事件流。它看起来是空的，占着位置，而且删不掉。
#:
#: ⚠️ **这不是"更严格"，是"同一件事的两种写法"。**
#: 用户按下零宽空格与按下普通空格，表达的意图完全一样：
#: "我没输入内容"。把它们区别对待，是让用户去猜实现用了哪个判据。
#:
#: ⚠️ 刻意**不含**方向控制符（``U+202A``–``U+202E``）与
#: 变体选择符（``U+FE00``–``U+FE0F``）：前者出现在正文里是
#: 一种真实（虽然可疑）的表达，后者是 emoji 的一部分。
#: 把它们一起抹掉会改变**有内容**的输入。
INVISIBLE_CHARACTERS: Final[frozenset[str]] = frozenset(
    # ⚠️ 用**码点**而不是字面量：把不可见字符直接写进源码，
    # 会让这个集合本身变成一段看不见的代码——评审时无从检查，
    # 编辑时容易连同注释一起被误删。码点写法是可读、可搜索、可核对的。
    chr(code_point)
    for code_point in (
        0x00AD,  # 软连字符 SOFT HYPHEN
        0x200B,  # 零宽空格 ZERO WIDTH SPACE
        0x200C,  # 零宽非连接符 ZERO WIDTH NON-JOINER
        0x200D,  # 零宽连接符 ZERO WIDTH JOINER
        0x2060,  # 词连接符 WORD JOINER
        0xFEFF,  # 零宽不换行空格 ZERO WIDTH NO-BREAK SPACE（BOM）
    )
)


def is_blank(text: str) -> bool:
    """该文本是否**只说了一件事：什么都没说**。

    🔴 判据是"去掉空白与不可见格式字符之后还剩下什么"，
    而不是"``len()`` 是不是 0"。

    ``min_length=1`` 挡得住空串，挡不住 ``"\\u200b"``——
    而后者在事件流里占着一条记录、看起来是空的、且无法被检索命中。

    Args:
        text: 待判定的文本。

    Returns:
        去掉 Unicode 空白与 :data:`INVISIBLE_CHARACTERS` 之后为空时返回 ``True``。
    """
    return not text.strip("".join(INVISIBLE_CHARACTERS)).strip()


def utc_now() -> datetime:
    """返回当前 UTC 时间（tz-aware）。

    统一使用本函数而不是 ``datetime.utcnow()``——后者返回 naive datetime，
    正是本项目要杜绝的（见 :func:`_ensure_utc`）。
    """
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    """校验并归一化到 UTC。

    🔴 **拒绝 naive datetime**：没有时区信息的时间在跨时区比较时会静默出错，
    而且无法判断它原本是本地时间还是 UTC。宁可构造时失败，也不要存储歧义数据。
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = (
            "领域对象的时间必须带时区（禁止 naive datetime）。"
            "请使用 ai_psi.domain.common.utc_now() 构造。"
        )
        raise ValueError(msg)
    return value.astimezone(UTC)


#: 强制 UTC 的时间类型。非 UTC 的 tz-aware 时间会被**转换**为 UTC，naive 时间被拒绝。
UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]

#: 可选的强制 UTC 时间类型。
UtcDatetimeOptional = Annotated[
    datetime | None, AfterValidator(lambda v: None if v is None else _ensure_utc(v))
]


class EntityMetadata(BaseModel):
    """除 ``Event`` 外所有领域对象的基类（ADR-0006）。

    提供任务书 §5.1 要求的六个通用字段。子类只声明自己的业务字段。

    🔴 **关于 ``extra="forbid"``：**
    未知字段一律拒绝，不静默接受。这与开发原则第 16 条
    （"任何模型返回值都视为不可信输入"）一致——
    模型多返回的字段会立刻暴露为校验错误，而不是悄悄进入系统。

    🔴 **关于 ``validate_assignment=True``：**
    赋值时重新校验。这样"设了状态却忘了设停止原因"这类错误
    会在**赋值的那一刻**失败，而不是等到持久化时。
    不变量 19（完成回合必须有停止原因）正是靠这个机制强制。
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    id: UUID = Field(default_factory=uuid4, description="对象唯一标识")
    created_at: UtcDatetime = Field(default_factory=utc_now, description="创建时间（UTC）")
    updated_at: UtcDatetime = Field(default_factory=utc_now, description="最后更新时间（UTC）")
    version: int = Field(default=1, ge=1, description="乐观锁版本号，每次持久化更新递增")
    created_by: str = Field(min_length=1, description="产生该对象的组件、模型或用户标识")
    schema_version: str = Field(default=SCHEMA_VERSION_V1, min_length=1, description="对象结构版本")

    @model_validator(mode="after")
    def _check_time_order(self) -> Self:
        """``updated_at`` 不得早于 ``created_at``。"""
        if self.updated_at < self.created_at:
            msg = (
                f"updated_at ({self.updated_at.isoformat()}) 不得早于 "
                f"created_at ({self.created_at.isoformat()})"
            )
            raise ValueError(msg)
        return self

    def bumped(self, **updates: Any) -> Self:
        """返回应用了 ``updates`` 的新实例，并递增版本号、刷新 ``updated_at``。

        用于乐观锁更新路径：**禁止就地覆盖旧版本**（ADR-0002）。
        调用方应把原始 ``version`` 一并交给仓储层做条件更新，
        版本不匹配时仓储层抛 :class:`~ai_psi.domain.exceptions.OptimisticLockError`。

        Args:
            **updates: 要更新的业务字段。

        Returns:
            新实例，``version`` 已递增，``updated_at`` 已刷新。
        """
        payload = self.model_dump()
        payload.update(updates)
        payload["version"] = self.version + 1
        payload["updated_at"] = utc_now()
        return type(self).model_validate(payload)
