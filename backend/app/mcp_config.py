"""将工作空间的通用 MCP JSON 转为 ACP session/new 配置。"""
from __future__ import annotations

import json
from pathlib import Path


def load_workspace_mcps(workspace: str, fallback: list[dict]) -> list[dict]:
    path = Path(workspace).expanduser() / '.agents' / 'mcps' / '.mcp.json'
    if not path.is_file():
        return list(fallback)
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        servers = data['mcpServers']
        if not isinstance(servers, dict):
            raise ValueError('mcpServers 必须为对象')
        out = []
        for name, server in servers.items():
            if not isinstance(server, dict):
                raise ValueError('MCP 服务配置必须为对象')
            if server.get('disabled'):
                continue
            kind = server.get('type') or ('http' if server.get('url') else 'stdio')
            if kind in ('http', 'sse'):
                if not isinstance(server.get('url'), str) or not server['url'].startswith(('http://', 'https://')):
                    raise ValueError('HTTP MCP 需要有效 URL')
                headers = server.get('headers') or {}
                if isinstance(headers, dict):
                    headers = [{'name': str(k), 'value': str(v)} for k, v in headers.items()]
                out.append({'name': name, 'type': kind, 'url': server['url'], 'headers': headers})
            elif kind == 'stdio':
                if not server.get('command'):
                    raise ValueError('stdio MCP 需要 command')
                env = server.get('env') or {}
                if isinstance(env, dict):
                    env = [{'name': str(k), 'value': str(v)} for k, v in env.items()]
                out.append({'name': name, 'command': server['command'], 'args': server.get('args') or [], 'env': env})
            else:
                raise ValueError('不支持的 MCP 类型')
        return out
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('无法读取工作空间 .agents/mcps/.mcp.json，请检查 MCP 配置格式') from exc
