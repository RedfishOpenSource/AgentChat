"""监控告警测试：AlertManager 去重/节流/恢复、LLM 失败告警、连接器巡检。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.alerts import AlertManager, build_notifier
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.models import Platform
from app.store import EventBus, MessageStore

from conftest import make_settings


class StubNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def test_raise_resolve_and_dedupe():
    notifier = StubNotifier()
    mgr = AlertManager(notifier=notifier, min_interval=0.0)

    assert mgr.raise_alert("conn", "连接器挂了") is True
    assert mgr.is_active("conn")
    assert mgr.raise_alert("conn", "还是挂的") is False  # 去重：不重复通知
    assert mgr.resolve_alert("conn") is True
    assert not mgr.is_active("conn")
    assert mgr.resolve_alert("conn") is False  # 已恢复，不再通知


def test_throttle():
    notifier = StubNotifier()
    clock = {"t": 0.0}
    mgr = AlertManager(notifier=notifier, min_interval=60.0, clock=lambda: clock["t"])

    mgr.raise_alert("a", "第一次")
    clock["t"] = 10.0
    mgr.resolve_alert("a")
    mgr.raise_alert("b", "十秒后第二次")  # 在节流窗口内 → 只记日志不发送
    clock["t"] = 100.0
    mgr.resolve_alert("b")  # 超过节流窗口 → 发送


def test_throttle_and_send_delivery():
    """异步环境中通知实际送达（含节流与恢复通知）。"""

    async def main():
        notifier = StubNotifier()
        clock = {"t": 0.0}
        mgr = AlertManager(notifier=notifier, min_interval=60.0, clock=lambda: clock["t"])
        mgr.raise_alert("a", "第一次告警")
        await asyncio.sleep(0.01)
        assert notifier.sent == ["⚠️ ChatAgent 告警 [a] 第一次告警"]
        clock["t"] = 10.0
        mgr.raise_alert("b", "节流窗口内")
        mgr.resolve_alert("b")
        await asyncio.sleep(0.01)
        assert len(notifier.sent) == 1  # 节流中，未发送
        clock["t"] = 100.0
        mgr.resolve_alert("a")
        await asyncio.sleep(0.01)
        assert len(notifier.sent) == 2
        assert "已恢复" in notifier.sent[1]

    asyncio.run(main())


def test_build_notifier_channels():
    assert build_notifier("off") is None
    assert build_notifier("") is None
    assert build_notifier("wechat-filehelper").name == "wechat-filehelper"
    assert build_notifier("dingtalk-self", dingtalk_chat_id="cidX").name == "dingtalk-self"
    assert build_notifier("bogus") is None


@pytest.mark.asyncio
async def test_llm_failure_streak_triggers_alert_and_success_resolves():
    class FlakyEngine(MockReplyGenerator):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def generate(self, message, history, kb=None):
            if self.fail:
                raise RuntimeError("llm down")
            return await super().generate(message, history)

    settings = make_settings(llm_failure_threshold=2)
    engine = FlakyEngine()
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=engine,
        alerts=AlertManager(notifier=StubNotifier(), min_interval=0.0),
    )
    agent.setup_connectors()

    from app.models import ChatMessage

    async def send(content):
        await agent.ingest(ChatMessage(
            platform=Platform.WECHAT, chat_id="c1", sender="x", content=content
        ))

    # 连续失败 2 次 → 告警激活
    await send("m0")
    await send("m1")
    assert agent.alerts.is_active("llm-failure")

    # 恢复 → 告警解除
    engine.fail = False
    await send("再来")
    assert not agent.alerts.is_active("llm-failure")
    await agent.stop_all()


@pytest.mark.asyncio
async def test_connector_watcher_alerts_on_error_and_resolves():
    settings = make_settings(alert_check_interval=0.05)
    notifier = StubNotifier()
    agent = AgentService(
        settings=settings, store=MessageStore(), bus=EventBus(), engine=MockReplyGenerator(),
        alerts=AlertManager(notifier=notifier, min_interval=0.0),
    )
    agent.setup_connectors()
    await agent.start_all()
    async def wait_until(fn, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if fn():
                return True
            await asyncio.sleep(0.05)
        return False

    try:
        conn = agent.connectors[Platform.WECHAT]
        conn.status = "error"
        conn.last_error = "微信窗口不见了"
        assert await wait_until(lambda: agent.alerts.is_active("connector-wechat"))

        conn.status = "running"
        assert await wait_until(lambda: not agent.alerts.is_active("connector-wechat"))
    finally:
        await agent.shutdown()


def test_status_api_includes_alerts(client):
    data = client.get("/api/status").json()
    assert "alerts" in data and isinstance(data["alerts"], list)
