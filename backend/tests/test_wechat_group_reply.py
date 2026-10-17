"""微信群聊引用+@回复（quote-at）：草稿前缀/元数据、候选切换、连接器发送路径。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.connectors.wechat import WeChatReplicaConnector
from app.config import ConnectorSettings
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.models import ChatMessage, Platform, STATUS_PENDING
from app.store import EventBus, MessageStore

from conftest import make_settings


def build_agent(**overrides):
    settings = make_settings(**overrides)
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(),
        engine=MockReplyGenerator(),
    )
    agent.setup_connectors()
    return agent


async def ingest_group(agent, content="@赵丽阳 开会", sender="张三"):
    msg = ChatMessage(
        platform=Platform.WECHAT, chat_id="项目群", sender=sender, content=content,
        at_me=True, is_group=True,
    )
    await agent.ingest(msg)
    return msg


def drafts(agent, chat_id):
    return agent.store.list_messages(chat_id=chat_id, direction="out")


@pytest.mark.asyncio
async def test_group_draft_has_at_prefix_and_quote_meta():
    """微信群聊回复：正文带「@触发者 」前缀，meta 携带引用目标。"""
    agent = build_agent()
    await ingest_group(agent, sender="张三")
    ds = drafts(agent, "项目群")
    assert len(ds) == 1
    assert ds[0].content.startswith("@张三 ")
    wr = ds[0].meta["wechat_reply"]
    assert wr["at_sender"] == "张三"
    assert wr["quote_text"].startswith("@赵丽阳")
    await agent.stop_all()


@pytest.mark.asyncio
async def test_private_draft_untouched():
    """私聊回复不带 @ 前缀、无 wechat_reply 元数据。"""
    agent = build_agent()
    await agent.ingest(ChatMessage(platform=Platform.WECHAT, chat_id="张三",
                                   sender="张三", content="你好"))
    ds = drafts(agent, "张三")
    assert len(ds) == 1
    assert not ds[0].content.startswith("@")
    assert "wechat_reply" not in ds[0].meta
    await agent.stop_all()


@pytest.mark.asyncio
async def test_group_reply_plain_mode_disables_prefix():
    """group_reply=plain：群聊回复直发，不带前缀与元数据。"""
    agent = build_agent(
        wechat=ConnectorSettings(enabled=True, mode="mock", group_reply="plain"))
    await ingest_group(agent)
    ds = drafts(agent, "项目群")
    assert len(ds) == 1
    assert not ds[0].content.startswith("@")
    assert "wechat_reply" not in ds[0].meta
    await agent.stop_all()


@pytest.mark.asyncio
async def test_group_placeholder_draft_also_gets_prefix():
    """群聊非文本占位草稿同样带 @ 前缀与引用元数据。"""
    agent = build_agent()
    await agent.ingest(ChatMessage(platform=Platform.WECHAT, chat_id="项目群",
                                   sender="李四", content="[图片]",
                                   msg_type="image", at_me=True, is_group=True))
    ds = drafts(agent, "项目群")
    assert len(ds) == 1
    assert ds[0].content.startswith("@李四 ")
    assert ds[0].meta["wechat_reply"]["at_sender"] == "李四"
    await agent.stop_all()


@pytest.mark.asyncio
async def test_select_candidate_keeps_at_prefix():
    """切换候选后仍保留 @ 前缀（候选原文不含前缀）。"""
    agent = build_agent()
    await ingest_group(agent, sender="王五")
    draft = drafts(agent, "项目群")[0]
    assert draft.meta["candidates"], "mock 引擎自带多候选"
    updated = await agent.select_candidate(draft.id, 1)
    assert updated.content.startswith("@王五 ")
    await agent.stop_all()


@pytest.mark.asyncio
async def test_prefix_not_duplicated():
    """正文已带 @ 前缀时不重复拼接。"""
    agent = build_agent()
    msg = await ingest_group(agent, content="@赵丽阳 你好", sender="张三")
    draft = drafts(agent, "项目群")[0]
    # 直接再次应用路由，不应出现「@张三 @张三 」
    agent._apply_reply_routing(draft, msg)
    assert not draft.content.startswith("@张三 @张三")
    await agent.stop_all()


# ---- 连接器发送路径（Fake GUI，不依赖真实微信） ----

class FakeGUI:
    def __init__(self, quote_fail: bool = False):
        self.calls: list[tuple] = []
        self.quote_fail = quote_fail

    def quote_msg(self, text, who=None, target_text=None):
        self.calls.append(("quote", text, who, target_text))
        if self.quote_fail:
            return SimpleNamespace(is_success=False, message="未找到要引用的消息")
        return SimpleNamespace(is_success=True, message="ok")

    def send_msg(self, text, who=None):
        self.calls.append(("send", text, who))
        return SimpleNamespace(is_success=True, message="ok")


def build_connector() -> WeChatReplicaConnector:
    return WeChatReplicaConnector(
        ConnectorSettings(enabled=True, mode="replica"), on_messages=lambda msgs: None,
    )


@pytest.mark.asyncio
async def test_connector_group_reply_uses_quote_msg():
    """群聊回复走 quote_msg，target_text 取自 meta.wechat_reply.quote_text。"""
    conn = build_connector()
    conn._gui = FakeGUI()
    reply = ChatMessage(
        platform=Platform.WECHAT, chat_id="项目群", sender="assistant",
        content="@张三 收到，马上处理", direction="out", status="sent",
        meta={"wechat_reply": {"at_sender": "张三", "quote_text": "@赵丽阳 开会"}},
    )
    await conn.send_reply(reply)
    assert conn._gui.calls == [("quote", "@张三 收到，马上处理", "项目群", "@赵丽阳 开会")]


@pytest.mark.asyncio
async def test_connector_quote_failure_falls_back_to_plain():
    """引用失败（如 OCR 未定位到原消息）自动回退直发，回复不丢失。"""
    conn = build_connector()
    conn._gui = FakeGUI(quote_fail=True)
    reply = ChatMessage(
        platform=Platform.WECHAT, chat_id="项目群", sender="assistant",
        content="@张三 收到", direction="out", status="sent",
        meta={"wechat_reply": {"at_sender": "张三", "quote_text": "原文"}},
    )
    await conn.send_reply(reply)
    kinds = [c[0] for c in conn._gui.calls]
    assert kinds == ["quote", "send"]
    assert conn._gui.calls[1][1] == "@张三 收到"  # 回退发送内容一致，不重复发引用


@pytest.mark.asyncio
async def test_connector_plain_reply_without_meta():
    """无私聊元数据/旧草稿：直发，行为与历史版本一致。"""
    conn = build_connector()
    conn._gui = FakeGUI()
    reply = ChatMessage(platform=Platform.WECHAT, chat_id="文件传输助手",
                        sender="assistant", content="你好", direction="out", status="sent")
    await conn.send_reply(reply)
    assert conn._gui.calls == [("send", "你好", "文件传输助手")]


@pytest.mark.asyncio
async def test_connector_quote_exception_falls_back_to_plain():
    """quote_msg 抛异常同样回退直发。"""
    conn = build_connector()

    class BoomGUI(FakeGUI):
        def quote_msg(self, text, who=None, target_text=None):
            raise RuntimeError("窗口不可见")

    conn._gui = BoomGUI()
    reply = ChatMessage(
        platform=Platform.WECHAT, chat_id="项目群", sender="assistant",
        content="@张三 收到", direction="out", status="sent",
        meta={"wechat_reply": {"at_sender": "张三", "quote_text": "原文"}},
    )
    await conn.send_reply(reply)
    assert [c[0] for c in conn._gui.calls] == ["send"]
