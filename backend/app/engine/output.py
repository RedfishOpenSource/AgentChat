"""隔离 ACP 适配器的启动横幅，保留真正的答复（不按关键词删除普通回答）。"""
from __future__ import annotations

import re

_SECTION = re.compile(r"^##\s+(Context|Skills|Extensions|Prompts|Themes)\s*$")
_VERSION = re.compile(r"^pi v?\d+\.\d+[^\n]*$")
_RESOURCE = re.compile(r"^\s*-\s+(?:[A-Za-z]:[\\/]|[./~\\]|npm:|/)[^\n]*$")


def clean_agent_reply(text: str) -> str:
    """仅移除开头的已知启动资源块；合法 Context/Skills 标题及业务列表原样保留。"""
    lines = text.splitlines(keepends=True)
    end = 0
    pos = 0
    while pos < len(lines):
        line = lines[pos].strip()
        if not line or line == '---':
            pos += 1
            continue
        if _VERSION.fullmatch(line):
            pos += 1
            end = pos
            continue
        if not _SECTION.fullmatch(line):
            break
        next_pos = pos + 1
        found = False
        while next_pos < len(lines):
            resource = lines[next_pos].rstrip('\r\n')
            if not resource.strip():
                next_pos += 1
            elif _RESOURCE.fullmatch(resource):
                found = True
                next_pos += 1
            else:
                break
        if not found:
            break
        pos = next_pos
        end = pos
    return ''.join(lines[end:]).strip()


def is_pi_notification(text: str) -> bool:
    """pi-acp 将重试/压缩通知也当作 agent_message_chunk 发送。"""
    text = text.strip()
    return text in {
        'Retry finished, resuming.',
        'Context nearing limit, running automatic compaction...',
        'Automatic compaction finished; context was summarized to continue the session.',
        'Cleared queued prompts.',
        'Retrying...',
    } or bool(re.fullmatch(r'(?:Queued message \(position \d+\)\.|Retrying \(attempt \d+/\d+, waiting \d+s\)\.\.\.)', text))
