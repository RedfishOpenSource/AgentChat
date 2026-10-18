"""图片理解（可选视觉模型）测试：识别成功、失败兜底、未配置占位、meta 图片源解析。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import LLMSettings, load_settings
from app.core.agent import AgentService
from app.engine import MockReplyGenerator
from app.engine.vision import VisionDescriber, resolve_image_source
from app.connectors.wechat import _parse_replica_msg
from app.models import ChatMessage, Platform
from app.store import EventBus, MessageStore

from conftest import make_settings


@pytest.fixture(autouse=True)
def _no_builtin_vision(monkeypatch):
    """阻止 AgentService.__init__ 按配置创建真实视觉 httpx 客户端；
    需要视觉能力的用例通过 build_agent(transport=...) 显式注入 Mock。"""
    monkeypatch.setattr(AgentService, "_build_vision", staticmethod(lambda settings: None))


def build_agent(settings, transport: httpx.AsyncBaseTransport | None = None) -> AgentService:
    store, bus = MessageStore(), EventBus()
    agent = AgentService(settings=settings, store=store, bus=bus, engine=MockReplyGenerator())
    agent.setup_connectors()
    if transport is not None:
        agent._vision = VisionDescriber(
            base_url="http://vision.test", api_key="k", model="vl-x", transport=transport
        )
    return agent


def vision_settings() -> object:
    return make_settings(
        llm=LLMSettings(vision_model="vl-x", vision_base_url="http://vision.test",
                        vision_api_key="k", max_history=4)
    )


def image_msg(image_path: str = "") -> ChatMessage:
    meta = {"image_path": image_path} if image_path else {}
    return ChatMessage(platform=Platform.WECHAT, chat_id="c1", sender="王五",
                       content="[图片]", msg_type="image", meta=meta)


async def last_reply(agent: AgentService):
    replies = [m for m in agent.store.list_messages(chat_id="c1", direction="out")]
    assert replies, "应生成一份草稿"
    return replies[-1]


def ok_transport(captured: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "图片是一张报销单照片"}}]})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_image_with_vision_describes(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG-fake-bytes")  # Mock 服务不校验内容
    agent = build_agent(vision_settings(), transport=ok_transport())
    await agent.ingest(image_msg(str(png)))
    reply = await last_reply(agent)
    assert reply.content == "[图片] AI 识别：图片是一张报销单照片"
    assert reply.meta["engine"] == "vision"
    assert reply.status == "pending"
    assert len(agent.connectors[Platform.WECHAT].sent) == 0  # 绝不自动发送
    await agent.stop_all()


@pytest.mark.asyncio
async def test_image_vision_payload_is_multimodal(tmp_path):
    png = tmp_path / "shot.jpg"
    png.write_bytes(b"jpegdata")
    captured: dict = {}
    agent = build_agent(vision_settings(), transport=ok_transport(captured))
    await agent.ingest(image_msg(str(png)))
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text" and content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    await agent.stop_all()


@pytest.mark.asyncio
async def test_image_without_vision_config_placeholder():
    agent = build_agent(make_settings())  # 未配 vision_model
    assert agent._vision is None
    await agent.ingest(image_msg())  # 连 meta 都没有
    reply = await last_reply(agent)
    assert reply.content == "[收到图片消息，暂不支持内容识别，请人工处理]"
    assert reply.meta["placeholder"] is True
    await agent.stop_all()


@pytest.mark.asyncio
async def test_image_vision_error_falls_back_to_placeholder(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "vision down"})

    agent = build_agent(vision_settings(), transport=httpx.MockTransport(handler))
    await agent.ingest(image_msg(str(tmp_path / "x.png")))
    reply = await last_reply(agent)
    assert reply.content == "[收到图片消息，暂不支持内容识别，请人工处理]"
    assert reply.meta["placeholder"] is True
    assert reply.meta.get("vision_error")  # 记录了失败原因，便于排查
    await agent.stop_all()


@pytest.mark.asyncio
async def test_image_without_source_meta_skips_vision():
    agent = build_agent(vision_settings(), transport=ok_transport())
    await agent.ingest(image_msg())  # 有视觉模型但无图片源 → 占位草稿
    reply = await last_reply(agent)
    assert reply.meta["placeholder"] is True
    await agent.stop_all()


@pytest.mark.asyncio
async def test_non_image_non_text_still_placeholder():
    agent = build_agent(vision_settings(), transport=ok_transport())
    msg = ChatMessage(platform=Platform.WECHAT, chat_id="c1", sender="赵六",
                      content="", msg_type="voice", meta={"image_path": "x.png"})
    await agent.ingest(msg)
    reply = await last_reply(agent)
    assert "语音" in reply.content and reply.meta["engine"] == "system"
    await agent.stop_all()


# ---- meta 图片源解析 ----
def test_resolve_image_source_nested():
    assert resolve_image_source({"replica": {"image_path": "D:/a.png"}}) == "D:/a.png"
    assert resolve_image_source({"image_url": "http://x/b.png"}) == "http://x/b.png"
    assert resolve_image_source({"replica": {"username": "wxid"}}) == ""
    assert resolve_image_source(None) == ""


def test_resolve_image_source_prefers_path_over_url():
    src = resolve_image_source({"image_url": "http://x/b.png", "image_path": "D:/a.png"})
    assert src == "D:/a.png"


# ---- 真实连接器透传 ----
def test_replica_msg_carries_image_meta():
    msg = _parse_replica_msg(
        {"content": "", "type": "图片", "sender_id": 1, "sender_username": "wxid_u",
         "create_time": 1, "image_path": "D:/img/cache/abc.png"},
        chat_name="文件传输助手", username="filehelper",
        nick_resolver=lambda _: "张三", self_nick="我",
    )
    assert msg is not None and msg.msg_type == "image"
    assert msg.meta["replica"]["image_path"] == "D:/img/cache/abc.png"


def test_replica_empty_voice_not_dropped():
    """回归：图片/语音等非文本消息内容常为空，不能被当成空行丢弃。"""
    msg = _parse_replica_msg(
        {"content": "", "type": "语音", "sender_id": 2, "sender_username": "wxid_v",
         "create_time": 1},
        chat_name="文件传输助手", username="filehelper",
        nick_resolver=lambda _: "李四", self_nick="我",
    )
    assert msg is not None and msg.msg_type == "voice"
    assert msg.content == "[语音]"


def test_replica_empty_text_still_dropped():
    """文本空内容仍按空行丢弃（系统行）。"""
    msg = _parse_replica_msg(
        {"content": "", "type": "文本", "sender_id": 3, "sender_username": "wxid_t",
         "create_time": 1},
        chat_name="文件传输助手", username="filehelper",
        nick_resolver=lambda _: "王五", self_nick="我",
    )
    assert msg is None


# ---- 视觉配置加载 ----
def test_vision_config_from_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("CHATAGENT_LLM_VISION_MODEL", "vl-env-x")
    monkeypatch.setenv("CHATAGENT_LLM_VISION_BASE_URL", "http://v.test")
    monkeypatch.setenv("CHATAGENT_LLM_VISION_API_KEY", "vk")
    s = load_settings(str(cfg))
    assert s.llm.vision_model == "vl-env-x"
    assert s.llm.vision_base_url == "http://v.test"
    assert s.llm.vision_api_key == "vk"
