"""对**真实向量服务**的端到端验证（阶段 5）。

🔴 **默认不运行。** 需要同时满足：

```
AI_PSI_EMBEDDING_PROVIDER=openai_compatible
AI_PSI_EMBEDDING_MODEL / BASE_URL / API_KEY 均已配置
AI_PSI_RUN_LIVE_TESTS=1
```

⚠️ **开发机上当前跑不了，这是已知事实而不是配置疏漏。**

* DeepSeek 没有 `/v1/embeddings` 接口（实测 404）；
* 开发机上的中转站有该端点，但账户分组下**没有任何 embedding 渠道**
  （实测 `503 model_not_found`）；
* `api.openai.com` 在本机不可达。

所以这一组用例**存在的意义是"渠道一通就能验证"**，
而不是"我们宣称它验证过了"。在渠道开通之前，
`OpenAICompatibleEmbedding` 的行为由 `tests/unit/test_embeddings.py`
里的 `httpx.MockTransport` 覆盖——那能证明解析逻辑对，
**证明不了真实服务返回的东西长这样**。

（这一点记录在 ADR-0017 §1，不藏着。）
"""

from __future__ import annotations

import os

import httpx
import pytest

from ai_psi.config import Settings
from ai_psi.providers.embeddings import OpenAICompatibleEmbedding
from ai_psi.providers.registry import build_embedding_provider

pytestmark = [pytest.mark.integration, pytest.mark.live]

_LIVE_ENABLED = os.environ.get("AI_PSI_RUN_LIVE_TESTS") == "1"


def _live_settings() -> Settings | None:
    """返回一份可用的外部向量配置；不可用则返回 ``None``。

    判断按**配置层的那套规则**来，不在测试里重写一遍——
    重写的规则迟早会和真实装配走岔（阶段 4 就踩过一次：
    只查了带前缀的环境变量名，而真正配着的是通用名）。
    """
    settings = Settings()
    if settings.embedding_provider != "openai_compatible":
        return None
    if not (
        settings.embedding_model and settings.embedding_base_url and settings.embedding_api_key
    ):
        return None
    return settings


_LIVE_SETTINGS = _live_settings()

pytestmark.append(
    pytest.mark.skipif(
        not (_LIVE_ENABLED and _LIVE_SETTINGS is not None),
        reason=(
            "真实向量测试需要 AI_PSI_EMBEDDING_PROVIDER=openai_compatible、"
            "配好 MODEL/BASE_URL/API_KEY，并设置 AI_PSI_RUN_LIVE_TESTS=1。"
            "开发机上没有可用的 embedding 渠道（ADR-0017 §1）"
        ),
    )
)


@pytest.fixture
def provider() -> OpenAICompatibleEmbedding:
    """按配置装配的真实向量 Provider。"""
    assert _LIVE_SETTINGS is not None
    return build_embedding_provider(_LIVE_SETTINGS, httpx.AsyncClient())  # type: ignore[return-value]


class TestLiveEmbedding:
    async def test_returns_vectors_of_the_declared_dimension(
        self, provider: OpenAICompatibleEmbedding
    ) -> None:
        """🔴 真实服务返回的维度必须与配置一致。

        维度是数据库列的固定属性：不一致时写入会被 PostgreSQL 拒绝，
        而那时错误信息离真正的原因已经很远。
        """
        batch = await provider.embed(texts=["用户偏好简洁回答"])
        assert len(batch.vectors) == 1
        assert len(batch.vectors[0]) == provider.dimension

    async def test_identical_text_gives_identical_vector(
        self, provider: OpenAICompatibleEmbedding
    ) -> None:
        batch = await provider.embed(texts=["用户偏好简洁回答", "用户偏好简洁回答"])
        assert batch.vectors[0] == pytest.approx(batch.vectors[1])

    async def test_batch_order_is_preserved(self, provider: OpenAICompatibleEmbedding) -> None:
        """🔴 批量调用必须**同序**返回。

        顺序错位会让文本与向量静默地配错——检索结果从此与内容无关，
        而且完全看不出来。真实的响应体我们控制不了，
        只能验证"归位逻辑在真实数据上成立"。
        """
        single = await provider.embed(texts=["天空中飘着云"])
        batch = await provider.embed(texts=["天空中飘着云", "用户偏好简洁回答"])
        assert batch.vectors[0] == pytest.approx(single.vectors[0], rel=1e-6)

    async def test_similar_text_is_closer_than_unrelated_text(
        self, provider: OpenAICompatibleEmbedding
    ) -> None:
        """真语义向量**应该**能分辨同义与无关——这正是它相对本地实现的价值。

        本地哈希向量过不了这一条：`喜欢简洁` 与 `讨厌啰嗦`
        在词面空间里几乎正交（ADR-0017 §1）。
        """
        from ai_psi.memory.retrieval import cosine_similarity

        vectors = (
            await provider.embed(texts=["用户喜欢简洁的回答", "用户讨厌啰嗦冗长", "今天股市大跌"])
        ).vectors
        same_topic = cosine_similarity(vectors[0], vectors[1])
        unrelated = cosine_similarity(vectors[0], vectors[2])
        assert same_topic > unrelated
