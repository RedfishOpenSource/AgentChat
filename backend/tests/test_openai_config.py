"""通用 OpenAI 配置回归：仅使用内存应用与 HTTP Mock，不接真实平台。"""
import httpx
import pytest
from pathlib import Path
from app.config import load_settings
from tests.test_config_hot import hot_client


def test_openai_settings_persist_and_hot_apply(hot_client):
    key = 'test-only-not-a-real-key'
    response = hot_client.put('/api/config', json={
        'engine': 'llm', 'llm_base_url': ' https://model.test/v1 ',
        'llm_model': ' custom-model ', 'llm_api_key': key, 'persist': True,
    })
    assert response.status_code == 200
    # 面板要求保存后可见：GET /api/config 回显 LLM Key 明文；其余密钥字段仍脱敏
    assert key in hot_client.get('/api/config').text
    hot_client.put('/api/config', json={'llm_max_retries': 4, 'persist': True})
    masked_text = hot_client.get('/api/config').text
    assert hot_client.get('/api/config').status_code == 200
    agent = hot_client.app.state.agent
    assert agent.engine.cfg.base_url == 'https://model.test/v1'
    assert agent.engine.cfg.model == 'custom-model'
    assert agent.engine.cfg.api_key == key
    loaded = load_settings(agent.settings.config_file)
    assert loaded.llm.api_key == key
    assert loaded.llm.model == 'custom-model'
    assert loaded.llm.base_url == 'https://model.test/v1'
    assert hot_client.put('/api/config', json={'llm_api_key': ' ', 'persist': True}).status_code == 200
    assert load_settings(agent.settings.config_file).llm.api_key == key
    assert agent.settings.llm.api_key == key

    def handler(request):
        assert str(request.url) == 'https://model.test/v1/chat/completions'
        assert request.headers['authorization'] == f'Bearer {key}'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"candidates":["mock transport reply"]}'}}]})

    # 替换模型 HTTP client，断言新参数真正用于请求，绝不联网。
    import asyncio
    asyncio.run(agent.engine._client.aclose())
    agent.engine._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = hot_client.post('/api/chat', json={'content': 'local test'})
    assert result.status_code == 200
    assert result.json()['reply']['content'] == 'mock transport reply'


@pytest.mark.parametrize('updates', [
    {'llm_base_url': ''}, {'llm_base_url': 'ftp://model.test'},
    {'llm_base_url': 'https://user:password@model.test/v1'},
    {'llm_base_url': 'https://model.test/v1?key=secret'},
    {'llm_base_url': 'https://model.test:bad/v1'}, {'llm_model': ' '},
])
def test_invalid_openai_config_does_not_persist(hot_client, updates):
    path = Path(hot_client.app.state.settings.config_file)
    response = hot_client.put('/api/config', json={**updates, 'engine': 'llm', 'persist': True})
    assert response.status_code == 400
    assert not path.exists()
    assert hot_client.app.state.agent.engine.name == 'mock'
