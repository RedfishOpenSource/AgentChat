"""DWS 钉钉连接器测试：事件解析、回复命令构造、假 CLI 端到端。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.config import ConnectorSettings
from app.connectors.dingtalk import DingTalkDwsConnector, _build_reply_cmd, _parse_event_line
from app.models import DIRECTION_OUT, Platform

ENVELOPE_LINE = json.dumps(
    {
        "type": "event",
        "data": json.dumps(
            {
                "conversationId": "cidTEST",
                "senderNick": "刘晨",
                "text": {"content": "  你好，帮我看看"},
                "msgId": "msgT1",
                "conversationTitle": "调试群",
                "createAt": 1792060000,
            },
            ensure_ascii=False,
        ),
    },
    ensure_ascii=False,
)

FLAT_LINE = json.dumps(
    {
        "openConversationId": "cidF2",
        "sender": "李四",
        "content": "直接字段消息",
        "messageId": "msgF1",
        "received_at_unix_ms": 1792060000123,
    },
    ensure_ascii=False,
)


# ---------------- 事件解析 ----------------
def test_parse_envelope_format():
    msg = _parse_event_line(ENVELOPE_LINE)
    assert msg is not None
    assert msg.platform == Platform.DINGTALK
    assert msg.chat_id == "调试群"
    assert msg.sender == "刘晨"
    assert msg.content == "你好，帮我看看"  # 去除 @ 后前导空格
    assert msg.meta["dws"] == {"message_id": "msgT1", "conversation_id": "cidTEST", "title": "调试群"}
    assert msg.ts == 1792060000000  # 秒级时间戳已转毫秒


def test_parse_flat_format():
    msg = _parse_event_line(FLAT_LINE)
    assert msg is not None
    assert msg.chat_id == "cidF2"  # 无会话名时用会话 ID
    assert msg.sender == "李四"
    assert msg.content == "直接字段消息"
    assert msg.ts == 1792060000123
    assert msg.meta["dws"]["conversation_id"] == "cidF2"


def test_parse_real_dws_event():
    """真实 DWS 事件样本（flatten snake_case）。"""
    line = json.dumps(
        {
            "type": "user_im_message_receive_at",
            "event_id": "ade03cf49e5c4508b631c518bcda91e9",
            "timestamp": 1792121116629,
            "subscribe_id": "subId-x",
            "message_id": "msg+0Ww1T8iPgNN5wfQr2Tkrg==",
            "conversation_id": "cidPlSRhLXedzevWuzFCfVs2w==",
            "sender": "赵丽阳",
            "sender_open_dingtalk_id": "Das4F1E6S3h3niSsJTCljtYrVf7iPohtiPuw",
            "content": "@赵丽阳  ChatAgent字段采样",
            "create_time": "2026-10-16 11:25:16",
            "event_time": 1792121116051,
        },
        ensure_ascii=False,
    )
    msg = _parse_event_line(line)
    assert msg is not None
    assert msg.chat_id == "cidPlSRhLXedzevWuzFCfVs2w=="
    assert msg.sender == "赵丽阳"
    assert msg.content == "ChatAgent字段采样"  # @我 前缀已剥离
    assert msg.is_group is True
    assert msg.at_me is True
    assert msg.meta["dws"]["message_id"] == "msg+0Ww1T8iPgNN5wfQr2Tkrg=="
    assert _parse_event_line(line).id == msg.id  # 外部消息重放时稳定去重
    assert msg.ts == 1792121116051


def test_parse_filters_non_json_lines():
    assert _parse_event_line("[event] ready event_key=user_im_message_receive_at") is None
    assert _parse_event_line("") is None
    assert _parse_event_line("not json at all") is None
    assert _parse_event_line("{broken json") is None


def test_parse_rejects_empty_content():
    line = json.dumps({"data": json.dumps({"senderNick": "x", "text": {"content": "  "}})})
    assert _parse_event_line(line) is None


def test_parse_fallback_sender_and_ts():
    line = json.dumps({"data": json.dumps({"text": {"content": "hi"}})})
    msg = _parse_event_line(line)
    assert msg is not None and msg.sender == "" and msg.ts > 0


# ---------------- 回复命令构造 ----------------
def test_build_reply_cmd_quote():
    src = {"message_id": "msgT1", "conversation_id": "cidTEST"}
    cmd = _build_reply_cmd("回复内容", src, dws_bin="dws", reply_mode="quote")
    assert cmd[:3] == ["dws", "chat", "+messages-reply"]
    assert "--ref-msg-id" in cmd and "msgT1" in cmd
    assert "--group" in cmd and "cidTEST" in cmd
    assert "--content" in cmd and "回复内容" in cmd
    assert "--yes" in cmd and "--idempotency-key" in cmd


def test_build_reply_cmd_falls_back_to_plain_without_ref():
    src = {"conversation_id": "cidTEST"}
    cmd = _build_reply_cmd("内容", src, reply_mode="quote")
    assert cmd[2] == "+messages-send"
    assert "--chat-id" in cmd and "cidTEST" in cmd


def test_build_reply_cmd_plain_mode():
    src = {"message_id": "msgT1"}
    cmd = _build_reply_cmd("内容", src, reply_mode="plain")
    assert cmd[2] == "+messages-send"


# ---------------- 假 CLI 端到端 ----------------
FAKE_DWS_BAT = r"""@echo off
if "%1"=="event" (
  echo [event] ready event_key=user_im_message_receive_at
  echo {"type":"event","data":"{\"conversationId\":\"cidTEST\",\"senderNick\":\"LiuChen\",\"text\":{\"content\":\"  Hello dws\"},\"msgId\":\"msgT1\",\"conversationTitle\":\"TestRoom\",\"createAt\":1792060000}"}
  ping -n 31 127.0.0.1 > nul
) else (
  >>"%~dp0calls.log" echo %*
  exit /b 0
)
"""


@pytest.mark.asyncio
async def test_dws_connector_end_to_end_with_fake_cli(tmp_path):
    fake = tmp_path / "fake-dws.bat"
    fake.write_text(FAKE_DWS_BAT, encoding="ascii")

    cs = ConnectorSettings(
        enabled=True, mode="dws", dws_bin=str(fake),
        listen_kind="at-me", poll_interval=0.02,
    )
    from app.store import EventBus, MessageStore
    from app.core.agent import AgentService
    from app.engine import MockReplyGenerator
    from tests.conftest import make_settings

    settings = make_settings(dingtalk=cs)
    store, bus = MessageStore(), EventBus()
    agent = AgentService(settings=settings, store=store, bus=bus, engine=MockReplyGenerator())
    agent.setup_connectors()
    conn = agent.connectors[Platform.DINGTALK]
    assert isinstance(conn, DingTalkDwsConnector)

    await agent.start_connector(Platform.DINGTALK)  # 模拟按钮启动监听
    try:
        # 等待假 CLI 事件被监听循环拾取并生成回复
        replies = []
        for _ in range(120):
            replies = store.list_messages(chat_id="TestRoom", direction="out")
            if replies:
                break
            await asyncio.sleep(0.05)
        assert replies, "事件流消息未进入管线"
        inbound = store.list_messages(chat_id="TestRoom", direction="in")[0]
        assert inbound.content == "Hello dws"
        assert inbound.meta["dws"]["message_id"] == "msgT1"
        assert replies[0].status == "pending"

        # 审批发送 → 假 CLI 收到回复命令，参数正确
        await agent.approve_reply(replies[0].id)
        for _ in range(40):
            log_file = tmp_path / "calls.log"
            if log_file.exists():
                break
            await asyncio.sleep(0.05)
        log_text = (tmp_path / "calls.log").read_text(encoding="utf-8", errors="ignore")
        assert "+messages-reply" in log_text, log_text
        assert "msgT1" in log_text and "cidTEST" in log_text
        assert "--yes" in log_text
    finally:
        await agent.shutdown()
