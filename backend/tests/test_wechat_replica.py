"""微信 replica 连接器测试：消息解析、类型映射、群聊/@我 检测（不依赖本机微信）。"""
from __future__ import annotations

from app.connectors.wechat import _parse_replica_msg
from app.models import Platform

MSG = {
    "sender_id": 12345,
    "content": "你好，测试消息",
    "type": "文本",
    "create_time": 1792120000.5,
    "username": "wxid_test",
}


def test_parse_replica_msg_normal():
    cm = _parse_replica_msg(MSG, "文件传输助手", "wxid_test", lambda sid: "张三")
    assert cm is not None
    assert cm.platform == Platform.WECHAT
    assert cm.chat_id == "文件传输助手"
    assert cm.sender == "张三"
    assert cm.content == "你好，测试消息"
    assert cm.ts == 1792120000500  # 秒转毫秒
    assert cm.msg_type == "text"
    assert cm.is_group is False
    assert cm.at_me is None  # 私聊无 @ 概念
    assert cm.meta["replica"] == {"username": "wxid_test", "sender_id": "12345",
                                  "sender_username": "", "raw_type": "文本"}


def test_parse_replica_msg_filters_self_echo():
    """回声过滤已移至 _is_echo（文件传输助手自消息需放行）。"""
    from app.connectors.wechat import _is_echo

    echo = {**MSG, "sender_username": "wxid_self", "content": "收到你的消息啦（自动回复）"}
    # 普通会话：自己发的 → 丢弃
    assert _is_echo(echo, "c", "wxid_self", []) is True
    # 他人消息 → 放行
    other = {**MSG, "sender_username": "wxid_friend"}
    assert _is_echo(other, "c", "wxid_self", []) is False
    # 文件传输助手：用户手打的备忘 → 放行
    note = {**MSG, "sender_username": "wxid_self", "content": "你是谁", "username": "filehelper"}
    assert _is_echo(note, "filehelper", "wxid_self", []) is False
    # 文件传输助手：与最近已发送回复一致 → 回声，丢弃
    import time
    recent = [("收到你的消息啦（自动回复）", time.monotonic())]
    assert _is_echo(echo, "filehelper", "wxid_self", recent) is True


def test_parse_replica_msg_group_and_at_me():
    """群聊消息：username 以 @chatroom 结尾；@昵称 → at_me=True。"""
    msg = {**MSG, "username": "12345@chatroom", "content": "@赵丽阳  开会啦"}
    cm = _parse_replica_msg(msg, "项目群", "12345@chatroom", lambda s: "赵六", self_nick="赵丽阳")
    assert cm.is_group is True
    assert cm.at_me is True
    assert cm.msg_type == "text"

    # 群聊但未@我
    msg2 = {**msg, "content": "大家注意一下"}
    cm2 = _parse_replica_msg(msg2, "项目群", "12345@chatroom", lambda s: "赵六", self_nick="赵丽阳")
    assert cm2.is_group is True
    assert cm2.at_me is False


def test_parse_replica_msg_type_mapping():
    assert _parse_replica_msg({**MSG, "type": "图片"}, "c", "u", lambda s: "").msg_type == "image"
    assert _parse_replica_msg({**MSG, "type": "语音"}, "c", "u", lambda s: "").msg_type == "voice"
    assert _parse_replica_msg({**MSG, "type": "视频"}, "c", "u", lambda s: "").msg_type == "video"
    assert _parse_replica_msg({**MSG, "type": "动画表情"}, "c", "u", lambda s: "").msg_type == "emoji"
    assert _parse_replica_msg({**MSG, "type": "文件/链接/卡片"}, "c", "u", lambda s: "").msg_type == "file"
    assert _parse_replica_msg({**MSG, "type": "系统消息"}, "c", "u", lambda s: "").msg_type == "system"
    # 未知类型按 text 尽力传递，不静默丢弃
    assert _parse_replica_msg({**MSG, "type": "神秘类型"}, "c", "u", lambda s: "").msg_type == "text"


def test_parse_replica_msg_empty_content_dropped():
    assert _parse_replica_msg({**MSG, "content": "  "}, "c", "u", lambda s: "") is None


def test_parse_replica_msg_sender_fallback_and_bad_ts():
    cm = _parse_replica_msg({**MSG, "sender_id": 7, "create_time": "bad"}, "c", "u", lambda s: "")
    assert cm is not None
    assert cm.sender == "用户7"
    assert cm.ts > 0
