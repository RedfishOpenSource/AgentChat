"""角色改写回归：新建/每次保存、显式失败、旧任务竞争、模板覆盖、真实 HTTP 请求契约。"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.role_prompts import RoleRewriteError, rewrite_role
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def no_template_network(monkeypatch):
    async def sync(workspace):
        return {'synced': 0, 'files': []}
    monkeypatch.setattr('app.prompts_sync.sync_workspace_prompts', sync)


def payload(tmp_path, **extra):
    return {'name': '客服', 'description': '帮助排查问题', 'workspace': str(tmp_path),
            'instructions': '旧的指令不应导致跳过改写', 'sync_mode': 'wait', **extra}


def wait_profile(client, profile_id, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        profile = next((p for p in client.get('/api/agent-profiles').json()['profiles'] if p['id'] == profile_id), None)
        if predicate(profile):
            return profile
        time.sleep(0.01)
    raise AssertionError('角色改写状态未按预期结束')


def test_create_and_each_save_rewrites_even_unchanged(client, tmp_path, monkeypatch):
    calls = []
    async def optimize(settings, description):
        calls.append(description)
        return f'第{len(calls)}版：分析证据，提供原因与方案。'
    monkeypatch.setattr('app.api.routes._llm_role_prompt', optimize)
    created = client.post('/api/agent-profiles', json=payload(tmp_path)).json()['profile']
    assert created['rewrite_status'] == 'succeeded' and not created['pending']
    first = Path(created['role_file']).read_text(encoding='utf-8')
    for index in (2, 3):
        result = client.post('/api/agent-profiles', json=payload(
            tmp_path, id=created['id'], description=created['description'], instructions=created['instructions'],
        )).json()['profile']
        assert result['rewrite_status'] == 'succeeded'
        assert result['description'] == result['instructions'] == f'第{index}版：分析证据，提供原因与方案。'
        assert result['rewrite_revision'] != created['rewrite_revision']
    assert len(calls) == 3
    assert Path(created['role_file']).read_text(encoding='utf-8') != first
    assert client.app.state.db.list_agent_profiles()[0]['instructions'] == result['instructions']


def test_failure_explicit_preserves_old_role_and_retry_succeeds(client, tmp_path, monkeypatch):
    async def ok(settings, description): return '成功改写后的角色提示词'
    monkeypatch.setattr('app.api.routes._llm_role_prompt', ok)
    created = client.post('/api/agent-profiles', json=payload(tmp_path)).json()['profile']
    role = Path(created['role_file'])
    before = role.read_text(encoding='utf-8')
    async def fail(settings, description): raise RoleRewriteError('HTTP 401，请检查配置')
    monkeypatch.setattr('app.api.routes._llm_role_prompt', fail)
    failed = client.post('/api/agent-profiles', json=payload(tmp_path, id=created['id'])).json()['profile']
    assert failed['rewrite_status'] == 'failed' and not failed['pending']
    assert '401' in failed['rewrite_error']
    assert role.read_text(encoding='utf-8') == before
    monkeypatch.setattr('app.api.routes._llm_role_prompt', ok)
    fixed = client.post('/api/agent-profiles', json=payload(tmp_path, id=created['id'])).json()['profile']
    assert fixed['rewrite_status'] == 'succeeded' and not fixed['rewrite_error']


def test_old_background_task_cannot_overwrite_new_save_or_resurrect_deleted(client, tmp_path, monkeypatch):
    async def optimize(settings, description):
        if description.startswith('慢'):
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)  # 模拟上游未及时取消
        return f'优化角色：{description}'
    monkeypatch.setattr('app.api.routes._llm_role_prompt', optimize)
    slow = client.post('/api/agent-profiles', json=payload(tmp_path, description='慢旧版本', sync_mode='background')).json()['profile']
    result = client.post('/api/agent-profiles', json=payload(tmp_path, id=slow['id'], description='新版本')).json()['profile']
    time.sleep(0.25)
    final = wait_profile(client, slow['id'], lambda p: not p['pending'])
    assert final['instructions'] == result['instructions'] == '优化角色：新版本'
    assert '新版本' in Path(final['role_file']).read_text(encoding='utf-8')
    client.post('/api/agent-profiles', json=payload(tmp_path, id=slow['id'], description='慢删除版本', sync_mode='background'))
    assert client.delete(f"/api/agent-profiles/{slow['id']}").status_code == 200
    time.sleep(0.25)
    assert client.get('/api/agent-profiles').json()['profiles'] == []


def test_background_success_and_failure_statuses(client, tmp_path, monkeypatch):
    async def optimize(settings, description):
        await asyncio.sleep(0.02)
        if description == '失败': raise RoleRewriteError('请求失败')
        return '优化成功的角色提示词'
    monkeypatch.setattr('app.api.routes._llm_role_prompt', optimize)
    created = client.post('/api/agent-profiles', json=payload(tmp_path, sync_mode='background')).json()['profile']
    assert created['pending']
    success = wait_profile(client, created['id'], lambda p: not p['pending'])
    assert success['rewrite_status'] == 'succeeded'
    client.post('/api/agent-profiles', json=payload(tmp_path, id=created['id'], description='失败', sync_mode='background'))
    failed = wait_profile(client, created['id'], lambda p: not p['pending'])
    assert failed['rewrite_status'] == 'failed' and failed['rewrite_error'] == '请求失败'


@pytest.mark.asyncio
async def test_model_request_no_silent_fallback():
    settings = make_settings()
    settings.llm.base_url = 'https://model.test/v1'
    settings.llm.model = 'test-model'
    attempts = []
    def handler(request):
        body = json.loads(request.content)
        attempts.append(body)
        assert body['messages'][1]['content'] == '原文'
        return httpx.Response(200, json={'choices':[{'message':{'content': '原文' if len(attempts)==1 else '改写后的角色职责与流程'}}]})
    result = await rewrite_role(settings, '原文', transport=httpx.MockTransport(handler))
    assert result == '改写后的角色职责与流程' and len(attempts) == 2
    for response in [httpx.Response(401), httpx.Response(200, json={'choices':[{'message':{'content':None,'reasoning_content':'不能作为正式角色正文'}}]})]:
        with pytest.raises(RoleRewriteError):
            await rewrite_role(settings, '原文', transport=httpx.MockTransport(lambda request: response))
    settings.llm.base_url = ''
    with pytest.raises(RoleRewriteError, match='请配置'):
        await rewrite_role(settings, '原文')


@pytest.mark.asyncio
async def test_template_sync_never_overwrites_agent_role(tmp_path):
    import app.prompts_sync as ps
    role = tmp_path / '.agents/memory/role.md'
    role.parent.mkdir(parents=True)
    role.write_text('这是已改写的角色', encoding='utf-8')
    def handler(request):
        if request.url.host == 'api.github.com':
            return httpx.Response(200, json=[
                {'type':'file','path':ps.PROMPTS_DIR_REPO+'/.agents/memory/role.md','download_url':'https://files.test/role'},
                {'type':'file','path':ps.PROMPTS_DIR_REPO+'/AGENTS.md','download_url':'https://files.test/agents'},
            ])
        return httpx.Response(200, content=b'template')
    # 本用例直接取真实函数，其他测试仅 mock 网络同步入口。
    result = await REAL_SYNC(str(tmp_path), transport=httpx.MockTransport(handler), prefer_git=False)
    assert result['files'] == ['AGENTS.md']
    assert role.read_text(encoding='utf-8') == '这是已改写的角色'


from app.prompts_sync import sync_workspace_prompts as REAL_SYNC
