"""向量 Provider（任务书 §10）。

🔴 这个文件里最重要的一条是 :meth:`TestLocalDeterminism.test_across_processes`
——它验证的是"同一段文本在不同进程里必须得到同一个向量"。
那是**跨进程可比性**，也是本实现不能用内置 ``hash()`` 的全部理由。
"""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
from pydantic import SecretStr

from ai_psi.domain.exceptions import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ai_psi.memory.retrieval import cosine_similarity
from ai_psi.providers.embeddings import (
    DEFAULT_EMBEDDING_DIMENSION,
    LocalHashingEmbedding,
    OpenAICompatibleEmbedding,
    normalize_vector,
)
from ai_psi.providers.response import TokenUsage

pytestmark = pytest.mark.unit


class TestLocalHashingEmbedding:
    """本地确定性向量。"""

    def test_dimension_and_version_are_stable(self) -> None:
        provider = LocalHashingEmbedding(dimension=128)
        assert provider.dimension == 128
        assert provider.name == "local_hashing"
        # 版本里带维度：维度不同的向量属于不同的空间，不能互相比较
        assert provider.version == "local-hashing-v1-d128"

    def test_default_dimension_matches_the_column(self) -> None:
        """默认维度必须与数据库列一致。

        它写死在迁移里（`memory_embeddings.embedding`），
        改错了不会报错，只会让每一次写入都被数据库拒绝。
        """
        assert LocalHashingEmbedding().dimension == DEFAULT_EMBEDDING_DIMENSION

    def test_rejects_non_positive_dimension(self) -> None:
        with pytest.raises(ValueError, match="维度必须为正"):
            LocalHashingEmbedding(dimension=0)

    async def test_vectors_are_unit_length(self) -> None:
        provider = LocalHashingEmbedding(dimension=64)
        batch = await provider.embed(texts=["用户偏好简洁回答"])
        norm = sum(component * component for component in batch.vectors[0]) ** 0.5
        assert norm == pytest.approx(1.0)

    async def test_identical_text_gives_identical_vector(self) -> None:
        provider = LocalHashingEmbedding()
        batch = await provider.embed(texts=["用户偏好简洁回答", "用户偏好简洁回答"])
        assert batch.vectors[0] == batch.vectors[1]

    async def test_unrelated_text_is_near_orthogonal(self) -> None:
        provider = LocalHashingEmbedding()
        left, right, same = (
            await provider.embed(
                texts=[
                    "用户偏好简洁回答",
                    "今天股市大跌",
                    "用户偏好简洁回答",
                ]
            )
        ).vectors
        assert cosine_similarity(left, same) == pytest.approx(1.0)
        assert cosine_similarity(left, right) == pytest.approx(0.0, abs=0.35)

    async def test_empty_text_is_the_zero_vector(self) -> None:
        """空文本得到零向量，而不是抛错。

        调用方必须把它当作"无匹配信号"处理（见 ``is_zero_vector``）——
        零向量的余弦相似度是未定义的。
        """
        provider = LocalHashingEmbedding()
        batch = await provider.embed(texts=["", "   "])
        assert all(component == 0.0 for component in batch.vectors[0])
        assert all(component == 0.0 for component in batch.vectors[1])

    async def test_declared_version_and_dimension_agree(self) -> None:
        provider = LocalHashingEmbedding(dimension=32)
        batch = await provider.embed(texts=["测试"])
        assert len(batch.vectors[0]) == provider.dimension
        assert batch.usage == TokenUsage()


class TestLocalDeterminism:
    """🔴 本地实现必须是**跨进程**确定的。"""

    def test_within_a_process(self) -> None:
        provider = LocalHashingEmbedding(dimension=32)
        assert provider.encode("用户偏好简洁回答") == provider.encode("用户偏好简洁回答")

    def test_across_processes(self) -> None:
        """🔴 换个进程、换个 ``PYTHONHASHSEED``，向量必须一模一样。

        这是本实现**不能用内置 ``hash()``** 的全部理由：
        CPython 对字符串的哈希做了随机化（防碰撞攻击），
        所以 ``hash("文本")`` 每个进程都不同。

        用它算向量的后果非常隐蔽：**同一个进程内**算出的向量彼此可比，
        测试全绿；但**写入数据库的向量是上一个进程算的**，
        重启之后查询向量落在另一套随机桶里——
        检索会静默地全部失配，不报错，只是永远查不到任何记忆。
        """
        program = (
            "import json;"
            "from ai_psi.providers.embeddings import LocalHashingEmbedding;"
            "print(json.dumps(LocalHashingEmbedding(dimension=64).encode('用户偏好简洁回答')))"
        )
        runs = [
            subprocess.run(
                [sys.executable, "-c", program],
                check=True,
                capture_output=True,
                text=True,
                env={"PYTHONHASHSEED": seed, "PATH": ""},
            ).stdout
            for seed in ("0", "1", "12345")
        ]
        vectors = [json.loads(run) for run in runs]
        assert vectors[0] == vectors[1] == vectors[2]
        # 顺带确认测试本身有意义：这段文本确实产生了非零向量
        assert any(component != 0.0 for component in vectors[0])


class TestNormalizeVector:
    def test_zero_vector_stays_zero(self) -> None:
        """🔴 零向量归一化后仍是零向量，**不是 NaN**。

        除以 0 会得到 NaN，而 NaN 一旦进入数据库，
        后续每一次比较都会得到 NaN——`ORDER BY` 会彻底失去意义，
        结果集看起来正常却毫无顺序。
        """
        assert normalize_vector([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]

    def test_scales_to_unit_length(self) -> None:
        result = normalize_vector([3.0, 4.0])
        assert result == pytest.approx([0.6, 0.8])


def _embedding_provider(handler: object, *, dimension: int = 4) -> OpenAICompatibleEmbedding:
    """构造一个走 MockTransport 的外部向量 Provider。"""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return OpenAICompatibleEmbedding(
        client=httpx.AsyncClient(transport=transport),
        api_key=SecretStr("sk-test"),
        base_url="https://embeddings.example/v1",
        model="text-embedding-test",
        dimension=dimension,
    )


def _ok(*vectors: list[float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        },
    )


class TestOpenAICompatibleEmbedding:
    """外部向量接口。

    ⚠️ 开发机上的中转站**没有 embedding 渠道**，任何模型都返回
    `model_not_found`，因此本类覆盖的是"成功响应"的处理逻辑，
    而不是一次真实调用。这一点记录在 ADR-0017。
    """

    async def test_parses_vectors_and_usage(self) -> None:
        provider = _embedding_provider(lambda request: _ok([1.0, 0.0, 0.0, 0.0]))
        batch = await provider.embed(texts=["a"])
        assert batch.vectors == [[1.0, 0.0, 0.0, 0.0]]
        assert batch.usage.input_tokens == 7

    async def test_orders_by_index_not_by_position(self) -> None:
        """🔴 按 ``index`` 归位，不依赖返回顺序。

        协议要求按 index 排序，但"协议要求"与"实现照做"是两回事。
        顺序错位会让文本与向量**静默地配错**——
        检索结果从此与内容无关，而且完全看不出来。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0, 0.0, 0.0]},
                        {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                    ]
                },
            )

        provider = _embedding_provider(handler)
        batch = await provider.embed(texts=["first", "second"])
        assert batch.vectors[0] == [1.0, 0.0, 0.0, 0.0]
        assert batch.vectors[1] == [0.0, 1.0, 0.0, 0.0]

    async def test_empty_input_skips_the_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            msg = "不应发出请求"
            raise AssertionError(msg)

        provider = _embedding_provider(handler)
        assert (await provider.embed(texts=[])).vectors == []

    async def test_dimension_mismatch_fails_loudly(self) -> None:
        """返回的维度与声明不符时**明确失败**。

        数据库的向量列是定长的：错位的向量根本存不进去。
        若这里放过，错误会推迟到一次 SQL 中，那时信息已经少得多。
        """
        provider = _embedding_provider(lambda request: _ok([1.0, 0.0]), dimension=4)
        with pytest.raises(ProviderError) as excinfo:
            await provider.embed(texts=["a"])
        assert excinfo.value.code == "dimension_mismatch"
        assert "AI_PSI_EMBEDDING_DIMENSION" in excinfo.value.message

    async def test_count_mismatch_fails_loudly(self) -> None:
        provider = _embedding_provider(lambda request: _ok([1.0, 0.0, 0.0, 0.0]))
        with pytest.raises(ProviderError) as excinfo:
            await provider.embed(texts=["a", "b"])
        assert excinfo.value.code == "invalid_response"

    async def test_duplicate_index_leaves_a_gap_and_fails_loudly(self) -> None:
        """重复的 ``index`` 会让某一条文本拿不到向量。

        条数对得上（2 条）、每个 index 也都在范围内——只有"归位"这一步
        能发现两条数据抢了同一个槽位，而另一条文本从来没被填上。
        若在这里静默地留下一个空位，那条文本会配上一个**别的文本的向量**，
        或者一个零向量；两者都是无声的错误。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                        {"index": 0, "embedding": [0.0, 1.0, 0.0, 0.0]},
                    ]
                },
            )

        provider = _embedding_provider(handler)
        with pytest.raises(ProviderError, match="没有返回 index=1"):
            await provider.embed(texts=["a", "b"])

    async def test_out_of_range_index_fails_loudly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 9, "embedding": [0.0] * 4}]})

        provider = _embedding_provider(handler)
        with pytest.raises(ProviderError, match="index 越界"):
            await provider.embed(texts=["a"])

    async def test_empty_vector_fails_loudly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": []}]})

        provider = _embedding_provider(handler)
        with pytest.raises(ProviderError, match="没有返回向量"):
            await provider.embed(texts=["a"])

    async def test_non_object_body_fails_loudly(self) -> None:
        provider = _embedding_provider(lambda request: httpx.Response(200, json=[1, 2, 3]))
        with pytest.raises(ProviderError, match="不是 JSON 对象"):
            await provider.embed(texts=["a"])

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, ProviderRateLimitError),
            (503, ProviderUnavailableError),
        ],
    )
    async def test_http_errors_map_to_domain_exceptions(
        self, status: int, expected: type[Exception]
    ) -> None:
        """与对话 Provider 共用同一套错误映射（任务书 §13.2）。"""
        provider = _embedding_provider(
            lambda request: httpx.Response(status, json={"error": "boom"})
        )
        with pytest.raises(expected):
            await provider.embed(texts=["a"])

    async def test_timeout_maps_to_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        provider = _embedding_provider(handler)
        with pytest.raises(ProviderTimeoutError):
            await provider.embed(texts=["a"])

    def test_version_carries_model_and_dimension(self) -> None:
        """版本里同时带模型名与维度。

        维度不是模型的属性而是**部署**的属性：同一个模型在不同供应商
        那里可能给出不同维度，而维度不同就是不同的向量空间。
        """
        provider = _embedding_provider(lambda request: _ok([0.0] * 4))
        assert provider.version == "openai_compatible_embeddings:text-embedding-test:d4"
