"""消息页补充给 AI：复用绑定会话、只生成草稿、按平台隔离。所有平台连接器均为 Mock。"""
from __future__ import annotations

import sys

from app.models import Platform
from tests.test_acp_agent import FAKE_ACP_AGENT


def bind_fake_agent(client, tmp_path):
    script = tmp_path / 'contact_chat_acp.py'
    script.write_text(FAKE_ACP_AGENT, encoding='utf-8')
    agent = client.app.state.agent
    agent.agent_profiles['support'] = {
        'id': 'support', 'name': '客服', 'engine': 'pi',
        'command': sys.executable, 'args': str(script), 'workspace': str(tmp_path),
    }
    agent.assign_agent('wechat', 'c1', 'support')
    agent.assign_agent('dingtalk', 'c1', 'support')
    return agent


def test_contact_chat_reuses_session_resets_and_never_auto_sends(client, tmp_path):
    agent = bind_fake_agent(client, tmp_path)
    agent.settings.auto_send = True
    # 监控来信先生成一份草稿；用 Mock 自动发送无真实外部操作。
    agent.settings.auto_send = False
    first = client.post('/api/simulate', json={
        'platform': 'wechat', 'chat_id': 'c1', 'content': '原始问题',
    }).json()
    assert 'sess-1' in first['reply']['content']
    agent.settings.auto_send = True
    response = client.post('/api/agent-sessions/chat', json={
        'platform': 'wechat', 'chat_id': 'c1', 'content': '请简短回答',
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['message']['sender'] == '操作员'
    assert data['message']['meta']['operator_input'] is True
    assert data['reply']['reply_to'] == data['message']['id']
    assert data['reply']['status'] == 'pending'
    assert 'sess-1' in data['reply']['content'] and '请简短回答' in data['reply']['content']
    other = client.post('/api/agent-sessions/chat', json={
        'platform': 'dingtalk', 'chat_id': 'c1', 'content': '钉钉问题',
    }).json()
    assert 'sess-2' in other['reply']['content']
    reset = client.post('/api/agent-sessions/reset', json={'platform': 'wechat', 'chat_id': 'c1'})
    assert reset.status_code == 200 and reset.json()['reset'] is True
    after = client.post('/api/agent-sessions/chat', json={
        'platform': 'wechat', 'chat_id': 'c1', 'content': '新问题',
    }).json()
    assert 'sess-3' in after['reply']['content']
    assert all(not conn.sent for conn in agent.connectors.values())
    listing = client.get('/api/messages').json()['messages']
    assert all(m['id'] != first['message']['id'] for m in listing)  # 清空已删除旧记录
    assert any(m['platform'] == 'dingtalk' and m['chat_id'] == 'c1' for m in listing)  # 其他会话不受影响


def test_contact_chat_validation_and_busy_reset(client):
    for body in [
        {'platform': 'web', 'chat_id': 'c1', 'content': 'hi'},
        {'platform': 'wechat', 'chat_id': ' ', 'content': 'hi'},
        {'platform': 'wechat', 'chat_id': 'c1', 'content': ' '},
        {'platform': 'wechat', 'chat_id': 'unbound', 'content': 'hi'},
    ]:
        assert client.post('/api/agent-sessions/chat', json=body).status_code == 400
    agent = client.app.state.agent
    agent.settings.debounce_seconds = 30
    client.post('/api/simulate', json={'platform': 'wechat', 'chat_id': 'busy', 'content': 'hi'})
    response = client.post('/api/agent-sessions/reset', json={'platform': 'wechat', 'chat_id': 'busy'})
    assert response.status_code == 409
    assert client.post('/api/agent-sessions/reset', json={'platform': 'dingtalk', 'chat_id': 'busy'}).status_code == 200


def test_supplement_regeneration_also_stays_draft(client):
    first = client.post('/api/simulate', json={
        'platform': 'wechat', 'chat_id': 'regen', 'content': '问题',
    }).json()
    client.app.state.agent.settings.auto_send = True
    response = client.post(f"/api/messages/{first['message']['id']}/regenerate", json={'extra': '补充'})
    assert response.status_code == 200
    assert response.json()['reply']['status'] == 'pending'
    assert client.app.state.agent.connectors[Platform.WECHAT].sent == []
