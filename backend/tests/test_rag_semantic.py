"""RAG 语义检索（可选 Embedding）测试：混合排序、降级、磁盘缓存、配置加载。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import EmbeddingSettings, LLMSettings, load_settings
from app.engine.llm import LLMReplyGenerator
from app.main import create_app
from app.models import ChatMessage, Platform
from app.rag import KnowledgeBase
from tests.conftest import make_settings


# ---- 假 Embedding 服务：按主题关键词映射为三维向量，可稳定预测检索结果 ----
_TOPICS = [
    (["报销", "发票", "财务", "审批", "钱", "退"], [1.0, 0.0, 0.0]),
    (["值班", "机房", "故障", "电话"], [0.0, 1.0, 0.0]),
    (["红烧肉", "五花肉", "炖", "糖"], [0.0, 0.0, 1.0]),
]
_FALLBACK_VEC = [0.0, 0.0, 0.0]


def _fake_vec(text: str) -> list[float]:
    for words, vec in _TOPICS:
        if any(w in text for w in words):
            return vec
    return _FALLBACK_VEC


def make_embedder(status_code: int = 200, counter: dict | None = None):
    """返回 (transport, handler)。counter 统计 /embeddings 收到的 input 条数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            if counter is not None:
                counter["inputs"] += len(json.loads(request.read())["input"])
                counter["calls"] += 1
            if status_code != 200:
                return httpx.Response(status_code, json={"error": "boom"})
            body = json.loads(request.read())
            data = [{"index": i, "embedding": _fake_vec(t)} for i, t in enumerate(body["input"])]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return httpx.MockTransport(handler)


def make_semantic_kb(tmp_path, cache_path=None, status_code=200, counter=None) -> KnowledgeBase:
    (tmp_path / "faq.md").write_text(
        "# 报销流程\n\n出差报销需要发票和审批单，每月25日前提交给财务张三办理。", encoding="utf-8"
    )
    (tmp_path / "cook.txt").write_text(
        "红烧肉做法：五花肉焯水，加糖炒色，小火炖四十分钟。", encoding="utf-8"
    )
    (tmp_path / "oncall.md").write_text(
        "值班安排：周一张三，周二李四，紧急故障直接电话联系。", encoding="utf-8"
    )
    kb = KnowledgeBase()
    kb.configure(str(tmp_path))
    kb.set_embedding(
        EmbeddingSettings(enabled=True, base_url="http://embed.test", api_key="k",
                          model="fake-embed", batch_size=2),
        cache_path=str(cache_path) if cache_path else None,
        transport=make_embedder(status_code, counter),
    )
    return kb


@pytest.fixture()
def sem_kb(tmp_path):
    return make_semantic_kb(tmp_path)


# ---- 混合检索 ----
@pytest.mark.asyncio
async def test_semantic_finds_paraphrase_where_lexical_fails(sem_kb):
    """核心价值：同义改述（无共同词面）靠向量召回。"""
    hits = await sem_kb.search_hybrid("钱怎么退回来")  # 与知识库无任何公共 bigram
    assert hits, "语义检索应命中"
    assert hits[0][1].file == "faq.md"
    assert sem_kb.stats()["embedding"]["last_error"] == ""


@pytest.mark.asyncio
async def test_hybrid_keeps_lexical_results(sem_kb):
    """词面能命中的查询，混合后仍然命中同一文档。"""
    hits = await sem_kb.search_hybrid("出差报销怎么办理")
    assert hits and hits[0][1].file == "faq.md"


@pytest.mark.asyncio
async def test_disabled_embedding_equals_lexical(tmp_path):
    kb = make_semantic_kb(tmp_path)
    kb.set_embedding(EmbeddingSettings(enabled=False))  # 关闭
    assert kb.stats()["embedding"]["enabled"] is False
    hits = await kb.search_hybrid("出差报销")
    assert hits and hits[0][1].file == "faq.md"
    await kb.aclose()


# ---- 降级 ----
@pytest.mark.asyncio
async def test_embed_failure_falls_back_to_lexical(tmp_path):
    kb = make_semantic_kb(tmp_path, status_code=500)
    hits = await kb.search_hybrid("出差报销")  # 词面可命中，语义挂了不影响
    assert hits and hits[0][1].file == "faq.md"
    assert kb.stats()["embedding"]["last_error"]  # 记录了错误
    await kb.aclose()


@pytest.mark.asyncio
async def test_embed_cooldown_skips_calls(tmp_path):
    counter = {"inputs": 0, "calls": 0}
    kb = make_semantic_kb(tmp_path, status_code=500, counter=counter)
    await kb.search_hybrid("值班安排")
    calls_after_first = counter["calls"]
    await kb.search_hybrid("值班安排")  # 冷却期内不再打 Embedding 接口
    assert counter["calls"] == calls_after_first
    await kb.aclose()


# ---- 磁盘缓存：重启后分块向量不重复计费 ----
@pytest.mark.asyncio
async def test_vector_disk_cache_avoids_rebilling(tmp_path):
    cache = tmp_path / "cache" / "vectors.json"
    counter1 = {"inputs": 0, "calls": 0}
    kb1 = make_semantic_kb(tmp_path, cache_path=cache, counter=counter1)
    await kb1.search_hybrid("出差报销")
    assert cache.is_file()
    chunk_inputs_first = counter1["inputs"] - 1  # 减去查询本身
    assert chunk_inputs_first > 0
    await kb1.aclose()

    counter2 = {"inputs": 0, "calls": 0}
    kb2 = make_semantic_kb(tmp_path, cache_path=cache, counter=counter2)
    hits = await kb2.search_hybrid("钱怎么退回来")
    assert hits and hits[0][1].file == "faq.md"
    assert counter2["inputs"] == 1, "重启后只应嵌入查询本身，分块全部命中缓存"
    await kb2.aclose()


@pytest.mark.asyncio
async def test_stats_report_cached_vectors(sem_kb):
    await sem_kb.search_hybrid("红烧肉怎么做")
    st = sem_kb.stats()["embedding"]
    assert st["enabled"] is True and st["model"] == "fake-embed"
    assert st["cached"] >= 3
    await sem_kb.aclose()


# ---- 配置加载 ----
def test_embedding_config_from_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "embedding": {"enabled": True, "base_url": "http://e.test",
                          "api_key": "ek", "model": "bge-m3", "batch_size": 8},
            "llm": {"vision_model": "qwen-vl-plus", "vision_base_url": "http://v.test"},
        }, allow_unicode=True),
        encoding="utf-8",
    )
    s = load_settings(str(cfg))
    assert s.embedding.enabled is True
    assert s.embedding.model == "bge-m3" and s.embedding.batch_size == 8
    assert s.embedding.api_key == "ek"
    assert s.llm.vision_model == "qwen-vl-plus"
    assert s.llm.vision_base_url == "http://v.test"


def test_embedding_config_from_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("CHATAGENT_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("CHATAGENT_EMBEDDING_MODEL", "emb-x")
    monkeypatch.setenv("CHATAGENT_EMBEDDING_API_KEY", "sk-t")
    s = load_settings(str(cfg))
    assert s.embedding.enabled is True and s.embedding.model == "emb-x"
    assert s.embedding.api_key == "sk-t"


def test_set_embedding_requires_base_url(tmp_path):
    kb = KnowledgeBase()
    with pytest.raises(ValueError):
        kb.set_embedding(EmbeddingSettings(enabled=True, model="m", base_url="", api_key=""))


def test_set_embedding_requires_model_when_enabled():
    """enabled=True 但未配 model：必须报错（而不是表面上开关已开、实际未生效）。"""
    kb = KnowledgeBase()
    with pytest.raises(ValueError, match="model"):
        kb.set_embedding(EmbeddingSettings(enabled=True, model=""))
    assert kb.stats()["embedding"]["enabled"] is False


# ---- LLM 引擎 × 语义检索 集成 ----
@pytest.mark.asyncio
async def test_llm_engine_uses_hybrid_semantic_hits(tmp_path):
    """同义改述查询（词面零命中）通过混合检索注入提示词并记录 meta.rag。"""
    kb = make_semantic_kb(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "好的"}}]})

    engine = LLMReplyGenerator(
        LLMSettings(api_key="t", base_url="http://llm.test"),
        transport=httpx.MockTransport(handler), kb=kb,
    )
    msg = ChatMessage(platform=Platform.WECHAT, chat_id="c", sender="张三", content="钱怎么退回来")
    result = await engine.generate(msg, [])
    assert result.meta["rag"] == ["faq.md"]
    await engine.aclose()
    await kb.aclose()


# ---- API 热更新（embedding_enabled） ----
@pytest.fixture()
def api_client(tmp_path):
    settings = make_settings(config_file=str(tmp_path / "config.yaml"))
    with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as c:
        yield c, tmp_path


def test_api_embedding_toggle_without_model_rejected(api_client):
    """未配 embedding.model 时开语义检索 → 400，且配置与 KB 状态都保持关闭（一致回滚）。"""
    client, _ = api_client
    r = client.put("/api/config", json={"embedding_enabled": True})
    assert r.status_code == 400
    assert client.get("/api/config").json()["embedding"]["enabled"] is False
    assert client.get("/api/knowledge").json()["embedding"]["enabled"] is False


def test_api_embedding_off_persists_to_yaml(api_client):
    client, tmp_path = api_client
    r = client.put("/api/config", json={"embedding_enabled": False, "persist": True})
    assert r.status_code == 200 and "embedding_enabled" in r.json()["changed"]
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert data["embedding"]["enabled"] is False
    assert load_settings(str(tmp_path / "config.yaml")).embedding.enabled is False


def test_mask_hides_embedding_api_key(tmp_path):
    from app.config import mask_settings
    s = load_settings(str(tmp_path / "empty.yaml"))
    s.embedding.api_key = "secret-key-123"
    masked = mask_settings(s)
    assert masked["embedding"]["api_key"].startswith("secr")
    assert "secret-key-123" not in str(masked)
