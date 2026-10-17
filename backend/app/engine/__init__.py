"""回复生成引擎工厂。"""
from __future__ import annotations
from typing import TYPE_CHECKING

from ..config import Settings
from .base import ReplyGenerator
from .jev import JEVJudge, build_jev
from .llm import LLMReplyGenerator
from .mock import MockReplyGenerator

if TYPE_CHECKING:
    from ..rag import KnowledgeBase

__all__ = ["build_engine", "ReplyGenerator", "JEVJudge", "MockReplyGenerator", "LLMReplyGenerator"]


def build_engine(settings: Settings, kb: "KnowledgeBase | None" = None) -> tuple[ReplyGenerator, JEVJudge | None]:
    """按配置构建回复生成器与可选的 JEV 判断器。

    - engine=mock: 规则实现（默认，测试用）
    - engine=llm:  DeepSeek 等 OpenAI 兼容实现；kb 传入时启用 RAG 知识库检索
    """
    jev = build_jev(settings.jev)
    if settings.engine == "llm":
        engine: ReplyGenerator = LLMReplyGenerator(
            settings.llm, jev=jev, rewrite_on_fail=settings.jev.rewrite_on_fail, kb=kb
        )
        return engine, jev
    return MockReplyGenerator(), jev
