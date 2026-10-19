"""回复生成器抽象接口。

需求：生成回复先实现一个接口，后续可替换为 RAG 等复杂实现。
当前提供两种实现：
- MockReplyGenerator：基于规则的本地实现，用于开发与模拟测试
- LLMReplyGenerator：DeepSeek 等 OpenAI 兼容接口实现，可选接 JEV 预判/复审
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from ..models import ChatMessage

if TYPE_CHECKING:
    from ..rag import KnowledgeBase


@dataclass
class Candidate:
    """一条候选回复。"""

    content: str
    score: float | None = None
    passed: bool | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationResult:
    """生成结果：最终选中的回复 + 全部候选 + 元信息。"""

    content: str
    candidates: list[Candidate] = field(default_factory=list)
    blocked: bool = False  # True 表示被质量门禁拦截，不应发送
    meta: dict[str, Any] = field(default_factory=dict)


class ReplyGenerator(ABC):
    """回复生成器接口。新增实现（如 RAG）只需继承本类。"""

    name: str = "base"

    @abstractmethod
    async def generate(
        self, message: ChatMessage, history: Sequence[ChatMessage], kb: "KnowledgeBase | None" = None
    ) -> GenerationResult:
        """根据新消息与聊天上下文生成回复；kb 可选，覆盖构造时传入的默认知识库。"""

    async def aclose(self) -> None:  # pragma: no cover
        """释放底层资源（如 HTTP 连接），默认无需处理。"""
