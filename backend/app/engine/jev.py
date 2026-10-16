"""JEV 结构化判断模块（可选）。

参考 jev-chat 项目的流程：
  JEV 预判（意图/风险/建议行动） → LLM 生成候选 → JEV 复审（质量门禁 + 排序）

两种实现：
- MockJEVJudge：本地规则实现，无需外部服务
- TypeSafeJEVJudge：调用 TypeSafe 官方 POST /v1/systemone，失败自动降级
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from ..models import ChatMessage
from ..config import JEVSettings
from .base import Candidate

logger = logging.getLogger(__name__)

_RISK_KEYWORDS = ("转账", "红包", "汇款", "打款", "付款", "密码", "验证码", "银行卡", "身份证")


@dataclass
class JEVPrefilter:
    """预判结果：对收到的消息做结构化分析。"""

    risk_level: str = "low"  # low | medium | high
    intent: str = ""
    user_need: str = ""
    suggested_action: str = "reply"
    substantive_ok: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class JEVReviewResult:
    """复审结果：给候选打分、判断是否通过并排序。"""

    ranking: list[int] = field(default_factory=list)  # 候选下标，按推荐度从高到低
    scores: list[float] = field(default_factory=list)
    passed: list[bool] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class JEVJudge(ABC):
    """JEV 判断接口。"""

    provider: str = "base"
    enabled: bool = False

    @abstractmethod
    async def pre_judge(self, message: ChatMessage, history: Sequence[ChatMessage]) -> JEVPrefilter | None:
        """预判；返回 None 表示本次不判断（降级）。"""

    @abstractmethod
    async def review(self, message: ChatMessage, candidates: Sequence[Candidate]) -> JEVReviewResult | None:
        """复审候选；返回 None 表示本次不复审（降级）。"""

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _transcript(message: ChatMessage, history: Sequence[ChatMessage], limit: int = 12) -> str:
    lines = [f"{m.sender}: {m.content}" for m in list(history)[-limit:]]
    lines.append(f"{message.sender}: {message.content}")
    return "\n".join(lines)


class MockJEVJudge(JEVJudge):
    """本地规则版 JEV：用于无外部服务时的判断与测试。"""

    provider = "mock"
    enabled = True

    async def pre_judge(self, message: ChatMessage, history: Sequence[ChatMessage]) -> JEVPrefilter:
        text = message.content
        risky = [k for k in _RISK_KEYWORDS if k in text]
        has_question = any(q in text for q in ("?", "？"))
        risk_level = "high" if risky else ("medium" if has_question else "low")
        return JEVPrefilter(
            risk_level=risk_level,
            intent="涉及资金/敏感信息" if risky else ("咨询" if has_question else "日常沟通"),
            user_need="对方希望得到明确答复" if risk_level != "low" else "普通闲聊",
            suggested_action="谨慎回复，拒绝资金与敏感操作" if risky else "reply",
            substantive_ok=not risky,
            raw={"keywords": risky},
        )

    async def review(self, message: ChatMessage, candidates: Sequence[Candidate]) -> JEVReviewResult:
        scores: list[float] = []
        passed: list[bool] = []
        reasons: list[str] = []
        for c in candidates:
            risky = [k for k in _RISK_KEYWORDS if k in c.content]
            score = min(len(c.content) / 60.0, 1.0)
            ok = (not risky) and (2 <= len(c.content) <= 500)
            scores.append(round(score, 3))
            passed.append(ok)
            reasons.append("包含敏感词，禁止发送" if risky else ("内容合理" if ok else "内容过短或过长"))
        ranking = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
        return JEVReviewResult(
            ranking=ranking, scores=scores, passed=passed, reasons=reasons, raw={"provider": "mock"}
        )


class TypeSafeJEVJudge(JEVJudge):
    """TypeSafe 官方 JEV（POST {base_url}/v1/systemone）。

    任何网络/接口错误都自动降级：返回 None，由 LLM 自行判断，不阻塞主流程。
    """

    provider = "typesafe"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "jev-latest",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.enabled = bool(api_key)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/systemone", json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 —— 降级策略
            logger.warning("TypeSafe JEV 调用失败，已降级: %s", exc)
            return None

    async def pre_judge(self, message: ChatMessage, history: Sequence[ChatMessage]) -> JEVPrefilter | None:
        data = await self._call(
            {"model": self._model, "stage": "pre", "transcript": _transcript(message, history)}
        )
        if not data:
            return None
        a = data.get("assessment", data)
        return JEVPrefilter(
            risk_level=str(a.get("risk_level", "low")),
            intent=str(a.get("intent", "")),
            user_need=str(a.get("user_need", "")),
            suggested_action=str(a.get("suggested_action", "reply")),
            substantive_ok=bool(a.get("substantive_ok", True)),
            raw=data,
        )

    async def review(self, message: ChatMessage, candidates: Sequence[Candidate]) -> JEVReviewResult | None:
        n = len(candidates)
        data = await self._call({"model": self._model, "stage": "review", "candidates": [c.content for c in candidates]})
        if not data:
            return None
        r = data.get("review", data)
        scores = [float(x) for x in (r.get("scores") or [0.0] * n)][:n]
        passed = [bool(x) for x in (r.get("passed") or [True] * n)][:n]
        ranking = [int(i) for i in (r.get("ranking") or list(range(n)))]
        reasons = [str(x) for x in (r.get("reasons") or [""] * n)][:n]
        return JEVReviewResult(ranking=ranking, scores=scores, passed=passed, reasons=reasons, raw=data)

    async def aclose(self) -> None:
        await self._client.aclose()


def build_jev(cfg: JEVSettings) -> JEVJudge | None:
    """根据配置构建 JEV 实例；未启用返回 None。"""
    if not cfg.enabled:
        return None
    if cfg.provider == "typesafe":
        return TypeSafeJEVJudge(api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model)
    return MockJEVJudge()
