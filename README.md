# ChatAgent — 微信/钉钉智能聊天机器人

参考 [jev-chat-windows-deepseek-jev](https://github.com/Aimark-dai/jev-chat-windows-deepseek-jev) 的核心流程实现的智能聊天机器人：
**监听新消息 → 连发合并（静默 3 秒） → （可选）JEV 结构化预判 → 生成回复草稿 → （可选）JEV 复审排序 → 默认人工编辑确认后发送；可显式开启普通回复自动发送**。

- **后端**：Python 3.11+ / FastAPI / WebSocket
- **前端**：Vue 3 + Vite
- **聊天来源**：微信（wechatauto-replica，适配微信 4.x 本地库增量轮询）、钉钉（DWS CLI Stream 事件流）
- **LLM**：DeepSeek 等任意 OpenAI 兼容 Chat Completions 接口；默认内置 Mock 规则引擎（无需 API Key 即可跑通全流程）
- **JEV（可选）**：结构化判断模块，提供 Mock 实现（本地规则）与 TypeSafe 官方 `/v1/systemone` 实现（参考项目同款流程），调用失败自动降级

## 架构

```
┌────────────────────────────── 后端 (FastAPI) ──────────────────────────────┐
│                                                                            │
│  连接器 Connectors            核心调度 Agent              回复生成引擎        │
│  ┌──────────────────┐   监听到新消息   ┌──────────────┐  调用接口  ┌──────────────────┐
│  │ WeChatConnector  │ ───────────────▶│  ingest()    │───────────▶│ ReplyGenerator   │
│  │  mock / wxauto   │                 │  存储/广播    │            │  mock(规则)       │
│  │ DingTalkCliConn  │                 │              │            │  llm(DeepSeek)   │
│  │  mock / cli      │◀───────────────│  发送回复     │            │  (图片理解/语义检索可选)│
│  └──────────────────┘   send_reply()  └──────────────┘            └────────┬─────────┘
│                                            ▲                               │ 可选
│  消息存储 MessageStore / 事件总线 EventBus    │ 人工审核            ┌─────────▼─────────┐
│  REST API (/api/*) + WebSocket (/ws)        └───────────────────│ JEVJudge 预判/复审 │
└────────────────────────────────────────────────────────────────┴───────────────────┘
          ▲
          │ HTTP + WebSocket (vite 代理)
┌─────────┴──────────┐
│  前端 (Vue 3)       │  连接器状态/启停、模拟消息注入、消息流+待审核操作、对话测试、
│                     │  知识库（目录选择/重扫/语义检索开关）
└────────────────────┘
```

核心需求对应：

1. **一旦监听到新消息，就触发生成回复** —— 每个连接器有独立监听循环（`BaseConnector._poll_loop`），
   poll 到新消息立即回调 `AgentService.ingest()` → `engine.generate()`，全程毫秒级。
2. **生成回复先实现一个接口** —— `app/engine/base.py::ReplyGenerator`（抽象接口），
   已实现 `MockReplyGenerator`（规则）与 `LLMReplyGenerator`（DeepSeek/JEV 全链路）；后续 RAG 等实现只需继承该接口并在 `build_engine` 注册。

## 快速开始

### 后端

```bash
cd backend
uv sync                                   # 安装依赖（自动管理 Python 虚拟环境）
uv run uvicorn app.main:app --reload      # 启动，默认 http://127.0.0.1:8320
```

无 uv 时：`pip install fastapi "uvicorn[standard]" httpx pydantic pyyaml` 后 `python -m uvicorn app.main:app`。

启动后两个连接器（微信/钉钉）默认 **mock 模式** 自动运行，不依赖任何外部程序。

### 前端

开发模式：

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 （/api 与 /ws 自动代理到后端 8320）
```

生产模式（推荐）：前端构建后由后端直接托管，访问 `http://127.0.0.1:8320` 即可：

```bash
cd frontend && npm run build   # 产物在 frontend/dist，后端自动探测并托管
```

### Windows 一键启停（推荐）

```bat
restart.bat   # 服务管理菜单（推荐日常使用）：1 停止服务 / 2 重新编译并启动
start.bat     # 启动（首次自动 uv sync；崩溃后 5 秒自动重启；日志写入 backend/logs/）
stop.bat      # 仅停止端口监听进程；若服务由 start.bat 守护启动，请用 restart.bat 停止，
              # 否则守护循环会在 5 秒后把服务重新拉起
```

### Docker

容器内仅支持 mock 连接器（微信/钉钉连接器依赖 Windows 桌面环境），适合体验前端与 API：

```bash
docker compose up -d
```

### ⚠️ 测试安全规则（必须遵守）

自动化测试涉及真实消息发送时，**只允许**：
- 钉钉 → 发到「ChatAgent测试群(仅赵丽阳)」（仅含用户本人）
- 微信 → 发到「文件传输助手」

且必须先在对话中获得用户批准。详见根目录 `AGENTS.md`。

## 模拟测试

```bash
cd backend
uv run pytest                                  # 31 个单元/接口测试
uv run python ../scripts/simulate.py           # 端到端模拟（含审核流、JEV 拦截验证）
node ../scripts/ws-test.mjs                    # WebSocket 联调（需前后端同时在跑）
```

## 接入真实平台

### 微信（wechatauto-replica，微信 4.x，推荐）

基于 wechatauto-replica 库：监听走本地数据库增量轮询（不受窗口影响、重启不漏消息），发送走 UIA+OCR：

```yaml
wechat:
  mode: replica
  listen_chats: ["张三", "项目交流群"]   # 要监听的微信会话名
  poll_interval: 1.0
  group_reply: quote-at                 # 群聊回复：quote-at(默认) | plain
```

要求：Windows + 登录微信桌面端（4.1.12+） + `uv pip install wechatauto-replica`。未安装时启动连接器会明确报错，不影响其他模块。

**群聊引用+@回复（quote-at，默认开启）**：群聊草稿正文自动带「@触发者 」前缀（人工可编辑，所见即所得），
发送时自动引用触发的那条原消息（右键 → 引用）；引用定位失败自动回退直发，回复不丢失。
设 `group_reply: plain` 恢复直发。

### 微信（wxauto，旧版兼容，仅微信 3.9.x）

```yaml
wechat:
  mode: wxauto
  listen_chats: ["张三", "项目交流群"]
```

要求：Windows + 登录微信桌面端 + `uv pip install wxauto`。未安装时启动连接器会明确报错，不影响其他模块。

### 钉钉（DWS CLI，已真机验证）

依赖本机已安装并登录 [DWS CLI](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)（`dws auth login`）：

```yaml
dingtalk:
  mode: dws
  listen_kind: at-me        # at-me(推荐,只回@我) | all-direct | all-group | group
  listen_chats: []          # listen_kind=group 时的群名列表
  reply_mode: quote         # quote(引用回复原消息) | plain(直接发送)
```

工作方式：
- 监听：`dws event +listen-im` 长连接子进程，NDJSON 事件实时触发回复（进程意外退出自动重启）
- 回复：`dws chat +messages-reply` 引用回复原消息（带幂等键防重发）；缺引用时回退 `+messages-send` 直发
- 已验证：真实群 @消息 → 事件触发 → 生成回复 → 人工批准 → 引用回复送达群聊

### 钉钉（通用 CLI 约定，mode=cli，兼容保留）

```yaml
dingtalk:
  mode: cli
  command: "你的拉取命令"          # NDJSON 输出: {"chat_id":..,"sender":..,"content":..}
  reply_command: "你的发送命令 --chat {chat_id} --text {content}"
```

### 接入外部 Agent（ACP，可插拔）

设置页「AI 接入」选 **Agent**，下拉直接选择已安装的 Agent（选项由 `/api/agents` 返回，含本机可用性检测）：**PI / Claude Code / Codex / Hermes / PI（内置 RPC）/ 自定义**。
每个被监听的联系人保持一个独立 Agent 会话，多轮问答共享上下文；知识、工具等能力全部由 Agent 自带。

| Agent | 接入方式 | 说明 |
| --- | --- | --- |
| PI | 标准 ACP（`pi-acp`，需 `npm i -g pi-acp`） | pi coding agent 经官方适配器接入，单进程多会话 |
| Claude Code | 标准 ACP（`claude-code-acp`，需 `npm i -g @zed-industries/claude-code-acp`） | claude CLI 已登录可不填 Key |
| Codex | 标准 ACP（`codex-acp`，需 `npm i -g @agentclientprotocol/codex-acp`） | 使用 codex CLI 已有登录态 |
| Hermes | 预留（`hermes-acp`） | 安装其 ACP 适配器后下拉即可选中 |
| PI（内置 RPC） | ChatAgent 内置适配（`pi --mode rpc`） | 每联系人独立子进程，`--session-id` 绑定会话，重启不丢上下文 |
| 自定义 | 任意 ACP 命令 | 填启动命令、参数、鉴权环境变量即可，后续新 Agent 均可这样接入 |

配置项：启动命令、参数、**工作空间（cwd）**、鉴权环境变量名与 Key、超时。
⚠️ Agent 具备工具执行（含 shell）能力，工作空间请指向专用目录；切换接入方式会重建引擎（会话重开）。

### 启用 LLM（OpenAI 兼容接口）

前端设置页现在只提供 **Agent（ACP）选择**；LLM / Mock 引擎仍受支持，但需通过 `backend/config.yaml` 或环境变量配置（改完重启服务）。
URL 使用服务商提供的基础地址（例如包含 `/v1`），程序追加 `/chat/completions`。
不要把真实 Key 写入配置示例或代码。配置文件是本地明文配置，应限制本机访问，也不要强制添加到 Git。

```bash
# 方式一：环境变量
export CHATAGENT_ENGINE=llm
export CHATAGENT_LLM_API_KEY=sk-xxx
# 方式二：backend/config.yaml（参考 config.example.yaml）
```

任何 OpenAI 兼容接口都可用（改 `llm.base_url` / `llm.model`）。

### 启用 JEV（可选）

```yaml
jev:
  enabled: true
  provider: mock        # mock=本地规则判断；typesafe=官方接口
  # provider: typesafe 时：
  api_key: "ts-xxx"     # 或环境变量 CHATAGENT_JEV_API_KEY
  base_url: "https://api.typesafe.ai"
```

流程与参考项目一致：**JEV 预判（风险/意图/建议行动）注入提示词 → LLM 生成候选 → JEV 复审打分排序 →
全部不合格时自动重写一次 → 仍不合格则拦截（页面可人工“忽略拦截发送”）**。

## 配置参考（环境变量优先级高于 config.yaml）

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `CHATAGENT_ENGINE` | mock / llm | mock |
| `CHATAGENT_DEBOUNCE_SECONDS` | 连发合并静默期（秒），0 关闭 | 3.0 |
| `CHATAGENT_LLM_API_KEY` | LLM 密钥 | 空 |
| `CHATAGENT_LLM_BASE_URL` | OpenAI 兼容地址 | https://api.deekseek.com |
| `CHATAGENT_LLM_MODEL` | 模型名 | deepseek-chat |
| `CHATAGENT_LLM_STYLE` | 回复风格预设：default / formal / concise / casual / friendly | default |
| `CHATAGENT_KNOWLEDGE_DIR` | RAG 知识库目录（.md/.txt）；空 = 未启用 | 空 |
| `CHATAGENT_EMBEDDING_ENABLED` / `_BASE_URL` / `_API_KEY` / `_MODEL` | RAG 语义检索开关/地址/密钥/模型 | false / 空(复用 llm) / 空(复用 llm) / 空 |
| `CHATAGENT_LLM_VISION_MODEL` / `_VISION_BASE_URL` / `_VISION_API_KEY` | 图片理解模型/地址/密钥 | 空 / 空(复用 llm) / 空(复用 llm) |
| `CHATAGENT_JEV_ENABLED` / `CHATAGENT_JEV_PROVIDER` / `CHATAGENT_JEV_API_KEY` | JEV 开关/实现/密钥 | false / mock / 空 |
| `CHATAGENT_WECHAT_MODE` / `CHATAGENT_WECHAT_GROUP_REPLY` | 微信连接器模式 / 群聊回复方式 | mock / quote-at |
| `CHATAGENT_DINGTALK_MODE` / `CHATAGENT_DINGTALK_DWS_BIN` / `CHATAGENT_DINGTALK_LISTEN_KIND` / `CHATAGENT_DINGTALK_REPLY_MODE` | 钉钉 dws 模式配置 | mock / dws / at-me / quote |

## 访问控制（无登录方案）

- 服务默认仅绑定本机（127.0.0.1），局域网无法访问
- 中间件按 **IP 白名单** 拦截所有 HTTP/WebSocket 请求：非白名单返回 403 / 断开
- 白名单配置：`allowed_ips: ["127.0.0.1", "::1"]`（config.yaml）或环境变量 `CHATAGENT_ALLOWED_IPS=192.168.1.5,10.0.0.8`；`["*"]` 表示全放行
- 需要局域网访问：启动时绑定 `--host 0.0.0.0` **并把对应客户端 IP 加入白名单**，两者缺一不可

## 配置热更新

`PUT /api/config`（前端「设置」面板）可运行时修改并立即生效：

- 回复引擎（mock ↔ llm）、JEV 开关/实现、候选数量、温度、重试次数、**回复风格预设**
- 群聊触发策略、非文本处理、连发合并时长、每会话生成限额、联系人白名单、告警渠道
- **知识库目录**（前端「知识库」面板选择，或 PUT /api/config 的 `knowledge_dir`）与**语义检索开关**（`embedding_enabled`）
- `persist: true` 时同步写回配置文件（`jev_api_key` 除外，不落盘）；连接器模式与监听地址等需重启

## RAG 知识库回复

让回复参考你自己的文档：前端「知识库（RAG）」面板 → 「选择目录」（服务端目录选择器，支持盘符/文件夹逐级浏览）
→ 后端扫描 `.md` / `.txt`（含子目录，UTF-8 / GBK 自动识别）并建立中文 bigram 倒排索引。

- **生成时**：自动检索与触发消息最相关的分块（top-4）注入提示词，并提示「资料中没有的信息不要编造」；引用了哪些文件记录在草稿 `meta.rag`
- **更新**：目录内容变化会在下次生成时自动重扫（冷却 5 秒防抖）；也可手动点「重新扫描」
- **开关**：面板「停用」或 `knowledge_dir: ""`；仅 engine=llm 时生效，mock 引擎不受影响

**语义检索（可选，默认关）**：词面检索（零依赖，始终可用）对同义改述无能为力，可选配任意
OpenAI 兼容 `/embeddings` 接口开启语义向量检索：

```yaml
embedding:
  enabled: true
  model: "bge-m3"          # 或 text-embedding-v3 / embedding-3 等；base_url/api_key 留空复用 llm 配置
```

- 开启后词面+向量两路排名按 **RRF 融合**，同义改述（如知识库写「报销流程」，提问「钱怎么退回来」）也能召回
- **分块向量磁盘缓存**（`rag_embeddings.json`，与数据库同目录）：重启不重复计费，只对新增/变更分块调用接口
- **失败自动降级**：Embedding 接口超时/报错时自动退回纯词面检索（60 秒冷却），绝不阻断回复生成
- 成本极低：小型嵌入模型按量计费通常每百万 token 几毛钱，分块向量只需算一次
- 前端「知识库」面板可一键开关并查看缓存向量数；也可 `PUT /api/config` 的 `embedding_enabled` 热切换

## 监控告警

- **触发源**：连接器异常（error 状态巡检，默认 30 秒）、LLM 连续失败（默认 3 次）
- **去重**：同一异常只通知一次，恢复时发送「已恢复」
- **前端**：页面顶部红色横幅常驻显示活动告警（`/api/status` 的 `alerts` 字段）
- **通知渠道（默认 off，告警消息发给你自己）**：
  - `alert_channel: wechat-filehelper` —— 微信文件传输助手（wechatauto GUI 发送）
  - `alert_channel: dingtalk-self` —— 钉钉自有会话（需配 `alert_dingtalk_chat_id`，DWS CLI 发送）
  - 通知节流：默认最小间隔 60 秒，防风暴

## LLM 韧性：重试 / 降级 / 限流

- **重试**：调用失败（超时/网络错误/429/5xx）按指数退避自动重试（`llm.max_retries`，默认 2 次）；401/400 等客户端错误快速失败
- **降级**：全部重试失败后生成「⚠️ AI 引擎暂时不可用，请人工回复」占位草稿，进入人工确认流程，消息绝不静默丢失
- **限流**：每会话每分钟最多 `llm_rate_limit_per_chat`（默认 10）次生成，超出的消息标记为「已忽略（频率限制）」并在消息流可见

## 消息场景策略（全部场景显式处理）

| 场景 | 处理方式 | 配置 |
| --- | --- | --- |
| 私聊文本 | 正常生成草稿 | — |
| 群聊文本·@我 | 生成草稿 | `group_trigger: at-mention`（默认） |
| 群聊文本·未@我 | 忽略（记录状态，不生成） | 同上 |
| 群聊全部消息触发 | — | `group_trigger: all` |
| 群聊只听不回 | — | `group_trigger: listen-only` |
| 图片/语音/视频/表情/文件等 | 生成占位草稿「收到图片消息，暂不支持内容识别」 | `non_text_action: placeholder`（默认） |
| 同上，静默忽略 | 记录状态不生成 | `non_text_action: skip` |
| 图片消息（已配视觉模型） | 调用视觉模型生成图片描述草稿「[图片] AI 识别：…」 | `llm.vision_model`（可选） |
| 同上，未配视觉模型 | 生成占位草稿「收到图片消息，暂不支持内容识别」 | `non_text_action: placeholder`（默认） |
| 系统消息（撤回等） | 始终忽略 | — |
| 自己发送的消息 | 连接器层直接过滤（防回声循环） | — |
| 不在联系人白名单的会话 | 忽略（记录状态，不生成）；名单可运行时修改 | `allowed_contacts` / 前端白名单面板 |

忽略的消息在前端消息流中标记为「已忽略」并显示原因；微信连接器通过 `username` 是否以 `@chatroom` 结尾判定群聊，通过内容中的 `@昵称` 检测 @我。

## 图片理解（可选）

配置 `llm.vision_model` 后，收到的图片会自动交给 OpenAI 兼容视觉模型生成**图片描述草稿**
（「[图片] AI 识别：…]」，仍走人工确认，不自动发送）；描述失败或未配置时退回占位草稿。
DeepSeek 无视觉模型，可将 `vision_base_url` / `vision_api_key` 指向 Qwen-VL、GLM-4V 等
OpenAI 兼容视觉接口（留空则复用对话模型配置）。图片来源取消息 meta 的 `image_path`（本地文件）
或 `image_url`（链接）；语音/视频等其他非文本类型仍按占位/忽略策略处理。

```yaml
llm:
  vision_model: "qwen-vl-plus"   # 空 = 图片只生成占位草稿（默认）
  vision_base_url: ""            # 空 = 复用 base_url
  vision_api_key: ""             # 空 = 复用 api_key
```

## 数据持久化与审计

- 所有消息（收到的/草稿/已发送/拦截/驳回）实时落盘 SQLite（默认 `backend/chatagent.db`，WAL 模式）
- 服务重启后自动恢复：历史消息、会话上下文、**未确认草稿**均可找回
- 审计日志：每次「确认发送/驳回」记录操作人、时间、内容快照，`GET /api/audit` 查询
- 微信监听水位独立落盘（wechatauto 库管理），重启不漏消息不重复推

## 安全约束：默认人工确认

- 生成结果默认为**草稿**（待确认状态）；`auto_send: true`（默认 false，前端可一键切换）时普通草稿自动发送，**blocked（JEV 拦截）草稿任何情况下永不自动发送**
- 前端待审核回复会填入**可编辑草稿框**：修改后发送发送编辑稿，原稿保留在 `meta.edited_from`
- 代码层面：`_dispatch()` 仅能被 `approve_reply()` 调用；blocked 草稿的自动发送在代码层被显式排除

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/status` | 引擎/JEV/连接器状态 |
| GET | `/api/messages?platform=&chat_id=&limit=` | 消息列表 |
| GET | `/api/replies?status=pending` | 回复列表（可按状态过滤） |
| POST | `/api/replies/{id}/approve` `/reject` | 人工批准/驳回 |
| POST | `/api/simulate` | 模拟一条微信/钉钉新消息（mock 模式） |
| POST | `/api/chat` | 直接对话（走生成器接口） |
| POST | `/api/connectors/{platform}/start` `/stop` | 连接器启停 |
| WS | `/ws` | 实时事件：`message` `reply` `reply_updated` `status` `error` |

## 测试结果

```
backend 单元/接口测试:  175 passed（含连接器、持久化、白名单、连发合并、场景策略、LLM 韧性、告警、热更新、静态托管、微信群聊引用+@回复、RAG 语义检索、图片理解、连接器级白名单/知识库隔离）
端到端模拟 simulate.py: 16 passed, 0 failed
钉钉真机验证:           真实群@消息 → 事件触发 → 生成回复 → 批准 → 引用回复送达 ✅
前端 vite build:        OK
```

## 设计决策（原 Roadmap 的结论）

- **RAG 语义向量检索 —— 已实现（可选）**：支持任意 OpenAI 兼容 Embedding 接口 + 磁盘缓存 +
  RRF 混合排序 + 失败降级；不配置则保持零成本词面检索。刻意不引入本地向量模型/向量库：
  小型嵌入 API 按量计费成本可忽略，避免本地算力与向量库运维负担。
- **微信群聊「真实 @」（选人弹窗强提醒）—— 刻意不做**：需 UIA 驱动微信选人弹层，
  脆弱（窗口遮挡/缩放/版本变更即失败）、无法自动化测试、且模拟人工操作有账号风控风险。
  现有「引用原消息 + 文本 @」已保证回复可定位到触发者，稳定性优先。
- **语音/视频等内容识别 —— 暂不支持**：语音需 silk 解码+ASR 链路、视频需抽帧，
  收益低于复杂度；现有占位草稿确保消息不静默丢失，由人工接管。图片已通过可选视觉模型覆盖。

## 投递状态

审核或自动发送会记录 `sending`、`sent`、`failed` 或 `unknown` 状态。`unknown` 表示客户端无法确认平台是否收到消息，界面要求先核对会话再显式确认重试，避免无提示重复发送；服务重启时遗留的 `sending` 状态会恢复为 `unknown`。

## 声明

- 本项目仅参考了 jev-chat-windows-deepseek-jev 的**处理流程设计**（预判→生成→复审→人工可控），代码为全新实现，未使用其源码。
- 请仅在有权查看的聊天场景中使用；涉及转账、红包、密码等内容默认被 JEV 门禁拦截。
