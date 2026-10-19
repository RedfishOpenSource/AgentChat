"""基于 OpenAI 兼容 Chat Completions 的回复实现（默认 DeepSeek）。

流程参考 jev-chat 项目：
  JEV 预判(可选) → LLM 生成候选 → JEV 复审排序(可选)
  → 全部不合格时重写一次 → 仍不合格则拦截(blocked)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Sequence

import httpx

from ..config import LLMSettings
from ..models import ChatMessage
from ..rag import Chunk, KnowledgeBase
from .base import Candidate, GenerationResult, ReplyGenerator
from .jev import JEVPrefilter, JEVJudge

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是一个工作助手，在微信/钉钉等即时通讯场景中帮用户回复对方的消息。"
    "要求：口语化、简洁、礼貌，符合中文聊天习惯；不要输出任何解释性文字；"
    "涉及转账、红包、付款、密码、验证码等资金或敏感操作时，必须明确拒绝并提醒风险。"
)

# 回复风格预设（llm.style）：追加到系统提示词；default 用内置默认语气
STYLE_PRESETS: dict[str, str] = {
    "default": "",  # 内置默认：口语化、简洁、礼貌
    "formal": "正式、专业、礼貌，用词严谨规范，适合工作场合沟通；不使用表情符号。",
    "concise": "极简高效：一两条短句说完，不寒暄不铺垫，直接给结论或行动项。",
    "casual": "轻松口语化，像朋友聊天，可以适当用语气词和 emoji。",
    "friendly": "热情友好，语气亲切自然，让对方感到被重视；结尾可主动提供进一步帮助。",
}


class LLMReplyGenerator(ReplyGenerator):
    name = "llm"

    def __init__(
        self,
        cfg: LLMSettings,
        jev: JEVJudge | None = None,
        rewrite_on_fail: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        kb: KnowledgeBase | None = None,
    ) -> None:
        self.cfg = cfg
        self.jev = jev
        self.rewrite_on_fail = rewrite_on_fail
        self.kb = kb  # RAG 知识库（可选）：检索结果注入用户提示词
        self._client = httpx.AsyncClient(timeout=cfg.timeout, transport=transport)

    # ---------- LLM 调用 ----------
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    async def _chat(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        resp = await self._client.post(url, json=payload, headers=self._headers())
        if resp.is_error:
            # 把服务商返回的错误详情（如智谱 1113「余额不足」）带进异常信息，便于前端与告警直接定位
            detail = (resp.text or "").strip()[:300] or resp.reason_phrase
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {detail}", request=resp.request, response=resp
            )
        data = resp.json()
        msg = data["choices"][0].get("message") or {}
        content = str(msg.get("content") or "")
        if not content.strip():
            # 兼容思考型模型（如 glm-5.3-flash）：思考阶段 content 可能为空，正文在 reasoning_content
            content = str(msg.get("reasoning_content") or "")
        return content

    async def _chat_with_retry(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        """带重试的 LLM 调用：仅对超时/网络错误/429/5xx 重试，指数退避；4xx 快速失败。"""
        attempts = max(1, int(getattr(self.cfg, "max_retries", 0)) + 1)
        backoff = float(getattr(self.cfg, "retry_backoff", 1.0))
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await self._chat(messages, temperature)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code != 429 and code < 500:
                    raise  # 401/400 等客户端错误重试无意义
                last_exc = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
            if i < attempts - 1:
                delay = backoff * (2 ** i)
                logger.warning(
                    "[llm] 调用失败(%d/%d): %s，%.1fs 后重试", i + 1, attempts, last_exc, delay
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # ---------- 提示词 ----------
    def _system_prompt(self) -> str:
        """系统提示词：内置基础要求 + 风格预设（llm.style，热更新即时生效）。"""
        style = STYLE_PRESETS.get((self.cfg.style or "default").strip(), "")
        return f"{_SYSTEM_PROMPT}\n回复风格要求：{style}" if style else _SYSTEM_PROMPT

    def _build_user_prompt(
        self,
        message: ChatMessage,
        history: Sequence[ChatMessage],
        assessment: JEVPrefilter | None,
        feedback: str = "",
        rag_hits: list[tuple[float, Chunk]] | None = None,
    ) -> str:
        lines = ["以下是最近的聊天记录（最后一条是需要回复的新消息）："]
        for m in list(history)[-self.cfg.max_history:]:
            lines.append(f"{m.sender}: {m.content}")
        lines.append(f"{message.sender}: {message.content}")

        if assessment:
            lines += [
                "",
                "[JEV 结构化判断]",
                f"风险等级: {assessment.risk_level}",
                f"真实意图: {assessment.intent}",
                f"对方需要: {assessment.user_need}",
                f"建议行动: {assessment.suggested_action}",
            ]
        if rag_hits:
            lines += ["", "[知识库参考资料]（可参考以下内容回复；资料中没有的信息不要编造）"]
            for _, chunk in rag_hits:
                lines.append(f"- {chunk.text}")
        if feedback:
            lines += ["", f"[注意] 上一轮候选未通过质量复审（原因：{feedback}），请重新生成更合适的回复。"]

        n = max(1, self.cfg.num_candidates)
        lines += [
            "",
            f"请生成 {n} 条候选回复，语气和长度略有差异。",
            '只输出 JSON，不要输出其他内容，格式：{"candidates": ["回复1", "回复2"]}',
        ]
        return "\n".join(lines)

    # ---------- 结果解析 ----------
    def _parse_candidates(self, raw: str) -> list[str]:
        text = (raw or "").strip()

        def _from_obj(obj: Any) -> list[str]:
            if isinstance(obj, dict) and isinstance(obj.get("candidates"), list):
                return [str(x).strip() for x in obj["candidates"] if str(x).strip()]
            if isinstance(obj, list):
                return [str(x).strip() for x in obj if str(x).strip()]
            return []

        # 1) 直接 JSON
        try:
            items = _from_obj(json.loads(text))
            if items:
                return items
        except (json.JSONDecodeError, ValueError):
            pass
        # 2) 从文本中提取 JSON 片段
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if m:
            try:
                items = _from_obj(json.loads(m.group(0)))
                if items:
                    return items
            except (json.JSONDecodeError, ValueError):
                pass
        # 3) 逐行解析（"1. xxx" 列表或纯文本）
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\d+[.、)]\s*", "", line).strip()
            line = line.strip("\"'“”")
            if line and not line.startswith(("{", "[", "}", "]")) and not line.lower().startswith("json"):
                items.append(line)
        return items

    # ---------- 主流程 ----------
    @staticmethod
    def _pick_best(candidates: list[Candidate], review: Any) -> Candidate | None:
        if review is None:
            return candidates[0] if candidates else None
        for i in review.ranking:
            if 0 <= i < len(candidates) and candidates[i].passed:
                return candidates[i]
        for c in candidates:
            if c.passed:
                return c
        return None

    async def generate(
        self, message: ChatMessage, history: Sequence[ChatMessage], kb: "KnowledgeBase | None" = None
    ) -> GenerationResult:
        jev_on = bool(self.jev and self.jev.enabled)

        # 1) JEV 预判（可选）
        assessment: JEVPrefilter | None = None
        if jev_on:
            assessment = await self.jev.pre_judge(message, history)  # type: ignore[union-attr]

        # RAG 知识库检索（可选）：调用方按平台传入 kb（不传用构造时的默认库）
        kb = kb if kb is not None else self.kb
        rag_hits = await kb.search_hybrid(message.content) if kb is not None else []

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._build_user_prompt(message, history, assessment, rag_hits=rag_hits)},
        ]
        raw = await self._chat_with_retry(messages)
        contents = self._parse_candidates(raw) or [raw.strip()]
        candidates = [Candidate(content=c) for c in contents]

        blocked = False
        meta: dict[str, Any] = {
            "engine": self.name,
            "model": self.cfg.model,
            "jev": jev_on,
            "assessment": assessment.__dict__ if assessment else None,
        }
        if rag_hits:
            meta["rag"] = sorted({c.file for _, c in rag_hits})  # 引用的知识库文件

        # 2) JEV 复审 + 排序（可选），不合格时重写一次
        if jev_on and candidates:
            review = await self.jev.review(message, candidates)  # type: ignore[union-attr]
            if review:
                for i, c in enumerate(candidates):
                    if i < len(review.scores):
                        c.score = review.scores[i]
                    if i < len(review.passed):
                        c.passed = review.passed[i]
                    if i < len(review.reasons):
                        c.reason = review.reasons[i]
            best = self._pick_best(candidates, review)

            if best is None and self.rewrite_on_fail:
                reasons = "；".join(x for x in (review.reasons if review else []) if x) or "质量复审未通过"
                meta["rewritten"] = True
                messages[1]["content"] = self._build_user_prompt(
                    message, history, assessment, feedback=reasons, rag_hits=rag_hits
                )
                raw2 = await self._chat_with_retry(messages, temperature=min(1.2, self.cfg.temperature + 0.2))
                contents2 = self._parse_candidates(raw2) or [raw2.strip()]
                candidates = [Candidate(content=c) for c in contents2]
                review2 = await self.jev.review(message, candidates)  # type: ignore[union-attr]
                if review2:
                    for i, c in enumerate(candidates):
                        if i < len(review2.scores):
                            c.score = review2.scores[i]
                        if i < len(review2.passed):
                            c.passed = review2.passed[i]
                        if i < len(review2.reasons):
                            c.reason = review2.reasons[i]
                best = self._pick_best(candidates, review2)

            if best is None:  # 重写后仍全部不合格 → 拦截
                blocked = True
                best = candidates[0]
        else:
            best = candidates[0]

        meta["candidates"] = [c.to_dict() for c in candidates]
        if blocked:
            meta["blocked_reason"] = "JEV 复审未通过，已拦截（可人工确认后发送）"
        return GenerationResult(content=best.content, candidates=candidates, blocked=blocked, meta=meta)

    async def aclose(self) -> None:
        await self._client.aclose()
