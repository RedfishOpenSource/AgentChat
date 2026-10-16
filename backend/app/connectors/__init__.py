"""连接器工厂。"""
from __future__ import annotations

from ..config import ConnectorSettings, Settings
from ..models import Platform
from .base import BaseConnector
from .dingtalk import DingTalkCliConnector, DingTalkDwsConnector
from .mock import MockConnector
from .wechat import WeChatConnector, WeChatReplicaConnector

__all__ = [
    "build_connectors",
    "BaseConnector",
    "MockConnector",
    "WeChatConnector",
    "WeChatReplicaConnector",
    "DingTalkCliConnector",
    "DingTalkDwsConnector",
]


def _build(platform: Platform, cs: ConnectorSettings, on_messages) -> BaseConnector:
    if platform == Platform.WECHAT and cs.mode == "replica":
        return WeChatReplicaConnector(cs, on_messages)
    if platform == Platform.WECHAT and cs.mode == "wxauto":
        return WeChatConnector(cs, on_messages)
    if platform == Platform.DINGTALK and cs.mode == "dws":
        return DingTalkDwsConnector(cs, on_messages)
    if platform == Platform.DINGTALK and cs.mode == "cli":
        return DingTalkCliConnector(cs, on_messages)
    return MockConnector(platform, cs, on_messages)


def build_connectors(settings: Settings, on_messages) -> dict[Platform, BaseConnector]:
    connectors: dict[Platform, BaseConnector] = {}
    for name in ("wechat", "dingtalk"):
        cs: ConnectorSettings = getattr(settings, name)
        if not cs.enabled:
            continue
        platform = Platform(name)
        connectors[platform] = _build(platform, cs, on_messages)
    return connectors
