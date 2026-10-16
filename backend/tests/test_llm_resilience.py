"""LLM 重试/限流/降级测试。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import LLMSettings
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.engine.llm import LLMReplyGenerator
from app.models import ChatMessage, Platform, STATUS_IGNORED
from app.store import EventBus, MessageStore

from conftest import make_settings

from app.core.agent import AgentService


def make_msg(content="你好"):
    return ChatMessage(platform=Platform.WECHAT, chat_id="c1", sender="张三", content=content)


def ok_body(text="你好呀"):
    return {"choices": [{"message": {"content": json.dumps({"candidates": [text]}, ensure_ascii=False)}}]}


@pytest.mark.asyncio
async def test_retry_on_500_then_success():
    """前两次 500，第三次成功：应重试并返回结果，共 3 次请求。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=ok_body("重试成功"))

    cfg = LLMSettings(api_key="k", base_url="http://llm.test", max_retries=2, retry_backoff=0.0)
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler))
    result = await engine.generate(make_msg("你好"), [])
    assert result.content == "重试成功"
    assert calls["n"] == 3
    await engine.aclose()


@pytest.mark.asyncio
async def test_retry_on_429_and_timeout():
    """429 与网络错误都应触发重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate"})
        if calls["n"] == 2:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json=ok_body("ok"))

    cfg = LLMSettings(api_key="k", base_url="http://llm.test", max_retries=2, retry_backoff=0.0)
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler))
    result = await engine.generate(make_msg("hi"), [])
    assert result.content == "ok"
    assert calls["n"] == 3
    await engine.aclose()


@pytest.mark.asyncio
async def test_no_retry_on_client_error():
    """401 等客户端错误快速失败，不重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    cfg = LLMSettings(api_key="k", base_url="http://llm.test", max_retries=3, retry_backoff=0.0)
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await engine.generate(make_msg("hi"), [])
    assert calls["n"] == 1
    await engine.aclose()


@pytest.mark.asyncio
async def test_all_attempts_exhausted():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "down"})

    cfg = LLMSettings(api_key="k", base_url="http://llm.test", max_retries=1, retry_backoff=0.0)
    engine = LLMReplyGenerator(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await engine.generate(make_msg("hi"), [])
    assert calls["n"] == 2  # 1 + 1 retry
    await engine.aclose()


# ---------------- Agent 降级草稿 ----------------
@pytest.mark.asyncio
async def test_engine_failure_produces_fallback_draft():
    """引擎彻底不可用：生成错误占位草稿进入人工流程，而不是消息丢失。"""

    class BoomEngine(MockReplyGenerator):
        async def generate(self, message, history):
            raise RuntimeError("LLM 服务爆炸")

    agent = AgentService(
        settings=make_settings(), store=MessageStore(), bus=EventBus(), engine=BoomEngine()
    )
    agent.setup_connectors()
    await agent.ingest(make_msg("你好"))
    drafts = agent.store.list_messages(chat_id="c1", direction="out")
    assert len(drafts) == 1
    assert "暂时不可用" in drafts[0].content
    assert drafts[0].status == "pending"
    assert drafts[0].meta["engine"] == "error"
    await agent.stop_all()


# ---------------- 限流 ----------------
@pytest.mark.asyncio
async def test_rate_limit_per_chat():
    """超出每会话每分钟限额的消息被忽略并记录原因；额度不影响非文本占位。"""
    settings = make_settings(llm_rate_limit_per_chat=2)
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=MockReplyGenerator()
    )
    agent.setup_connectors()
    for i in range(4):
        await agent.ingest(make_msg(f"消息{i}"))

    drafts = agent.store.list_messages(chat_id="c1", direction="out")
    assert len(drafts) == 2  # 前 2 条生成
    msgs = agent.store.list_messages(chat_id="c1", direction="in")
    ignored = [m for m in msgs if m.status == STATUS_IGNORED]
    assert len(ignored) == 2
    assert all("频率限制" in m.meta["ignore_reason"] for m in ignored)
    await agent.stop_all()


@pytest.mark.asyncio
async def test_rate_limit_zero_unlimited():
    settings = make_settings(llm_rate_limit_per_chat=0)
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=MockReplyGenerator()
    )
    agent.setup_connectors()
    for i in range(5):
        await agent.ingest(make_msg(f"m{i}"))
    assert len(agent.store.list_messages(chat_id="c1", direction="out")) == 5
    await agent.stop_all()
