"""连发消息合并（debounce）测试。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.models import Platform
from app.store import EventBus, MessageStore

from conftest import make_settings


class CountingEngine(MockReplyGenerator):
    """记录每次生成的触发消息与上下文，便于断言合并行为。"""

    name = "counting-mock"

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, list[str]]] = []

    async def generate(self, message, history, kb=None):
        self.calls.append((message.content, [h.content for h in history]))
        return await super().generate(message, history)


def build_agent(settings, engine=None):
    store, bus = MessageStore(), EventBus()
    agent = AgentService(
        settings=settings, store=store, bus=bus, engine=engine or MockReplyGenerator()
    )
    agent.setup_connectors()
    return agent


async def wait_until(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        await asyncio.sleep(0.03)
    return False


@pytest.mark.asyncio
async def test_burst_merged_into_single_draft():
    """三条连发只生成一份草稿，触发消息是最后一条，前两条进入上下文。"""
    engine = CountingEngine()
    agent = build_agent(make_settings(debounce_seconds=0.3), engine)
    conn = agent.connectors[Platform.WECHAT]

    m1 = await conn.inject("c1", "李四", "在吗")
    m2 = await conn.inject("c1", "李四", "明天有空吗")
    m3 = await conn.inject("c1", "李四", "帮我看看那个文档")
    for m in (m1, m2, m3):
        await agent.ingest(m)

    ok = await wait_until(lambda: len(engine.calls) == 1)
    assert ok, "静默期后应只生成一次"
    await asyncio.sleep(0.2)  # 确认没有第二次生成
    assert len(engine.calls) == 1

    content, history = engine.calls[0]
    assert content == "帮我看看那个文档"  # 最后一条为触发消息
    assert "在吗" in history and "明天有空吗" in history  # 连发前两条进入上下文

    drafts = agent.store.list_messages(chat_id="c1", direction="out")
    assert len(drafts) == 1
    assert drafts[0].reply_to == m3.id
    assert drafts[0].status == "pending"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_timer_resets_on_new_message():
    """静默期内来新消息会重置计时：草稿只在最后一条之后出现一次。"""
    engine = CountingEngine()
    agent = build_agent(make_settings(debounce_seconds=0.4), engine)
    conn = agent.connectors[Platform.WECHAT]

    m1 = await conn.inject("c1", "甲", "第一条")
    await agent.ingest(m1)
    await asyncio.sleep(0.2)  # 小于静默期，计时应被重置
    m2 = await conn.inject("c1", "甲", "第二条")
    await agent.ingest(m2)

    ok = await wait_until(lambda: len(engine.calls) == 1)
    assert ok
    assert engine.calls[0][0] == "第二条"
    assert len(agent.store.list_messages(chat_id="c1", direction="out")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_debounce_disabled_generates_immediately():
    """debounce=0：保持旧行为，每条消息各自生成。"""
    engine = CountingEngine()
    agent = build_agent(make_settings(debounce_seconds=0.0), engine)
    conn = agent.connectors[Platform.WECHAT]

    for text in ("一", "二"):
        await agent.ingest(await conn.inject("c1", "甲", text))

    ok = await wait_until(lambda: len(engine.calls) == 2)
    assert ok
    assert len(agent.store.list_messages(chat_id="c1", direction="out")) == 2
    await agent.stop_all()


@pytest.mark.asyncio
async def test_shutdown_flushes_pending_burst():
    """关闭时尽力冲刷积压连发，草稿不丢失。"""
    engine = CountingEngine()
    agent = build_agent(make_settings(debounce_seconds=30.0), engine)  # 很长，模拟未到期
    conn = agent.connectors[Platform.WECHAT]
    await agent.ingest(await conn.inject("c1", "甲", "还没到期就要关机了"))

    assert len(engine.calls) == 0  # 静默期内，未生成
    await agent.shutdown()  # 冲刷
    assert len(engine.calls) == 1
    drafts = agent.store.list_messages(chat_id="c1", direction="out")
    assert len(drafts) == 1 and drafts[0].status == "pending"


@pytest.mark.asyncio
async def test_different_chats_debounce_independently():
    """不同会话各自计时，互不影响。"""
    engine = CountingEngine()
    agent = build_agent(make_settings(debounce_seconds=0.25), engine)
    wa = agent.connectors[Platform.WECHAT]
    da = agent.connectors[Platform.DINGTALK]

    await agent.ingest(await wa.inject("w1", "甲", "微信消息"))
    await agent.ingest(await da.inject("d1", "乙", "钉钉消息"))

    ok = await wait_until(lambda: len(engine.calls) == 2)
    assert ok
    contents = {c for c, _ in engine.calls}
    assert contents == {"微信消息", "钉钉消息"}
    await agent.stop_all()
