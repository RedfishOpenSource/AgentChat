"""数据模型。"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def now_ms() -> int:
    return int(time.time() * 1000)


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Platform(str, Enum):
    WECHAT = "wechat"
    DINGTALK = "dingtalk"
    WEB = "web"


# ---- 消息类型 ----
_TYPE_LABELS = {
    "image": "图片", "voice": "语音", "video": "视频", "emoji": "动画表情",
    "file": "文件/链接", "location": "位置", "card": "名片", "call": "音视频通话",
    "redpacket": "红包", "unknown": "未知类型",
}


def type_label(msg_type: str) -> str:
    """类型的中文名（用于占位草稿等展示）。"""
    return _TYPE_LABELS.get(msg_type, msg_type)


# ---- 消息方向 ----
DIRECTION_IN = "in"  # 收到的消息
DIRECTION_OUT = "out"  # 助手生成的回复

# ---- 回复状态 ----
STATUS_RECEIVED = "received"  # 收到的消息
STATUS_PENDING = "pending"  # 回复待人工确认
STATUS_SENT = "sent"  # 已发送
STATUS_REJECTED = "rejected"  # 人工驳回
STATUS_BLOCKED = "blocked"  # JEV 质量门禁拦截，默认禁止发送
STATUS_IGNORED = "ignored"  # 按策略忽略（非文本/群聊未@等），不生成草稿


class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: gen_id("msg"))
    platform: Platform
    chat_id: str = "default"
    sender: str = ""
    content: str
    ts: int = Field(default_factory=now_ms)
    direction: str = DIRECTION_IN
    status: str = STATUS_RECEIVED
    reply_to: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)
    # ---- 消息类型与场景（策略用） ----
    msg_type: str = "text"  # text|image|voice|video|emoji|file|location|card|call|system|redpacket|unknown
    at_me: Optional[bool] = None  # 群聊里是否@我（私聊为 None）
    is_group: Optional[bool] = None  # 是否群聊（未知为 None）


class SimulateRequest(BaseModel):
    """模拟一条外部平台新消息（开发/测试用）。"""

    platform: Platform
    chat_id: str = "default"
    sender: str = "对方"
    content: str


class DirectChatRequest(BaseModel):
    """前端“对话测试”：直接调用回复生成器接口。"""

    content: str
    chat_id: str = "web-demo"


class ApproveRequest(BaseModel):
    """人工确认发送：可携带编辑后的草稿内容（填入输入框后人工修改再发送）。"""

    content: Optional[str] = None


class CandidateSelectRequest(BaseModel):
    """多候选切换：把第 index 条候选设为草稿正文。"""

    index: int


class ContactsRequest(BaseModel):
    """联系人白名单更新（运行时生效；重启后以配置文件/环境变量为准）。"""

    allowed_contacts: list[str] = []


class ConfigUpdateRequest(BaseModel):
    """配置热更新（部分更新；persist=True 时同步写回配置文件）。"""

    engine: Optional[str] = None  # mock | llm
    jev_enabled: Optional[bool] = None
    jev_provider: Optional[str] = None  # mock | typesafe
    jev_api_key: Optional[str] = None
    num_candidates: Optional[int] = None
    temperature: Optional[float] = None
    llm_max_retries: Optional[int] = None
    llm_style: Optional[str] = None  # 回复风格预设：default | formal | concise | casual | friendly
    group_trigger: Optional[str] = None  # at-mention | all | listen-only
    non_text_action: Optional[str] = None  # placeholder | skip
    debounce_seconds: Optional[float] = None
    llm_rate_limit_per_chat: Optional[int] = None
    alert_channel: Optional[str] = None  # off | wechat-filehelper | dingtalk-self
    alert_dingtalk_chat_id: Optional[str] = None
    allowed_contacts: Optional[list[str]] = None
    knowledge_dir: Optional[str] = None  # RAG 知识库目录（空字符串 = 停用）
    embedding_enabled: Optional[bool] = None  # RAG 语义检索开关（需已配置 embedding.model）
    auto_send: Optional[bool] = None  # 自动发送开关（默认人工确认）
    persist: bool = False
