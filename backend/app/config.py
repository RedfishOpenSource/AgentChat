"""配置模块：默认值 <- config.yaml <- 环境变量（后者覆盖前者）。"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_ENV_PREFIX = "CHATAGENT_"
_DEFAULT_CONFIG_FILENAMES = ("config.yaml",)  # config.example.yaml 只是模板，不自动加载


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class LLMSettings:
    """OpenAI 兼容 Chat Completions 配置（默认 DeepSeek）。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    timeout: float = 60.0
    num_candidates: int = 1
    max_history: int = 12
    max_retries: int = 2  # 失败重试次数（对超时/5xx/429）；总尝试 = 1 + max_retries
    retry_backoff: float = 1.0  # 重试退避基数（秒），指数递增
    style: str = "default"  # 回复风格预设：default | formal | concise | casual | friendly
    # ---- 图片理解（可选）：vision_model 为空 = 关闭；base_url/api_key 留空则复用上方对话模型配置 ----
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""


@dataclass
class EmbeddingSettings:
    """RAG 语义检索的 Embedding 配置（可选，OpenAI 兼容 /embeddings 接口）。

    enabled=False（默认）时 RAG 仅用零成本词面检索；开启后词面+向量按 RRF 混合排序，
    调用失败自动降级回词面检索。base_url/api_key 留空复用 llm 配置（同一服务商时零额外配置）。
    """

    enabled: bool = False
    base_url: str = ""  # 空 = 复用 llm.base_url
    api_key: str = ""  # 空 = 复用 llm.api_key
    model: str = ""  # 如 bge-m3 / text-embedding-v3 / embedding-3
    timeout: float = 15.0
    batch_size: int = 16  # 分块向量批量嵌入的批大小


@dataclass
class JEVSettings:
    """JEV 结构化判断配置（参考 TypeSafe JEV：预判 + 复审排序，可选）。"""

    enabled: bool = False
    provider: str = "mock"  # mock | typesafe
    api_key: str = ""
    base_url: str = "https://api.typesafe.ai"
    model: str = "jev-latest"
    rewrite_on_fail: bool = True


@dataclass
class ConnectorSettings:
    """聊天平台连接器配置。"""

    enabled: bool = True
    mode: str = "mock"  # wechat: mock|replica|wxauto; dingtalk: mock|dws|cli
    auto_start: bool = False  # 服务启动时是否自动开始监听（默认关，由前端按钮控制）
    poll_interval: float = 1.0
    command: str = ""  # dingtalk cli: 拉取新消息的命令
    reply_command: str = ""  # dingtalk cli: 发送回复命令模板 {chat_id} {sender} {content}
    listen_chats: list[str] = field(default_factory=list)  # 微信/钉钉 dws: 要监听的会话名（昵称/备注/群名）
    listen_kind: str = "at-me"  # dingtalk dws: at-me | all-direct | all-group | group
    dws_bin: str = "dws"  # dingtalk dws: DWS CLI 可执行文件路径
    reply_mode: str = "quote"  # dingtalk dws: quote(引用回复) | plain(直接发送)
    group_reply: str = "quote-at"  # wechat replica 群聊回复: quote-at(引用原消息+文本@触发者) | plain(直发)


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    engine: str = "mock"  # mock | llm
    # 发送模式：auto_send=False（默认）仅生成草稿，人工确认后发送；True 时自动发送（前端按钮可切）
    auto_send: bool = False
    # 安全底线：blocked（敏感拦截）的草稿任何情况下都不会被自动发送。
    max_store: int = 2000
    debounce_seconds: float = 3.0  # 连发合并：静默 N 秒后才生成（同一会话新消息会重置计时）；0 = 关闭
    db_path: str = "chatagent.db"  # SQLite 落盘路径；:memory: 表示不落盘（测试用）
    allowed_ips: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])  # 访问白名单，["*"] 全放行
    # ---- 消息场景策略 ----
    group_trigger: str = "at-mention"  # 群聊触发：at-mention(只回@我) | all(全部) | listen-only(只监听不回复)
    non_text_action: str = "placeholder"  # 非文本消息：placeholder(生成提示草稿) | skip(直接忽略)
    llm_rate_limit_per_chat: int = 10  # 每会话每分钟最多生成次数（0 = 不限），超出忽略并记录原因
    allowed_contacts: list[str] = field(default_factory=list)  # 联系人白名单（会话名）；空 = 允许全部；["*"] = 全部
    knowledge_dir: str = ""  # RAG 知识库目录（.md/.txt）；空 = 未启用；前端目录选择器设置
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)  # RAG 语义检索（可选）
    # ---- 监控告警（告警消息发给你自己，默认关闭） ----
    alert_channel: str = "off"  # off | wechat-filehelper | dingtalk-self
    alert_dingtalk_chat_id: str = ""  # dingtalk-self 目标会话 ID
    alert_min_interval: float = 60.0  # 两次通知的最小间隔（秒）
    alert_check_interval: float = 30.0  # 连接器健康巡检间隔（秒）
    llm_failure_threshold: int = 3  # LLM 连续失败 N 次后告警
    config_file: str = "config.yaml"  # 启动时实际加载的配置文件（热更新落盘目标）
    # ---- 部署 ----
    log_file: str = "logs/chatagent.log"  # 日志文件（按天轮转，保留 14 天）；空 = 仅控制台
    static_dir: str = ""  # 前端构建产物目录；空 = 自动探测 ../frontend/dist，不存在则仅服务 API
    llm: LLMSettings = field(default_factory=LLMSettings)
    jev: JEVSettings = field(default_factory=JEVSettings)
    wechat: ConnectorSettings = field(default_factory=ConnectorSettings)
    dingtalk: ConnectorSettings = field(default_factory=ConnectorSettings)


def _find_config_file(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for name in _DEFAULT_CONFIG_FILENAMES:
        for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
            p = base / name
            if p.exists():
                return p
    return None


def _apply_connector(data: dict[str, Any], cs: ConnectorSettings) -> None:
    for key in ("enabled", "mode", "auto_start", "poll_interval", "command", "reply_command",
                "listen_chats", "listen_kind", "dws_bin", "reply_mode", "group_reply"):
        if key in data:
            setattr(cs, key, data[key])


def load_settings(config_path: str | None = None) -> Settings:
    s = Settings()
    cfg_file = _find_config_file(config_path)
    data: dict[str, Any] = {}
    if cfg_file is not None and yaml is not None:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}

    if isinstance(data.get("llm"), dict):
        for key in ("api_key", "base_url", "model", "temperature", "timeout", "num_candidates",
                    "max_history", "max_retries", "retry_backoff", "style",
                    "vision_model", "vision_base_url", "vision_api_key"):
            if key in data["llm"]:
                setattr(s.llm, key, data["llm"][key])
    if isinstance(data.get("embedding"), dict):
        for key in ("enabled", "base_url", "api_key", "model", "timeout", "batch_size"):
            if key in data["embedding"]:
                setattr(s.embedding, key, data["embedding"][key])
    if isinstance(data.get("jev"), dict):
        for key in ("enabled", "provider", "api_key", "base_url", "model", "rewrite_on_fail"):
            if key in data["jev"]:
                setattr(s.jev, key, data["jev"][key])
    for name in ("wechat", "dingtalk"):
        if isinstance(data.get(name), dict):
            _apply_connector(data[name], getattr(s, name))
    for key in ("engine", "max_store", "host", "port", "db_path", "debounce_seconds",
                "group_trigger", "non_text_action", "alert_channel", "alert_dingtalk_chat_id",
                "alert_min_interval", "alert_check_interval", "llm_failure_threshold",
                "log_file", "static_dir", "auto_send", "knowledge_dir"):
        if key in data:
            setattr(s, key, data[key])
    for list_key in ("allowed_ips", "allowed_contacts"):
        if isinstance(data.get(list_key), list):
            setattr(s, list_key, [str(x) for x in data[list_key]])
    if cfg_file is not None:
        # 热更新写回目标永远是 config.yaml；仅存在示例文件时不把它当写回目标
        s.config_file = str(cfg_file) if cfg_file.name == "config.yaml" else "config.yaml"
    # YAML 1.1 会把 off/on 解析为布尔值，归一化回字符串
    if isinstance(s.alert_channel, bool):
        s.alert_channel = "on" if s.alert_channel else "off"

    # ---- 环境变量覆盖 ----
    env = os.environ.get
    s.engine = env(_ENV_PREFIX + "ENGINE", s.engine)
    s.host = env(_ENV_PREFIX + "HOST", s.host)
    s.port = int(env(_ENV_PREFIX + "PORT", s.port))
    s.db_path = env(_ENV_PREFIX + "DB_PATH", s.db_path)
    _deb = env(_ENV_PREFIX + "DEBOUNCE_SECONDS")
    if _deb is not None:
        try:
            s.debounce_seconds = max(0.0, float(_deb))
        except ValueError:
            pass
    _ips = env(_ENV_PREFIX + "ALLOWED_IPS")
    if _ips is not None:
        s.allowed_ips = [x.strip() for x in _ips.split(",") if x.strip()]
    s.group_trigger = env(_ENV_PREFIX + "GROUP_TRIGGER", s.group_trigger)
    s.non_text_action = env(_ENV_PREFIX + "NON_TEXT_ACTION", s.non_text_action)
    _ac = env(_ENV_PREFIX + "ALLOWED_CONTACTS")
    if _ac is not None:
        s.allowed_contacts = [x.strip() for x in _ac.split(",") if x.strip()]
    s.alert_channel = env(_ENV_PREFIX + "ALERT_CHANNEL", s.alert_channel)
    s.log_file = env(_ENV_PREFIX + "LOG_FILE", s.log_file)
    s.static_dir = env(_ENV_PREFIX + "STATIC_DIR", s.static_dir)
    _rl = env(_ENV_PREFIX + "LLM_RATE_LIMIT")
    if _rl is not None:
        try:
            s.llm_rate_limit_per_chat = int(_rl)
        except ValueError:
            pass
    _mr = env(_ENV_PREFIX + "LLM_MAX_RETRIES")
    if _mr is not None:
        try:
            s.llm.max_retries = max(0, int(_mr))
        except ValueError:
            pass

    s.auto_send = _as_bool(env(_ENV_PREFIX + "AUTO_SEND"), s.auto_send)

    s.llm.api_key = env(_ENV_PREFIX + "LLM_API_KEY", s.llm.api_key)
    s.llm.base_url = env(_ENV_PREFIX + "LLM_BASE_URL", s.llm.base_url)
    s.llm.model = env(_ENV_PREFIX + "LLM_MODEL", s.llm.model)
    s.llm.style = env(_ENV_PREFIX + "LLM_STYLE", s.llm.style)
    s.llm.vision_model = env(_ENV_PREFIX + "LLM_VISION_MODEL", s.llm.vision_model)
    s.llm.vision_base_url = env(_ENV_PREFIX + "LLM_VISION_BASE_URL", s.llm.vision_base_url)
    s.llm.vision_api_key = env(_ENV_PREFIX + "LLM_VISION_API_KEY", s.llm.vision_api_key)
    s.knowledge_dir = env(_ENV_PREFIX + "KNOWLEDGE_DIR", s.knowledge_dir)

    s.embedding.enabled = _as_bool(env(_ENV_PREFIX + "EMBEDDING_ENABLED"), s.embedding.enabled)
    s.embedding.base_url = env(_ENV_PREFIX + "EMBEDDING_BASE_URL", s.embedding.base_url)
    s.embedding.api_key = env(_ENV_PREFIX + "EMBEDDING_API_KEY", s.embedding.api_key)
    s.embedding.model = env(_ENV_PREFIX + "EMBEDDING_MODEL", s.embedding.model)

    s.jev.enabled = _as_bool(env(_ENV_PREFIX + "JEV_ENABLED"), s.jev.enabled)
    s.jev.provider = env(_ENV_PREFIX + "JEV_PROVIDER", s.jev.provider)
    s.jev.api_key = env(_ENV_PREFIX + "JEV_API_KEY", s.jev.api_key) or env(
        _ENV_PREFIX + "TYPESAFE_API_KEY", s.jev.api_key
    )
    s.jev.base_url = env(_ENV_PREFIX + "JEV_BASE_URL", s.jev.base_url)

    s.wechat.mode = env(_ENV_PREFIX + "WECHAT_MODE", s.wechat.mode)
    s.wechat.group_reply = env(_ENV_PREFIX + "WECHAT_GROUP_REPLY", s.wechat.group_reply)
    s.dingtalk.mode = env(_ENV_PREFIX + "DINGTALK_MODE", s.dingtalk.mode)
    s.dingtalk.command = env(_ENV_PREFIX + "DINGTALK_COMMAND", s.dingtalk.command)
    s.dingtalk.reply_command = env(_ENV_PREFIX + "DINGTALK_REPLY_COMMAND", s.dingtalk.reply_command)
    s.dingtalk.listen_kind = env(_ENV_PREFIX + "DINGTALK_LISTEN_KIND", s.dingtalk.listen_kind)
    s.dingtalk.dws_bin = env(_ENV_PREFIX + "DINGTALK_DWS_BIN", s.dingtalk.dws_bin)
    s.dingtalk.reply_mode = env(_ENV_PREFIX + "DINGTALK_REPLY_MODE", s.dingtalk.reply_mode)
    return s


def save_config_partial(updates: dict[str, Any], path: str) -> None:
    """把更新合并写入 config.yaml（深层合并，保留其他键；注释会丢失，属已知代价）。"""
    p = Path(path)
    data: dict[str, Any] = {}
    if p.exists() and yaml is not None:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}

    def merge(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(data, updates)
    p.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def mask_settings(s: Settings) -> dict[str, Any]:
    """输出配置用于展示，遮蔽密钥字段。"""
    data = asdict(s)

    def _mask(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if "api_key" in k and v:
                    out[k] = v[:4] + "****"
                else:
                    out[k] = _mask(v)
            return out
        return obj

    return _mask(data)  # type: ignore[return-value]
