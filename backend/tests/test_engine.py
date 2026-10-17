"""回复生成器测试：Mock 实现 + LLM 实现（httpx MockTransport 模拟 DeepSeek）。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import LLMSettings
from app.engine import MockReplyGenerator
from app.engine.base import Candidate
from app.engine.jev import MockJEVJudge
from app.engine.llm import LLMReplyGenerator, STYLE_PRESETS, _SYSTEM_PROMPT
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


# ---------------- 回复风格预设 ----------------
def test_llm_style_default_prompt_untouched():
    """style=default：系统提示词保持内置默认，不追加风格段。"""
    engine = LLMReplyGenerator(LLMSettings(api_key="t"))
    assert engine._system_prompt() == _SYSTEM_PROMPT


def test_llm_style_all_presets_injected():
    """全部预设都会把风格要求拼进系统提示词，且互不相同。"""
    prompts = {}
    for name, desc in STYLE_PRESETS.items():
        if not desc:
            continue
        engine = LLMReplyGenerator(LLMSettings(api_key="t", style=name))
        p = engine._system_prompt()
        assert p.startswith(_SYSTEM_PROMPT)
        assert desc in p
        prompts[name] = p
    assert len(set(prompts.values())) == len(prompts)


def test_llm_style_unknown_falls_back_to_default():
    """未知风格名回退默认提示词（不报错）。"""
    engine = LLMReplyGenerator(LLMSettings(api_key="t", style="不存在的风格"))
    assert engine._system_prompt() == _SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_llm_style_sent_in_request():
    """generate() 实际发出的 system 消息带风格要求。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        captured["system"] = body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    cfg = LLMSettings(api_key="t", base_url="http://llm.test", style="formal")
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler))
    await engine.generate(make_msg("你好"), [])
    assert "正式、专业" in captured["system"]
    await engine.aclose()
