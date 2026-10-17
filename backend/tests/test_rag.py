"""RAG 知识库测试：分块、中文 bigram 检索、提示词注入、API 与热更新。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import LLMSettings, load_settings
from app.engine.llm import LLMReplyGenerator
from app.main import create_app
from app.rag import KnowledgeBase, _chunk_text
from tests.conftest import make_settings


# ---- 分块 ----
def test_chunk_text_merges_and_splits():
    paras = [f"段落{i}" + "内容" * 50 for i in range(10)]
    text = "\n\n".join(paras)
    chunks = _chunk_text(text)
    assert all(len(c) <= 520 for c in chunks)  # 上限 + 段间换行余量
    assert "".join(chunks).startswith("段落0")
    assert len(_chunk_text("短文")) == 1


def test_chunk_text_hard_split_long_paragraph():
    chunks = _chunk_text("长" * 1200)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) == 3


# ---- 检索 ----
@pytest.fixture()
def kb(tmp_path: Path) -> KnowledgeBase:
    (tmp_path / "faq.md").write_text(
        "# 报销流程\n\n出差报销需要发票和审批单，每月25日前提交给财务张三办理。", encoding="utf-8"
    )
    (tmp_path / "cook.txt").write_text(
        "红烧肉做法：五花肉焯水，加糖炒色，小火炖四十分钟。", encoding="utf-8"
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "oncall.md").write_text(
        "值班安排：周一张三，周二李四，紧急故障直接电话联系。", encoding="utf-8"
    )
    kb = KnowledgeBase()
    kb.configure(str(tmp_path))
    return kb


def test_configure_scans_supported_files(kb: KnowledgeBase):
    st = kb.stats()
    assert st["enabled"] is True
    assert st["files"] == 3  # 含子目录
    assert st["chunks"] >= 3
    assert st["last_error"] == ""


def test_configure_rejects_missing_dir(tmp_path: Path):
    kb = KnowledgeBase()
    with pytest.raises(ValueError):
        kb.configure(str(tmp_path / "不存在"))


def test_search_ranks_relevant_doc_top(kb: KnowledgeBase):
    hits = kb.search("出差报销怎么办理")
    assert hits and hits[0][1].file == "faq.md"
    hits2 = kb.search("值班安排 周二")
    assert hits2 and hits2[0][1].file == "sub/oncall.md"


def test_search_unrelated_query_returns_empty(kb: KnowledgeBase):
    assert kb.search("今天天气怎么样") == []


def test_search_short_query_returns_empty(kb: KnowledgeBase):
    assert kb.search("好") == []


def test_reset_clears(kb: KnowledgeBase):
    kb.reset()
    assert kb.stats()["enabled"] is False
    assert kb.search("报销") == []


def test_rescan_picks_up_new_files(kb: KnowledgeBase, tmp_path: Path):
    before = kb.stats()["files"]
    (tmp_path / "new.md").write_text("新知识：服务器机房在B栋三楼。", encoding="utf-8")
    st = kb.scan(force=True)
    assert st["files"] == before + 1
    hits = kb.search("机房在哪里")
    assert hits and hits[0][1].file == "new.md"


def test_gbk_file_supported(tmp_path: Path):
    (tmp_path / "gbk.txt").write_bytes("老系统的GBK说明文档内容".encode("gbk"))
    kb = KnowledgeBase()
    kb.configure(str(tmp_path))
    assert kb.stats()["files"] == 1
    assert kb.search("GBK说明文档")


# ---- LLM 提示词注入 ----
@pytest.mark.asyncio
async def test_llm_rag_hits_injected_into_prompt(kb: KnowledgeBase):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        captured["user"] = body["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "好的"}}]})

    cfg = LLMSettings(api_key="t", base_url="http://llm.test")
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler), kb=kb)
    from app.models import ChatMessage, Platform

    msg = ChatMessage(platform=Platform.WECHAT, chat_id="c", sender="张三", content="出差报销怎么办理")
    result = await engine.generate(msg, [])
    assert "[知识库参考资料]" in captured["user"]
    assert "发票和审批单" in captured["user"]
    assert result.meta["rag"] == ["faq.md"]
    await engine.aclose()


@pytest.mark.asyncio
async def test_llm_without_kb_no_rag_section():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "好的"}}]})

    engine = LLMReplyGenerator(LLMSettings(api_key="t", base_url="http://llm.test"),
                               transport=httpx.MockTransport(handler))
    from app.models import ChatMessage, Platform

    msg = ChatMessage(platform=Platform.WECHAT, chat_id="c", sender="张三", content="报销流程")
    result = await engine.generate(msg, [])
    assert "rag" not in result.meta
    await engine.aclose()


# ---- API 与热更新 ----
@pytest.fixture()
def rag_client(tmp_path):
    settings = make_settings(config_file=str(tmp_path / "config.yaml"))
    with TestClient(create_app(settings), client=("127.0.0.1", 12345)) as c:
        yield c, tmp_path


def test_api_knowledge_flow(rag_client):
    client, tmp_path = rag_client
    # 未配置状态
    st = client.get("/api/knowledge").json()
    assert st["enabled"] is False
    # 目录选择器：列出盘符根 / 子目录
    roots = client.get("/api/fs/dirs").json()
    assert roots["parent"] is None and roots["dirs"]
    sub = client.get("/api/fs/dirs", params={"path": str(tmp_path)}).json()
    assert sub["path"] == str(tmp_path)
    # 设置目录 → 立即生效并可查询
    r = client.put("/api/config", json={"knowledge_dir": str(tmp_path)})
    assert r.status_code == 200 and "knowledge_dir" in r.json()["changed"]
    st = client.get("/api/knowledge").json()
    assert st["enabled"] is True and st["files"] == 0  # 空目录：0 文件
    # 非法目录 → 400
    assert client.put("/api/config", json={"knowledge_dir": str(tmp_path / "no")}).status_code == 400
    # 停用
    assert client.put("/api/config", json={"knowledge_dir": ""}).status_code == 200
    assert client.get("/api/knowledge").json()["enabled"] is False


def test_api_knowledge_rescan(rag_client):
    client, tmp_path = rag_client
    client.put("/api/config", json={"knowledge_dir": str(tmp_path)})
    (tmp_path / "late.md").write_text("补充知识：Wi-Fi 密码是 12345678。", encoding="utf-8")
    st = client.post("/api/knowledge/scan").json()
    assert st["files"] == 1
    assert client.get("/api/knowledge").json()["chunks"] >= 1


def test_api_fs_dirs_invalid_path(rag_client):
    client, _ = rag_client
    assert client.get("/api/fs/dirs", params={"path": "Z:\\不存在"}).status_code == 400


def test_knowledge_dir_persisted(rag_client):
    client, tmp_path = rag_client
    cfg_file = tmp_path / "config.yaml"
    client.put("/api/config", json={"knowledge_dir": str(tmp_path), "persist": True})
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert data["knowledge_dir"] == str(tmp_path)
    assert load_settings(str(cfg_file)).knowledge_dir == str(tmp_path)
