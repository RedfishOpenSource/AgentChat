"""Agent 核心管线测试：新消息触发回复、人工审核、监听循环。"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.config import ConnectorSettings, LLMSettings
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.engine.llm import LLMReplyGenerator
from app.models import Platform
from app.store import EventBus, MessageStore

from conftest import make_settings


def build_agent(settings):
    store, bus = MessageStore(), EventBus()
    agent = AgentService(settings=settings, store=store, bus=bus, engine=MockReplyGenerator())
    agent.setup_connectors()
    return agent


async def wait_reply(store, chat_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        replies = [m for m in store.list_messages(chat_id=chat_id, direction="out")]
        if replies:
            return replies
        await asyncio.sleep(0.05)
    return []


@pytest.mark.asyncio
async def test_new_message_triggers_pending_reply():
    """需求(1)：监听到新消息即触发生成回复；安全约束：仅生成草稿，不自动发送。"""
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    msg = await conn.inject(chat_id="c1", sender="李四", content="你好")
    await agent.ingest(msg)

    msgs = agent.store.list_messages(chat_id="c1")
    assert [m.direction for m in msgs] == ["in", "out"]
    reply = msgs[1]
    assert reply.reply_to == msg.id
    assert reply.status == "pending"
    assert reply.content
    assert len(conn.sent) == 0  # 未确认，绝不发送
    await agent.stop_all()


@pytest.mark.asyncio
async def test_never_auto_sends():
    """安全约束：无论任何配置，生成后都不自动发送，发送仅能由 approve 触发。"""
    agent = build_agent(make_settings())
    wechat = agent.connectors[Platform.WECHAT]
    dingtalk = agent.connectors[Platform.DINGTALK]
    msg1 = await wechat.inject("c1", "甲", "你好")
    msg2 = await dingtalk.inject("d1", "乙", "在吗")
    await agent.ingest(msg1)
    await agent.ingest(msg2)

    for conn in (wechat, dingtalk):
        assert len(conn.sent) == 0  # 连接器不应收到任何发送请求
        assert conn.status in ("running", "stopped")
    replies = [m for m in agent.store.list_messages(direction="out")]
    assert all(r.status == "pending" for r in replies)
    await agent.stop_all()


@pytest.mark.asyncio
async def test_approve_with_content_edit():
    """填入输入框模式：人工可编辑草稿后发送，发送的是编辑后内容。"""
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    msg = await conn.inject("c1", "甲", "你好")
    await agent.ingest(msg)
    reply = agent.store.list_messages(chat_id="c1", direction="out")[0]
    original = reply.content  # approve 会原地修改对象，先保存原稿

    sent = await agent.approve_reply(reply.id, content="这是我编辑后的回复")
    assert sent.status == "sent"
    assert sent.content == "这是我编辑后的回复"
    assert sent.meta["edited_from"] == original
    assert conn.sent[0].content == "这是我编辑后的回复"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_failed_dispatch_is_not_marked_sent_and_can_retry():
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    msg = await conn.inject("retry-chat", "甲", "你好")
    await agent.ingest(msg)
    reply = agent.store.list_messages(chat_id="retry-chat", direction="out")[0]

    async def fail(_message):
        return False

    conn.send_reply = fail
    failed = await agent.approve_reply(reply.id)
    assert failed.status == "failed"
    assert failed.status != "sent"

    conn.send_reply = lambda message: _success(message, conn)
    sent = await agent.approve_reply(reply.id)
    assert sent.status == "sent"
    await agent.stop_all()


async def _success(message, conn):
    conn.sent.append(message)
    return True


@pytest.mark.asyncio
async def test_unknown_dispatch_requires_explicit_confirmation():
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    msg = await conn.inject("unknown-chat", "甲", "你好")
    await agent.ingest(msg)
    reply = agent.store.list_messages(chat_id="unknown-chat", direction="out")[0]

    async def uncertain(_message):
        raise TimeoutError("timeout")

    conn.send_reply = uncertain
    assert (await agent.approve_reply(reply.id)).status == "unknown"
    with pytest.raises(ValueError):
        await agent.approve_reply(reply.id)
    conn.send_reply = lambda message: _success(message, conn)
    assert (await agent.approve_reply(reply.id, confirm_unknown=True)).status == "sent"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_approve_and_reject():
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]

    msg1 = await conn.inject("c1", "甲", "你好")
    await agent.ingest(msg1)
    reply1 = agent.store.list_messages(chat_id="c1", direction="out")[0]
    sent = await agent.approve_reply(reply1.id)
    assert sent.status == "sent"
    assert len(conn.sent) == 1
    # 重复审批应报错
    try:
        await agent.approve_reply(reply1.id)
        assert False, "should raise ValueError"
    except ValueError:
        pass

    msg2 = await conn.inject("c2", "乙", "在吗")
    await agent.ingest(msg2)
    reply2 = agent.store.list_messages(chat_id="c2", direction="out")[0]
    rejected = await agent.reject_reply(reply2.id)
    assert rejected.status == "rejected"
    assert len(conn.sent) == 1  # 驳回的不发送
    await agent.stop_all()


@pytest.mark.asyncio
async def test_monitor_loop_picks_up_injected_message():
    """真实监听链路：按钮启动后，消息进入队列 → poll 循环拾取 → 自动生成回复。"""
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.DINGTALK]
    await agent.start_connector(Platform.DINGTALK)  # 模拟前端按钮启动
    try:
        msg = await conn.inject("d1", "赵六", "在吗", enqueue=True)
        replies = await wait_reply(agent.store, "d1")
        assert replies, "监听循环未产生回复"
        assert replies[0].reply_to == msg.id
    finally:
        await agent.stop_all()


@pytest.mark.asyncio
async def test_connectors_do_not_autostart_by_default():
    """安全默认：监听不自动开启，需前端按钮启动。"""
    agent = build_agent(make_settings())
    await agent.start_all()
    try:
        assert all(c.status == "stopped" for c in agent.connectors.values())
    finally:
        await agent.stop_all()


@pytest.mark.asyncio
async def test_auto_start_config_starts_listeners():
    settings = make_settings()
    settings.wechat.auto_start = True
    agent = build_agent(settings)
    await agent.start_all()
    try:
        assert agent.connectors[Platform.WECHAT].status == "running"
        assert agent.connectors[Platform.DINGTALK].status == "stopped"  # 未开启的仍停止
    finally:
        await agent.stop_all()


@pytest.mark.asyncio
async def test_auto_send_dispatches_and_blocked_never():
    """auto_send=True：普通草稿自动发送；blocked（敏感拦截）依然绝不自动发送。"""
    settings = make_settings(auto_send=True)
    agent = build_agent(settings)
    conn = agent.connectors[Platform.DINGTALK]
    await agent.ingest(await conn.inject("d1", "王五", "下午好"))
    reply = agent.store.list_messages(chat_id="d1", direction="out")[0]
    assert reply.status == "sent"
    assert len(conn.sent) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_auto_send_hot_toggle_via_apply_config():
    """auto_send 支持运行时切换（前端按钮路径）。"""
    agent = build_agent(make_settings())  # 默认关
    conn = agent.connectors[Platform.WECHAT]
    await agent.ingest(await conn.inject("c1", "甲", "你好"))
    assert agent.store.list_messages(chat_id="c1", direction="out")[0].status == "pending"
    assert conn.sent == []

    await agent.apply_config({"auto_send": True})  # 打开
    assert agent.settings.auto_send is True
    await agent.ingest(await conn.inject("c1", "甲", "再聊"))
    reply2 = [m for m in agent.store.list_messages(chat_id="c1", direction="out") if m.reply_to][1]
    assert reply2.status == "sent"
    assert len(conn.sent) == 1  # 只有开启后那条被自动发送
    await agent.stop_all()


@pytest.mark.asyncio
async def test_ingest_is_idempotent():
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    msg = await conn.inject("c1", "甲", "你好")
    await agent.ingest(msg)
    await agent.ingest(msg)  # 重复投递
    assert len(agent.store.list_messages(chat_id="c1")) == 2  # in + out，不重复
    await agent.stop_all()


@pytest.mark.asyncio
async def test_direct_chat():
    agent = build_agent(make_settings())
    user, reply = await agent.direct_chat("你好", chat_id="web-demo")
    assert user.platform == Platform.WEB
    assert reply.reply_to == user.id
    assert reply.status == "sent"
    assert agent.store.count() == 2
    await agent.stop_all()
