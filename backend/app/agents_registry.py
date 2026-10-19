"""内置 Agent 注册表：可通过下拉直接选择的现成 Agent（均经 ACP 接入）。

- pi / claude-code / codex：本机已装对应 ACP 适配器（pi-acp / claude-code-acp / codex-acp）
- hermes：预留条目，安装其 ACP 适配器（提供 hermes-acp 命令）后即可直接使用
- pi-rpc：ChatAgent 内置的 pi RPC 适配（非标准 ACP，每联系人独立子进程、会话落盘）

后续接入新 Agent：在内置表加一行，或直接在前端选「自定义」手填任意 ACP 命令。
"""
from __future__ import annotations

import shutil

BUILTIN_AGENTS: list[dict] = [
    {
        "id": "pi",
        "name": "PI",
        "mode": "acp",
        "command": "pi-acp",
        "args": "",
        "description": "pi coding agent（pi-acp 适配器，标准 ACP；npm i -g pi-acp）",
    },
    {
        "id": "claude-code",
        "name": "Claude Code",
        "mode": "acp",
        "command": "claude-code-acp",
        "args": "",
        "description": "Claude Code（claude-code-acp 适配器；claude CLI 已登录可不填 Key）",
    },
    {
        "id": "codex",
        "name": "Codex",
        "mode": "acp",
        "command": "codex-acp",
        "args": "",
        "description": "OpenAI Codex（codex-acp 适配器；使用 codex CLI 已有登录态）",
    },
    {
        "id": "hermes",
        "name": "Hermes",
        "mode": "acp",
        "command": "hermes-acp",
        "args": "",
        "description": "Hermes（预留：安装其 ACP 适配器 hermes-acp 后即可使用）",
    },
    {
        "id": "pi-rpc",
        "name": "PI（内置 RPC 适配）",
        "mode": "pi",
        "command": "pi",
        "args": "",
        "description": "pi --mode rpc：每联系人独立子进程，会话文件落盘，服务重启不丢上下文",
    },
]


def list_agents() -> list[dict]:
    """内置 Agent 列表 + 本机可用性检测（PATH 中能否找到启动命令）。"""
    out: list[dict] = []
    for a in BUILTIN_AGENTS:
        item = dict(a)
        try:
            item["available"] = shutil.which(str(a["command"])) is not None
        except Exception:  # noqa: BLE001
            item["available"] = False
        out.append(item)
    return out


def find_agent(agent_id: str) -> dict | None:
    for a in BUILTIN_AGENTS:
        if a["id"] == agent_id:
            return dict(a)
    return None
