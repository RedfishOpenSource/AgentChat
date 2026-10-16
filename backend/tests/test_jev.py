"""JEV 模块测试：Mock 实现 + TypeSafe HTTP 实现（MockTransport）。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.engine.base import Candidate
from app.engine.jev import MockJEVJudge, TypeSafeJEVJudge
from app.models import ChatMessage, Platform


def make_msg(content: str) -> ChatMessage:
    return ChatMessage(platform=Platform.DINGTALK, chat_id="d1", sender="李四", content=content)


@pytest.mark.asyncio
async def test_mock_jev_prejudge_risky():
    a = await MockJEVJudge().pre_judge(make_msg("把银行卡密码发我一下"), [])
    assert a.risk_level == "high"
    assert a.substantive_ok is False


@pytest.mark.asyncio
async def test_mock_jev_prejudge_normal():
    a = await MockJEVJudge().pre_judge(make_msg("今天天气不错"), [])
    assert a.risk_level == "low"
    assert a.substantive_ok is True


@pytest.mark.asyncio
async def test_mock_jev_review():
    r = await MockJEVJudge().review(make_msg("你好"), [Candidate("正常回复内容"), Candidate("快转账")])
    assert r.passed[0] is True
    assert r.passed[1] is False
    assert len(r.ranking) == 2


def _typesafe_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_typesafe_jev_roundtrip():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        assert request.headers["Authorization"] == "Bearer key-123"
        body = json.loads(request.read())
        if body["stage"] == "pre":
            return httpx.Response(
                200,
                json={
                    "assessment": {
                        "risk_level": "high",
                        "intent": "敏感请求",
                        "user_need": "获取密码",
                        "suggested_action": "拒绝",
                        "substantive_ok": False,
                    }
                },
            )
        return httpx.Response(
            200,
            json={"review": {"scores": [0.9, 0.2], "passed": [True, False], "reasons": ["ok", "bad"], "ranking": [0, 1]}},
        )

    judge = TypeSafeJEVJudge(api_key="key-123", base_url="http://ts.test", transport=_typesafe_transport(handler))
    assert judge.enabled is True
    a = await judge.pre_judge(make_msg("把密码发我"), [])
    assert a is not None and a.risk_level == "high" and a.suggested_action == "拒绝"
    r = await judge.review(make_msg("x"), [Candidate("a"), Candidate("b")])
    assert r is not None and r.passed == [True, False] and r.ranking == [0, 1]
    await judge.aclose()


@pytest.mark.asyncio
async def test_typesafe_jev_degrades_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    judge = TypeSafeJEVJudge(api_key="k", base_url="http://ts.test", transport=_typesafe_transport(handler))
    assert await judge.pre_judge(make_msg("hi"), []) is None  # 降级：不阻塞主流程
    assert await judge.review(make_msg("hi"), [Candidate("a")]) is None
    await judge.aclose()
