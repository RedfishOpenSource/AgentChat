"""生成进度：只使用 Mock 连接器与本地 Fake ACP，不向真实平台发送消息。"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.config import ACPSettings
from app.engine.acp import ACPAgentError, ACPAgentGenerator
from app.models import ChatMessage, Platform
from app.persistence import SqliteStore
from app.store import MessageStore
from tests.test_agent import build_agent
from tests.conftest import make_settings
from tests.test_acp_agent import FAKE_ACP_AGENT


@pytest.mark.asyncio
async def test_progress_debounce_completion_and_persistence(tmp_path):
    db = SqliteStore(str(tmp_path / "progress.db"))
    agent = build_agent(make_settings(debounce_seconds=0.03))
    agent.store = MessageStore(db=db)
    conn = agent.connectors[Platform.WECHAT]
    first = await conn.inject("progress", "甲", "一")
    second = await conn.inject("progress", "甲", "二")
    await agent.ingest(first)
    await agent.ingest(second)
    assert set(agent.processing_snapshot()) == {first.id, second.id}
    await agent._timers[agent._key(second)]
    assert agent.processing_snapshot() == {}
    assert first.meta["processing"]["phase"] == "skipped"
    assert second.meta["processing"]["phase"] == "completed"
    assert second.meta["processing"]["finished_at"] >= second.meta["processing"]["since"]
    restored = MessageStore(db=db)
    restored.restore()
    assert restored.get(second.id).meta["processing"]["phase"] == "completed"
    assert conn.sent == []
    await agent.shutdown()
    db.close()


@pytest.mark.asyncio
async def test_ignored_and_failed_generations_finish():
    agent = build_agent(make_settings(debounce_seconds=0.01))
    conn = agent.connectors[Platform.WECHAT]
    ignored = await conn.inject("group", "甲", "不@", is_group=True, at_me=False)
    await agent.ingest(ignored)
    await agent._timers[agent._key(ignored)]
    assert ignored.meta["processing"]["phase"] == "skipped"
    assert not agent.processing_snapshot()

    async def fail(*args, **kwargs):
        raise RuntimeError("测试连接中断")

    agent.engine.generate = fail
    incoming = await conn.inject("failure", "甲", "你好")
    await agent.ingest(incoming)
    await agent._timers[agent._key(incoming)]
    assert not agent.processing_snapshot()
    assert incoming.meta["processing"]["phase"] == "failed"
    assert "测试连接中断" in incoming.meta["processing"]["detail"]
    reply = agent.store.list_messages(chat_id="failure", direction="out")[0]
    assert reply.status == "pending" and reply.meta["error"] == "测试连接中断"
    assert conn.sent == []
    await agent.shutdown()


@pytest.mark.asyncio
async def test_timeout_and_overlapping_messages_do_not_leave_stale_progress():
    agent = build_agent(make_settings())
    conn = agent.connectors[Platform.WECHAT]
    entered, release = asyncio.Event(), asyncio.Event()
    original = agent.engine.generate

    async def slow(message, history, kb=None):
        entered.set()
        await release.wait()
        return await original(message, history, kb)

    agent.engine.generate = slow
    a = await conn.inject("same", "甲", "第一条")
    b = await conn.inject("same", "甲", "第二条")
    task_a = asyncio.create_task(agent.ingest(a))
    await entered.wait()
    task_b = asyncio.create_task(agent.ingest(b))
    await asyncio.sleep(0)
    assert set(agent.processing_snapshot()) == {a.id, b.id}
    snapshot = agent.processing_snapshot()
    snapshot[a.id]["steps"].clear()
    assert agent.processing_snapshot()[a.id]["steps"]  # 快照互不影响
    release.set()
    await asyncio.gather(task_a, task_b)
    assert not agent.processing_snapshot()

    release.clear()
    agent.save_contact_config("wechat", "timeout", {"timeout": 0.02})
    timed = await conn.inject("timeout", "甲", "超时")
    await agent.ingest(timed)
    assert timed.meta["processing"]["phase"] == "failed"
    assert "已停止等待" in timed.meta["processing"]["detail"]
    assert not agent.processing_snapshot()
    assert conn.sent == []
    await agent.shutdown()


@pytest.mark.asyncio
async def test_acp_streams_real_progress_filters_other_sessions_and_omits_private_reasoning(tmp_path):
    notifications = r'''
        for update in [
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "PRIVATE_REASONING"}},
            {"sessionUpdate": "tool_call", "title": "读取日期", "status": "in_progress"},
            {"sessionUpdate": "tool_call_update", "title": "读取日期", "status": "completed"},
        ]:
            print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "wrong-session", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OTHER_CONTACT"}}}}), flush=True)
'''
    script = tmp_path / "progress_agent.py"
    script.write_text(FAKE_ACP_AGENT.replace('        text = "".join', notifications + '        text = "".join'), encoding="utf-8")
    engine = ACPAgentGenerator(ACPSettings(command=sys.executable, args=str(script), timeout=5))
    progress = []
    try:
        result = await engine.generate(
            ChatMessage(platform=Platform.WECHAT, chat_id="progress", content="日期"), [],
            on_progress=lambda phase, **extra: progress.append({"phase": phase, **extra}),
        )
        phases = [p["phase"] for p in progress]
        assert phases[:4] == ["queued", "starting", "session", "thinking"]
        assert "tool" in phases and phases[-1] == "writing"
        assert "日期" in result.content
        assert "PRIVATE_REASONING" not in str(progress)
        assert "OTHER_CONTACT" not in result.content and "OTHER_CONTACT" not in str(progress)
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_acp_connection_exit_fails_promptly(tmp_path):
    script = tmp_path / "exit_agent.py"
    script.write_text("import sys\nfor line in sys.stdin:\n    sys.exit(1)\n", encoding="utf-8")
    engine = ACPAgentGenerator(ACPSettings(command=sys.executable, args=str(script), timeout=5))
    try:
        with pytest.raises(ACPAgentError, match="连接已断开"):
            await asyncio.wait_for(engine.generate(ChatMessage(platform=Platform.WECHAT, content="hello"), []), timeout=2)
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_bound_profile_progress_and_reply_end_to_end(tmp_path):
    script = tmp_path / 'profile_agent.py'
    script.write_text(FAKE_ACP_AGENT, encoding='utf-8')
    agent = build_agent(make_settings())
    agent.agent_profiles['support'] = {
        'id': 'support', 'name': '中台客服', 'engine': 'pi',
        'command': sys.executable, 'args': str(script), 'workspace': str(tmp_path),
    }
    agent.agent_assignments[('wechat', 'profile-chat')] = 'support'
    conn = agent.connectors[Platform.WECHAT]
    incoming = await conn.inject('profile-chat', '甲', '今天几号')
    try:
        await agent.ingest(incoming)
        reply = agent.store.list_messages(chat_id='profile-chat', direction='out')[0]
        assert reply.meta['mode'] == 'acp' and reply.status == 'pending'
        assert '今天几号' in reply.content
        state = incoming.meta['processing']
        assert state['agent'] == '中台客服' and state['phase'] == 'completed'
        assert 'writing' in [step['phase'] for step in state['steps']]
        assert '今天几号' in state['preview']
        assert not agent.processing_snapshot() and conn.sent == []
    finally:
        await agent.shutdown()
    assert not agent.profile_engines


def test_profile_pi_adapter_uses_standard_acp():
    agent = build_agent(make_settings())
    engine = agent.engine_for_profile({"id": "pi-profile", "engine": "pi", "command": "pi-acp"})
    assert engine.cfg.mode == "acp"
    legacy = agent.engine_for_profile({"id": "legacy-profile", "engine": "pi", "command": "pi"})
    assert legacy.cfg.mode == "pi"
