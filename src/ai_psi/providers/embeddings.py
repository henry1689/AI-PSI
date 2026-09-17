"""向量（embedding）Provider（阶段 5，任务书 §10）。

长期记忆的检索靠向量相似度。本模块定义 :class:`EmbeddingProvider` 协议
与两个实现：

========================================  ==========================================
实现                                       性质
========================================  ==========================================
:class:`LocalHashingEmbedding`              **确定性词面向量**，零依赖、零成本、离线
:class:`OpenAICompatibleEmbedding`          OpenAI 兼容的 ``/embeddings`` 接口
========================================  ==========================================

🔴 **本地实现不是"语义"向量，这一点必须说清楚。**

它把文本切成**字符二元组与拉丁词**，用带符号的哈希把它们映射到固定维度
（feature hashing，特征哈希），再做 L2 归一化。余弦相似度因此近似于
**词面重合度**——「喜欢简洁」与「讨厌啰嗦」在这个空间里**几乎正交**。

那为什么还要它：它让 pgvector 的**全部真实机制**（固定维度、索引、
删除传播、版本兼容）在没有外部服务的情况下就能跑通并被测试钉死；
而且它完全确定，测试里不需要任何 mock。

需要真语义检索时换成 :class:`OpenAICompatibleEmbedding` 即可——
换的是一个实现类与两行配置，检索路径一行都不用动。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

import httpx
from pydantic import SecretStr

from ai_psi.domain.exceptions import ProviderError
from ai_psi.providers.http import map_http_error
from ai_psi.providers.response import TokenUsage

__all__ = [
    "AVAILABLE_EMBEDDING_PROVIDERS",
    "DEFAULT_EMBEDDING_DIMENSION",
    "EmbeddingBatch",
    "EmbeddingProvider",
    "LocalHashingEmbedding",
    "OpenAICompatibleEmbedding",
    "normalize_vector",
]

#: 可用的向量 Provider 名称。
#:
#: 🔴 **单一来源。** 配置层的校验器与装配工厂都从这里取——
#: 两处各写一份名单，迟早会各自漂移，而漂移的表现是
#: "配置说合法、装配说未知"（或反过来），排查时两头都对。
AVAILABLE_EMBEDDING_PROVIDERS: Final[frozenset[str]] = frozenset({"local", "openai_compatible"})

#: 本地哈希向量的默认维度。
#:
#: 维度的取舍是**哈希碰撞**与**存储/索引成本**之间的权衡：
#: 维度越低，不同的 n-gram 越容易落到同一个桶里，于是不相关的文本
#: 也会得到一个虚高的相似度（假阳性）；维度越高，每行向量越大，
#: HNSW 索引越占内存。512 在"一个用户的记忆条数在千级别"这个规模下
#: 碰撞可忽略，同时一行只有 2KB。
#:
#: ⚠️ **它是数据库列的固定维度**，改了必须同时改迁移。
#: 装配时若 Provider 声明的维度与配置不一致，会**明确失败**而不是静默错位。
DEFAULT_EMBEDDING_DIMENSION: Final[int] = 512

#: 拉丁词与数字的切分规则。中文没有词边界，走字符 n-gram；
#: 拉丁文本按空白与标点切词比二元组更准（"belief" 的二元组会
#: 与 "be"、"li" 之类的噪声混在一起）。
_LATIN_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

#: 字符 n-gram 的长度。取 2 与 :mod:`ai_psi.reliability.repetition_detector`
#: 保持一致——两处对"文本像不像"的判断标准不该互相矛盾。
_NGRAM_SIZE: Final[int] = 2


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """一次向量化调用的结果。

    Attributes:
        vectors: 与输入**等长且同序**的向量列表。
        usage: 供应商报告的用量；本地实现为空。
    """

    vectors: list[list[float]]
    usage: TokenUsage = field(default_factory=TokenUsage)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本向量化。

    🔴 **三个属性必须稳定**：

    * :attr:`version` 会写进 ``Memory.embedding_version``，检索时只比对
      **同版本**的向量——换模型后旧向量自动失效，而不是被拿去和一个
      语义空间已经不同的查询向量比较（那会得到看似合理、实则无意义的结果）；
    * :attr:`dimension` 必须是**固定值**，且与数据库列一致；
    * :attr:`name` 用于诊断输出。
    """

    @property
    def name(self) -> str:
        """Provider 名称，用于诊断。"""
        ...

    @property
    def dimension(self) -> int:
        """向量维度。"""
        ...

    @property
    def version(self) -> str:
        """向量空间的版本标识，写入 ``embedding_version``。"""
        ...

    async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
        """把一批文本向量化。

        Args:
            texts: 待向量化的文本，**顺序即返回顺序**。

        Returns:
            与 ``texts`` 等长同序的结果。

        Raises:
            ProviderError: 调用失败或返回的向量不符合协议。
        """
        ...


# ---------------------------------------------------------------------------
# 本地确定性实现
# ---------------------------------------------------------------------------


class LocalHashingEmbedding:
    """确定性的词面哈希向量（feature hashing）。

    算法：

    1. 归一化（去空白、转小写）；
    2. 切 token：字符二元组 + 拉丁词/数字；
    3. 每个 token 用 **blake2b** 哈希到 ``[0, dimension)`` 中的一格，
       并由哈希的最高位决定贡献的符号——**带符号**是关键：
       两个不同 token 撞进同一格时，它们的贡献会互相抵消而不是叠加，
       于是估计出的内积是无偏的（这是 feature hashing 的标准做法，
       不带符号的版本会让碰撞单调地抬高相似度）；
    4. 累加词频后做 L2 归一化，于是余弦相似度就是内积。

    🔴 **为什么不用内置的 ``hash()``。**

    ``hash("文本")`` 对字符串**每个进程都不一样**——CPython 用
    ``PYTHONHASHSEED`` 做哈希随机化以防碰撞攻击。用它算向量意味着：
    同一个进程内计算的向量彼此可比，**跨进程就完全不可比**。
    写入数据库的向量是上一个进程算的，重启后算出的查询向量落在
    另一套随机桶里，检索会静默地全部失配——不报错，只是永远查不到。

    blake2b 是确定的，同样的输入在任何进程、任何机器上得到同样的向量。

    Note:
        :attr:`version` 里带着维度。维度不同时向量之间不可比，
        而"版本不同就不参与检索"这条规则正好把这种情况挡住了。
    """

    def __init__(self, *, dimension: int = DEFAULT_EMBEDDING_DIMENSION) -> None:
        """初始化。

        Args:
            dimension: 向量维度，必须为正。
        """
        if dimension <= 0:
            msg = f"向量维度必须为正，收到 {dimension}"
            raise ValueError(msg)
        self._dimension = dimension

    @property
    def name(self) -> str:
        """Provider 名称。"""
        return "local_hashing"

    @property
    def dimension(self) -> int:
        """向量维度。"""
        return self._dimension

    @property
    def version(self) -> str:
        """向量空间版本。"""
        return f"local-hashing-v1-d{self._dimension}"

    async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
        """把一批文本向量化。

        Args:
            texts: 待向量化的文本。

        Returns:
            与输入等长同序的向量。**空文本得到全零向量**——
            调用方必须把全零向量当作"无匹配信号"处理（见
            :func:`ai_psi.memory.retrieval.rank_candidates`）。
        """
        return EmbeddingBatch(vectors=[self.encode(text) for text in texts])

    def encode(self, text: str) -> list[float]:
        """把单条文本编码为向量。

        同步方法——本地实现不涉及 IO，检索路径里需要它做查询编码时
        不必进事件循环。

        Args:
            text: 原始文本。

        Returns:
            长度为 :attr:`dimension` 的 L2 归一化向量。
        """
        dimension = self._dimension
        accumulator = [0.0] * dimension
        for token, count in _token_counts(text).items():
            index, sign = _hash_token(token, dimension)
            accumulator[index] += sign * count
        return normalize_vector(accumulator)


def _token_counts(text: str) -> Counter[str]:
    """把文本切分为 token → 词频。

    token 带前缀区分来源（``bg:`` / ``w:``），避免"字符二元组"与
    "拉丁词"恰好同形时被当成同一个特征。

    Args:
        text: 原始文本。

    Returns:
        token 到出现次数的映射。
    """
    normalized = "".join(char for char in text if not char.isspace()).lower()
    counts: Counter[str] = Counter()
    if not normalized:
        return counts

    if len(normalized) <= _NGRAM_SIZE:
        # 极短文本没有二元组，退化成它自己——否则短查询会得到全零向量
        counts[f"bg:{normalized}"] += 1
    else:
        for index in range(len(normalized) - _NGRAM_SIZE + 1):
            counts[f"bg:{normalized[index : index + _NGRAM_SIZE]}"] += 1

    for word in _LATIN_WORD.findall(normalized):
        counts[f"w:{word}"] += 1
    return counts


def _hash_token(token: str, dimension: int) -> tuple[int, float]:
    """把 token 映射到 ``(桶下标, 符号)``。

    取哈希的低位定桶、**最高位定符号**。两者取自不同的位，
    因此符号与桶位置相互独立——否则"落在奇数桶的 token 恒为正"
    这类系统性偏差会让相似度整体偏高。

    Args:
        token: 带前缀的 token。
        dimension: 向量维度。

    Returns:
        ``(下标, +1.0 或 -1.0)``。
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return value % dimension, sign


def normalize_vector(vector: list[float]) -> list[float]:
    """L2 归一化。

    Args:
        vector: 原始向量。

    Returns:
        归一化后的向量。**零向量的归一化结果是它自己**——
        除以 0 会得到 NaN 或抛错，而 NaN 一旦进到数据库，
    后续每一次比较都会得到 NaN，排序列会彻底失去意义。
    """
    norm = sum(component * component for component in vector) ** 0.5
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]


# ---------------------------------------------------------------------------
# OpenAI 兼容实现
# ---------------------------------------------------------------------------


class OpenAICompatibleEmbedding:
    """OpenAI 兼容的 ``POST {base_url}/embeddings``。

    与 :class:`~ai_psi.providers.openai_compatible.OpenAICompatibleProvider`
    共用一套 HTTP 错误映射，因此超时、限流、不可用的语义完全一致
    （任务书 §13.2 的降级判定依赖这套分类）。

    ⚠️ **本实现未经真实成功响应的验证。** 开发机上的中转站
    （``api.agnes-ai.cn``）虽有 ``/v1/embeddings`` 端点，但账户分组下
    没有任何 embedding 渠道，任何模型都返回 ``model_not_found``。
    因此它的行为由 ``httpx.MockTransport`` 覆盖，
    而不是由一次真实调用覆盖——这一点记录在 ADR-0017 里。
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        base_url: str,
        model: str,
        dimension: int,
        name: str = "openai_compatible_embeddings",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """初始化。

        Args:
            client: 共享的 HTTP 客户端（生命周期由容器管理）。
            api_key: 供应商密钥。
            base_url: 形如 ``https://api.example.com/v1`` 的基础地址。
            model: 向量模型名，如 ``text-embedding-3-small``。
            dimension: **期望**的向量维度。返回的向量与它不符时直接失败——
                维度错位会让检索静默地永远失配，而数据库列是定长的，
                错位的向量根本存不进去。
            name: Provider 名称。
            extra_headers: 额外的请求头。
        """
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimension = dimension
        self._name = name
        self._extra_headers = dict(extra_headers or {})

    @property
    def name(self) -> str:
        """Provider 名称。"""
        return self._name

    @property
    def dimension(self) -> int:
        """向量维度。"""
        return self._dimension

    @property
    def version(self) -> str:
        """向量空间版本。

        带维度而不是只带模型名：同一个模型在不同供应商那里可能给出
        不同维度，而**维度不同就是不同的向量空间**。
        """
        return f"{self._name}:{self._model}:d{self._dimension}"

    @property
    def model(self) -> str:
        """向量模型名（可安全记录）。"""
        return self._model

    async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
        """把一批文本向量化。

        Args:
            texts: 待向量化的文本。

        Returns:
            与输入等长同序的结果。

        Raises:
            ProviderTimeoutError: 超时。
            ProviderRateLimitError: 触发速率限制。
            ProviderUnavailableError: 供应商不可用。
            ProviderError: 其他失败，或返回结构不符合协议。
        """
        if not texts:
            return EmbeddingBatch(vectors=[])

        payload: dict[str, object] = {
            "model": self._model,
            "input": texts,
            "encoding_format": "float",
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                    **self._extra_headers,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise map_http_error(
                exc, provider=self._name, model=self._model, task_name="embeddings"
            ) from exc

        return self._parse(response.json(), text_count=len(texts))

    def _parse(self, body: object, *, text_count: int) -> EmbeddingBatch:
        """解析响应。

        Raises:
            ProviderError: 响应结构不符合协议、条数不符，或维度与声明不符。
        """
        if not isinstance(body, dict):
            msg = "向量接口返回的不是 JSON 对象"
            raise ProviderError(msg, code="invalid_response", provider=self._name)

        data = body.get("data")
        if not isinstance(data, list) or len(data) != text_count:
            msg = (
                f"向量接口返回的条数与输入不一致：期望 {text_count} 条，"
                f"实际 {len(data) if isinstance(data, list) else '非列表'}"
            )
            raise ProviderError(
                msg,
                code="invalid_response",
                provider=self._name,
                context={"expected": text_count},
            )

        # 🔴 **按 index 归位，不依赖返回顺序。**
        # 协议要求按 index 排序，但"协议要求"与"实现照做"是两回事；
        # 顺序错位会让文本与向量静默地配错，而且完全看不出来。
        ordered: list[list[float] | None] = [None] * text_count
        for entry in data:
            if not isinstance(entry, dict):
                msg = "向量接口返回的 data 元素不是对象"
                raise ProviderError(msg, code="invalid_response", provider=self._name)
            index = entry.get("index")
            vector = entry.get("embedding")
            if not isinstance(index, int) or not 0 <= index < text_count:
                msg = f"向量接口返回的 index 越界：{index!r}"
                raise ProviderError(msg, code="invalid_response", provider=self._name)
            if not isinstance(vector, list) or not vector:
                msg = f"向量接口在 index={index} 处没有返回向量"
                raise ProviderError(msg, code="invalid_response", provider=self._name)
            ordered[index] = [float(component) for component in vector]

        vectors: list[list[float]] = []
        for index, vector in enumerate(ordered):
            if vector is None:
                msg = f"向量接口没有返回 index={index} 的向量"
                raise ProviderError(msg, code="invalid_response", provider=self._name)
            if len(vector) != self._dimension:
                msg = (
                    f"向量维度与配置不符：模型 {self._model} 返回 {len(vector)} 维，"
                    f"而配置声明 {self._dimension} 维。"
                    "维度是数据库列的固定属性，改维度必须同时改迁移"
                    "（AI_PSI_EMBEDDING_DIMENSION）"
                )
                raise ProviderError(msg, code="dimension_mismatch", provider=self._name)
            vectors.append(vector)

        usage_payload = body.get("usage")
        usage = TokenUsage()
        if isinstance(usage_payload, dict):
            prompt_tokens = usage_payload.get("prompt_tokens")
            total_tokens = usage_payload.get("total_tokens")
            usage = TokenUsage(
                input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                # 向量接口没有"输出 token"的概念，总用量即输入用量
                output_tokens=0 if isinstance(total_tokens, int) else None,
            )
        return EmbeddingBatch(vectors=vectors, usage=usage)
