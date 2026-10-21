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
STATUS_SENT = "sent"  # 连接器确认发送成功
STATUS_SENDING = "sending"  # 正在调用连接器
STATUS_FAILED = "failed"  # 连接器明确拒绝/失败，可重试
STATUS_UNKNOWN = "unknown"  # 超时或结果不确定，需核对平台后重试
STATUS_REJECTED = "rejected"  # 人工驳回
STATUS_BLOCKED = "blocked"  # 质量门禁拦截，默认禁止发送（历史消息兼容）
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
    msg_type: str = "text"
    is_group: Optional[bool] = None
    at_me: Optional[bool] = None


class DirectChatRequest(BaseModel):
    """前端“对话测试”：直接调用回复生成器接口。"""

    content: str
    chat_id: str = "web-demo"
    platform: Optional[str] = None  # web | wechat | dingtalk；we默认 web（用全局知识库）


class ApproveRequest(BaseModel):
    """人工确认发送：可携带编辑后的草稿内容（填入输入框后人工修改再发送）。"""

    content: Optional[str] = None
    confirm_unknown: bool = False  # 核对平台后明确允许重试结果不确定的投递


class CandidateSelectRequest(BaseModel):
    """多候选切换：把第 index 条候选设为草稿正文。"""

    index: int


class ContactsRequest(BaseModel):
    """联系人白名单更新（运行时生效；重启后以配置文件/环境变量为准）。"""

    allowed_contacts: list[str] = []


class AgentProfileRequest(BaseModel):
    id: Optional[str] = None
    name: str
    description: str = ""
    instructions: str = ""
    skills: list[str] = Field(default_factory=list)
    engine: str = "pi"
    command: str = ""
    args: str = ""
    workspace: str
    capabilities: list[str] = Field(default_factory=list)
    knowledge_dir: str = ""
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    plan_before_execute: bool = True
    timeout: float = 300
    sync_mode: str = Field(default="background", pattern=r"^(background|wait)$")


class AgentMcpOpenRequest(BaseModel):
    workspace: str


class RegenerateRequest(BaseModel):
    """补充说明后重新生成回复。"""

    extra: str


class SessionResetRequest(BaseModel):
    """清空指定联系人的 Agent 会话。"""

    platform: str
    chat_id: str


class ContactChatRequest(SessionResetRequest):
    """操作员在消息页继续指定联系人的 AI 对话。"""

    content: str = Field(min_length=1, max_length=20000)


class AgentDebugRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class AgentAssignmentRequest(BaseModel):
    platform: str
    chat_id: str
    agent_id: str


class ContactConfigRequest(BaseModel):
    """联系人监听配置（选中监听人时指定）。"""

    platform: str
    chat_id: str
    timeout: float = 300
    group_trigger: str = "at-mention"
    non_text_action: str = "placeholder"
    debounce_seconds: float = 3.0



class ConfigUpdateRequest(BaseModel):
    """配置热更新（部分更新；persist=True 时同步写回配置文件）。"""

    engine: Optional[str] = None  # mock | llm | acp
    acp_mode: Optional[str] = None  # acp | pi
    acp_command: Optional[str] = None
    acp_args: Optional[str] = None
    acp_cwd: Optional[str] = None
    acp_env_name: Optional[str] = None
    acp_env_key: Optional[str] = None
    acp_timeout: Optional[float] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
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
    allowed_contacts: Optional[list[str]] = None  # 全局默认白名单（各平台可单独覆盖）
    wechat_allowed_contacts: Optional[list[str]] = None  # 微信独立白名单
    dingtalk_allowed_contacts: Optional[list[str]] = None  # 钉钉独立白名单
    knowledge_dir: Optional[str] = None  # 全局默认知识库目录（空字符串 = 停用）
    wechat_knowledge_dir: Optional[str] = None  # 微信独立知识库目录（空 = 用全局）
    dingtalk_knowledge_dir: Optional[str] = None  # 钉钉独立知识库目录（空 = 用全局）
    embedding_enabled: Optional[bool] = None  # RAG 语义检索开关（需已配置 embedding.model）
    auto_send: Optional[bool] = None  # 自动发送开关（默认人工确认）
    persist: bool = False  # legacy config.yaml persistence
    persist_to_db: bool = False  # 设置面板保存到 SQLite，与 persist 互斥
