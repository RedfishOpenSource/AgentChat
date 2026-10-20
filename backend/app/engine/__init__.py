"""回复生成引擎工厂。"""
from __future__ import annotations
from typing import TYPE_CHECKING

from ..config import Settings
from .acp import ACPAgentGenerator
from .base import ReplyGenerator
from .llm import LLMReplyGenerator
from .mock import MockReplyGenerator

if TYPE_CHECKING:
    from ..rag import KnowledgeBase

__all__ = ["build_engine", "ReplyGenerator", "MockReplyGenerator", "LLMReplyGenerator", "ACPAgentGenerator"]


def build_engine(settings: Settings, kb: "KnowledgeBase | None" = None) -> ReplyGenerator:
    """按配置构建回复生成器。

    - engine=mock: 规则实现（默认，测试用）
    - engine=llm: DeepSeek 等 OpenAI 兼容实现；kb 传入时启用 RAG 知识库检索
    - engine=acp: 外部 Agent（ACP 协议 / pi RPC）：pi / claude code / codex / hermes / 自定义；
      每个联系人保持一个 Agent 会话，多轮问答共享上下文
    """
    if settings.engine == "llm":
        return LLMReplyGenerator(settings.llm, kb=kb)
    if settings.engine == "acp":
        return ACPAgentGenerator(settings.acp)
    return MockReplyGenerator()
