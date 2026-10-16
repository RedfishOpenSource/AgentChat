"""消息场景策略测试：群聊触发、非文本处理、系统消息。"""
from __future__ import annotations

import pytest

from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.models import ChatMessage, Platform, STATUS_IGNORED
from app.store import EventBus, MessageStore

from conftest import make_settings


def build_agent(settings):
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(),
        engine=MockReplyGenerator(),
    )
    agent.setup_connectors()
    return agent


def msg(platform=Platform.WECHAT, chat_id="c1", sender="张三", content="你好",
        msg_type="text", at_me=None, is_group=None) -> ChatMessage:
    return ChatMessage(
        platform=platform, chat_id=chat_id, sender=sender, content=content,
        msg_type=msg_type, at_me=at_me, is_group=is_group,
    )


def drafts(agent, chat_id):
    return agent.store.list_messages(chat_id=chat_id, direction="out")


@pytest.mark.asyncio
async def test_private_text_normal():
    agent = build_agent(make_settings())
    await agent.ingest(msg())
    assert len(drafts(agent, "c1")) == 1
    assert agent.store.list_messages(chat_id="c1")[0].status == "received"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_group_at_mention_policy():
    """at-mention（默认）：群聊仅@我触发；未@被忽略。"""
    agent = build_agent(make_settings())
    # 未@我 → 忽略
    await agent.ingest(msg(chat_id="群1", content="大家注意", at_me=False, is_group=True))
    assert drafts(agent, "群1") == []
    ignored = agent.store.list_messages(chat_id="群1")[0]
    assert ignored.status == STATUS_IGNORED
    assert "未@我" in ignored.meta["ignore_reason"]
    # @我 → 触发
    await agent.ingest(msg(chat_id="群1", content="@赵丽阳 开会", at_me=True, is_group=True))
    assert len(drafts(agent, "群1")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_group_listen_only_policy():
    agent = build_agent(make_settings(group_trigger="listen-only"))
    await agent.ingest(msg(chat_id="群2", content="@赵丽阳 在吗", at_me=True, is_group=True))
    assert drafts(agent, "群2") == []
    assert agent.store.list_messages(chat_id="群2")[0].status == STATUS_IGNORED
    await agent.stop_all()


@pytest.mark.asyncio
async def test_group_all_policy():
    agent = build_agent(make_settings(group_trigger="all"))
    await agent.ingest(msg(chat_id="群3", content="随便说说", at_me=False, is_group=True))
    assert len(drafts(agent, "群3")) == 1
    await agent.stop_all()


@pytest.mark.asyncio
async def test_non_text_placeholder():
    """非文本默认生成占位草稿（不走引擎）。"""
    agent = build_agent(make_settings())
    await agent.ingest(msg(content="[图片]", msg_type="image"))
    ds = drafts(agent, "c1")
    assert len(ds) == 1
    assert "图片" in ds[0].content
    assert ds[0].meta["placeholder"] is True
    assert ds[0].status == "pending"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_non_text_skip():
    agent = build_agent(make_settings(non_text_action="skip"))
    await agent.ingest(msg(content="[语音]", msg_type="voice"))
    assert drafts(agent, "c1") == []
    assert agent.store.list_messages(chat_id="c1")[0].status == STATUS_IGNORED
    await agent.stop_all()


@pytest.mark.asyncio
async def test_system_message_always_ignored():
    agent = build_agent(make_settings(group_trigger="all"))
    await agent.ingest(msg(content="张三 撤回了一条消息", msg_type="system", is_group=True))
    assert drafts(agent, "c1") == []
    assert agent.store.list_messages(chat_id="c1")[0].status == STATUS_IGNORED
    await agent.stop_all()
