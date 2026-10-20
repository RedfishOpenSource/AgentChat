"""单独调试已创建的 Agent：按 Agent/调试会话隔离、复用配置、进度广播、禁止平台发送。"""
from tests.test_contact_chat import bind_fake_agent


def test_debug_agent_session_isolation_and_no_platform_send(client, tmp_path):
    agent = bind_fake_agent(client, tmp_path)
    agent.settings.auto_send = True

    def debug(session, content='hello', profile='support'):
        response = client.post(f'/api/agent-profiles/{profile}/chat', json={
            'session_id': session, 'content': content,
        })
        assert response.status_code == 200, response.text
        return response.json()

    first = debug('session-a')
    assert first['message']['platform'] == 'web'
    assert first['message']['chat_id'] == 'debug:support:session-a'
    assert first['reply']['status'] == 'debug'
    assert first['reply']['meta']['debug_profile_id'] == 'support'
    assert first['message']['meta']['processing']['phase'] == 'completed'
    assert first['message']['meta']['processing']['detail'] == '调试回复已生成'
    assert 'sess-1' in first['reply']['content']
    assert 'sess-1' in debug('session-a', 'follow-up')['reply']['content']
    assert 'sess-2' in debug('session-b')['reply']['content']
    contact = client.post('/api/agent-sessions/chat', json={
        'platform': 'wechat', 'chat_id': 'c1', 'content': 'contact question',
    }).json()
    assert 'sess-3' in contact['reply']['content']
    assert contact['reply']['status'] == 'pending'
    agent.agent_profiles['second'] = {**agent.agent_profiles['support'], 'id': 'second', 'name': '另一个 Agent'}
    assert 'sess-1' in debug('session-a', profile='second')['reply']['content']
    assert client.post(f"/api/replies/{first['reply']['id']}/approve").status_code == 400
    assert all(not conn.sent for conn in agent.connectors.values())
    history = client.get('/api/messages', params={
        'platform': 'web', 'chat_id': first['message']['chat_id'],
    }).json()['messages']
    assert len(history) == 4


def test_debug_endpoint_validation(client, tmp_path):
    agent = bind_fake_agent(client, tmp_path)
    assert client.post('/api/agent-profiles/missing/chat', json={'session_id':'a', 'content':'hi'}).status_code == 404
    assert client.post('/api/agent-profiles/support/chat', json={'session_id':'../bad', 'content':'hi'}).status_code == 422
    assert client.post('/api/agent-profiles/support/chat', json={'session_id':'a', 'content':' '}).status_code == 400
    agent.agent_profiles['support']['pending'] = True
    assert client.post('/api/agent-profiles/support/chat', json={'session_id':'a', 'content':'hi'}).status_code == 409
