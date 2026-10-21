"""清空上下文分界：本地历史隔离、重启/引擎重建保持、其他会话不受影响。"""
import pytest

from app.config import ACPSettings
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.engine.acp import ACPAgentGenerator
from app.models import ChatMessage, Platform
from app.persistence import SqliteStore
from app.store import EventBus, MessageStore
from tests.conftest import make_settings


class HistoryEngine(MockReplyGenerator):
    def __init__(self):
        self.histories = []

    async def generate(self, message, history, kb=None):
        self.histories.append((message.chat_id, [m.content for m in history]))
        return await super().generate(message, history, kb)


def service(db, engine):
    store = MessageStore(db=db)
    store.restore()
    return AgentService(make_settings(), store, EventBus(), engine, db=db)


@pytest.mark.asyncio
async def test_history_cutoff_survives_restart_and_preserves_other_platform(tmp_path):
    db = SqliteStore(str(tmp_path / 'boundary.db'))
    engine = HistoryEngine()
    agent = service(db, engine)
    async def ask(platform, content):
        message = ChatMessage(platform=platform, chat_id='same', content=content)
        await agent.ingest(message)
        return message
    await ask(Platform.WECHAT, 'old-wechat-secret')
    await ask(Platform.DINGTALK, 'old-dingtalk-secret')
    assert agent.reset_contact_session(Platform.WECHAT, 'same') >= 2
    assert not agent.store.list_messages(platform='wechat', chat_id='same')  # 页面与数据库记录已删除
    await ask(Platform.WECHAT, 'fresh')
    assert engine.histories[-1][1] == []
    await ask(Platform.WECHAT, 'follow-up')
    assert 'fresh' in engine.histories[-1][1]
    assert 'old-wechat-secret' not in engine.histories[-1][1]
    await ask(Platform.DINGTALK, 'other-platform')
    assert 'old-dingtalk-secret' in engine.histories[-1][1]
    assert any(m.chat_id == 'same' and m.platform == Platform.DINGTALK for m in agent.store.list_messages())
    await agent.shutdown()
    restored_engine = HistoryEngine()
    restored = service(db, restored_engine)
    await restored.ingest(ChatMessage(platform=Platform.WECHAT, chat_id='same', content='after restart'))
    history = restored_engine.histories[-1][1]
    assert 'fresh' in history and 'old-wechat-secret' not in history
    await restored.shutdown()
    db.close()


@pytest.mark.asyncio
async def test_persisted_pi_token_applies_after_profile_engine_rebuild(tmp_path):
    db = SqliteStore(str(tmp_path / 'pi-boundary.db'))
    engine = ACPAgentGenerator(ACPSettings(mode='pi', command='pi'))
    agent = service(db, engine)
    key = ('wechat', 'same')
    original = engine._pi_session_id(key)
    agent.reset_contact_session(Platform.WECHAT, 'same')
    agent._apply_session_token(engine, Platform.WECHAT, 'same')
    reset_id = engine._pi_session_id(key)
    assert original != reset_id
    rebuilt = ACPAgentGenerator(ACPSettings(mode='pi', command='pi'))
    restored = service(db, rebuilt)
    restored._apply_session_token(rebuilt, Platform.WECHAT, 'same')
    assert rebuilt._pi_session_id(key) == reset_id
    restored.reset_contact_session(Platform.WECHAT, 'same')
    assert rebuilt._pi_session_id(key) not in {original, reset_id}
    await agent.shutdown()
    await restored.shutdown()
    db.close()
