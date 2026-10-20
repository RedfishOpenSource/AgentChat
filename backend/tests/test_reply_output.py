"""启动信息过滤与平台发送边界：使用 Mock 连接器，不进行真实发送。"""
from __future__ import annotations

import pytest

from app.engine.output import clean_agent_reply, is_pi_notification
from app.models import ChatMessage, Platform
from tests.test_contact_chat import bind_fake_agent

BANNER = '## Context\n- D:\\project\\AGENTS.md\n\n## Skills\n- .agents\\skills\\demo\\SKILL.md\n\n## Extensions\n- C:\\pi\\extensions\\demo.ts\n'


@pytest.mark.parametrize('text,expected', [
    (BANNER + '今天是 10 月 20 日。', '今天是 10 月 20 日。'),
    ('pi v1.2.3\n---\n\n' + BANNER + 'OK', 'OK'),
    (BANNER, ''),
    ('## Context\n这里是问题背景。\n## Skills\n- Python\n- SQL', '## Context\n这里是问题背景。\n## Skills\n- Python\n- SQL'),
    ('## Skills\n- Python\n- SQL', '## Skills\n- Python\n- SQL'),
    ('请查看 D:\\project\\AGENTS.md。', '请查看 D:\\project\\AGENTS.md。'),
    ('示例：\n## Context\n- /tmp/file\n正文', '示例：\n## Context\n- /tmp/file\n正文'),
])
def test_clean_startup_info_only(text, expected):
    assert clean_agent_reply(text) == expected


def test_retry_notifications_are_not_replies():
    assert is_pi_notification('Retrying (attempt 1/3, waiting 2s)...')
    assert is_pi_notification('Retry finished, resuming.')
    assert not is_pi_notification('用户希望了解 Retrying 的含义')


@pytest.mark.parametrize('platform', ['wechat', 'dingtalk'])
def test_old_draft_cleaned_before_mock_send(client, platform):
    agent = client.app.state.agent
    reply = ChatMessage(platform=Platform(platform), chat_id='clean', direction='out', status='pending',
                        content=BANNER + '今天是 10 月 20 日。', meta={'engine': 'acp'})
    agent.store.add(reply)
    listed = client.get('/api/messages', params={'chat_id': 'clean'}).json()['messages']
    assert listed[0]['content'] == '今天是 10 月 20 日。'
    response = client.post(f'/api/replies/{reply.id}/approve')
    assert response.status_code == 200
    assert response.json()['message']['content'] == '今天是 10 月 20 日。'
    assert agent.connectors[Platform(platform)].sent[0].content == '今天是 10 月 20 日。'


def test_adapter_banner_absent_from_reply_and_preview(client, tmp_path):
    agent = bind_fake_agent(client, tmp_path)
    script = tmp_path / 'contact_chat_acp.py'
    source = script.read_text(encoding='utf-8')
    source = source.replace('f"[{sid}] echo:{text}"', repr(BANNER) + ' + "干净的答复"')
    script.write_text(source, encoding='utf-8')
    response = client.post('/api/agent-sessions/chat', json={
        'platform': 'wechat', 'chat_id': 'c1', 'content': '你好',
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['reply']['content'] == '干净的答复'
    assert data['message']['meta']['processing']['preview'] == '干净的答复'
    assert all(not conn.sent for conn in agent.connectors.values())
