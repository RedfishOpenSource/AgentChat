"""基于规则的 Mock 回复实现：确定性行为，用于开发与模拟测试。"""
from __future__ import annotations

from typing import Sequence

from ..models import ChatMessage
from .base import Candidate, GenerationResult, ReplyGenerator


class MockReplyGenerator(ReplyGenerator):
    name = "mock"

    _GREETINGS = ("你好", "您好", "hello", "hi", "在吗", "在么")
    _THANKS = ("谢谢", "感谢", "thanks", "多谢", "辛苦")
    _BYE = ("再见", "拜拜", "bye", "晚安")
    _QUESTION_MARKS = ("?", "？")

    async def generate(self, message: ChatMessage, history: Sequence[ChatMessage]) -> GenerationResult:
        text = message.content.strip()
        low = text.lower()

        if any(g in low for g in self._GREETINGS):
            candidates = [
                f"你好，{message.sender or '朋友'}！很高兴收到你的消息～",
                "你好呀，请问有什么可以帮你的吗？",
            ]
        elif any(t in low for t in self._THANKS):
            candidates = ["不客气，有需要随时找我～", "客气啦，应该的！"]
        elif any(b in low for b in self._BYE):
            candidates = ["再见，有空常聊！", "拜拜，祝一切顺利～"]
        elif any(q in text for q in self._QUESTION_MARKS):
            candidates = [
                f"关于你提到的“{text[:30]}”，我的理解是这样的……（mock 回复）",
                "这个问题我需要确认一下细节，稍后给你详细答复。",
            ]
        else:
            candidates = [
                f"收到你的消息啦：“{text[:50]}”。（mock 自动回复）",
                "好的，我已经记录，会尽快跟进。",
            ]

        chosen = candidates[0]
        return GenerationResult(
            content=chosen,
            candidates=[Candidate(content=c, passed=True) for c in candidates],
            meta={"engine": self.name, "rule": "keyword",
                  "candidates": [Candidate(content=c, passed=True).to_dict() for c in candidates]},
        )
