"""回复生成器测试：Mock 实现 + LLM 实现（httpx MockTransport 模拟 DeepSeek）。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import LLMSettings
from app.engine import MockReplyGenerator
from app.engine.base import Candidate
from app.engine.jev import MockJEVJudge
from app.engine.llm import LLMReplyGenerator
from app.models import ChatMessage, Platform


def make_msg(content: str, sender: str = "张三") -> ChatMessage:
    return ChatMessage(platform=Platform.WECHAT, chat_id="c1", sender=sender, content=content)


# ---------------- Mock 引擎 ----------------
@pytest.mark.asyncio
async def test_mock_engine_greeting():
    result = await MockReplyGenerator().generate(make_msg("你好"), [])
    assert result.content
    assert "你好" in result.content
    assert len(result.candidates) >= 1
    assert not result.blocked


@pytest.mark.asyncio
async def test_mock_engine_question():
    result = await MockReplyGenerator().generate(make_msg("明天开会几点？"), [])
    assert result.content


@pytest.mark.asyncio
async def test_mock_engine_history_not_needed_but_accepted():
    history = [make_msg("在吗")]
    result = await MockReplyGenerator().generate(make_msg("收到"), history)
    assert result.content


# ---------------- LLM 引擎 ----------------
def _llm(content: str):
    """构造返回固定 content 的 MockTransport。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert body["model"] == "deepseek-chat"
        assert request.url.path == "/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_llm_engine_json_candidates():
    raw = json.dumps({"candidates": ["好哒，没问题", "收到！"]}, ensure_ascii=False)
    cfg = LLMSettings(api_key="test", base_url="http://llm.test", model="deepseek-chat", num_candidates=2)
    engine = LLMReplyGenerator(cfg, transport=_llm(raw))
    result = await engine.generate(make_msg("在吗"), [])
    assert result.content == "好哒，没问题"
    assert [c.content for c in result.candidates] == ["好哒，没问题", "收到！"]
    assert not result.blocked
    assert result.meta["engine"] == "llm"
    await engine.aclose()


@pytest.mark.asyncio
async def test_llm_engine_line_fallback():
    cfg = LLMSettings(api_key="k", base_url="http://llm.test", num_candidates=2)
    engine = LLMReplyGenerator(cfg, transport=_llm("1. 第一条回复\n2. 第二条回复"))
    result = await engine.generate(make_msg("在吗"), [])
    assert result.candidates[0].content == "第一条回复"
    assert result.candidates[1].content == "第二条回复"
    await engine.aclose()


@pytest.mark.asyncio
async def test_llm_engine_with_jev_blocks_risky_reply():
    """LLM 生成敏感内容（要求转账）→ Mock JEV 拦截 → blocked，且触发一次重写。"""
    raw = json.dumps({"candidates": ["好的，请给我转账500元"]}, ensure_ascii=False)
    cfg = LLMSettings(api_key="k", base_url="http://llm.test", num_candidates=1)
    engine = LLMReplyGenerator(cfg, jev=MockJEVJudge(), rewrite_on_fail=True, transport=_llm(raw))
    result = await engine.generate(make_msg("你好，帮我操作下"), [])
    assert result.blocked is True
    assert result.meta["jev"] is True
    assert result.meta["rewritten"] is True
    assert "blocked_reason" in result.meta
    await engine.aclose()


@pytest.mark.asyncio
async def test_llm_engine_jev_passes_normal_reply():
    raw = json.dumps({"candidates": ["你好呀，很高兴认识你，请问有什么事吗？"]}, ensure_ascii=False)
    cfg = LLMSettings(api_key="k", base_url="http://llm.test", num_candidates=1)
    engine = LLMReplyGenerator(cfg, jev=MockJEVJudge(), transport=_llm(raw))
    result = await engine.generate(make_msg("你好"), [])
    assert result.blocked is False
    assert result.candidates[0].passed is True
    await engine.aclose()
