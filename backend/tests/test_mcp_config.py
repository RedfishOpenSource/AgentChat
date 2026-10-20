"""文档中 AgentR MCP JSON 可加载成 ACP 参数，不连接实际中间件。"""
import json
from pathlib import Path
import re

import pytest
from app.mcp_config import load_workspace_mcps


def test_documented_agentr_mcp_config_loads(tmp_path):
    guide = Path(__file__).resolve().parents[2] / 'frontend/public/guide.html'
    text = guide.read_text(encoding='utf-8')
    config = json.loads(re.search(r'<pre id="mcp-config">(.*?)</pre>', text, re.S).group(1))
    path = tmp_path / '.agents/mcps/.mcp.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config), encoding='utf-8')
    servers = load_workspace_mcps(str(tmp_path), [])
    assert {s['name'] for s in servers} == {'agentr-rust','agentr-node','agentr-go','agentr-python'}
    assert all(s['type'] == 'http' and s['headers'] == [] for s in servers)


def test_stdio_headers_and_invalid_config(tmp_path):
    path = tmp_path / '.agents/mcps/.mcp.json'
    assert load_workspace_mcps(str(tmp_path), [{'name':'fallback'}]) == [{'name':'fallback'}]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'mcpServers':{'stdio':{'command':'node','args':['server.js'],'env':{'KEY':'value'}},
        'http':{'url':'http://localhost/mcp','headers':{'x-test':'ok'}}}}), encoding='utf-8')
    servers = load_workspace_mcps(str(tmp_path), [])
    assert servers[0]['env'] == [{'name':'KEY','value':'value'}]
    assert servers[1]['headers'] == [{'name':'x-test','value':'ok'}]
    path.write_text('{bad json}', encoding='utf-8')
    with pytest.raises(ValueError, match='请检查 MCP 配置格式'):
        load_workspace_mcps(str(tmp_path), [])
