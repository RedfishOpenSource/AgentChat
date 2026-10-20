"""AI 角色改写：失败显式报错，禁止用原文伪装为改写成功。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import httpx


class RoleRewriteError(RuntimeError):
    pass


async def rewrite_role(settings, description: str, *, transport=None) -> str:
    llm = settings.llm
    if not llm.base_url.strip() or not llm.model.strip():
        raise RoleRewriteError("请配置用于角色改写的 LLM Base URL 和模型后重新保存")
    system = (
        "你是 Agent 角色提示词设计师。请重新编写和优化用户提供的角色描述，输出可直接作为"
        "系统提示词的中文角色设定，包含职责、工作步骤、输出风格和边界。即使输入已经是"
        "角色提示词，也必须重新组织改进，不能直接原样返回。保留用户指定的项目路径、"
        "分支和 worktree 规则、数据库/Redis/MQ/MCP 要求，不得编造配置或凭据。"
        "业务答复只保留结论、证据与必要操作，不输出内部推理、Context/Skills 加载信息。"
        "只输出角色提示词正文，不输出解释、寒暄或包裹全文的代码围栏。"
    )
    failure = "AI 未返回有效的角色提示词"
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), transport=transport) as client:
        for attempt in range(2):
            try:
                resp = await client.post(
                    llm.base_url.rstrip('/') + '/chat/completions',
                    headers={"Authorization": f"Bearer {llm.api_key}"} if llm.api_key else {},
                    json={"model": llm.model, "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": description},
                    ], "temperature": 0.4, "max_tokens": 8192},
                )
                resp.raise_for_status()
                value = resp.json()['choices'][0]['message'].get('content')
                if isinstance(value, str):
                    text = value.strip()
                    if text.startswith('```') and text.endswith('```'):
                        text = '\n'.join(text.splitlines()[1:-1]).strip()
                    if text and text != description.strip():
                        return text
                failure = "AI 返回了空内容或未经改写的原文，请重新保存重试"
            except httpx.HTTPStatusError as exc:
                # 只展示状态码，不把响应正文/请求头中的凭据写入配置或界面。
                failure = f"角色改写接口返回 HTTP {exc.response.status_code}，请检查 LLM 配置后重新保存"
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
            except httpx.RequestError:
                failure = "角色改写接口连接失败或超时，请检查服务后重新保存"
            except (ValueError, KeyError, IndexError, TypeError):
                failure = "角色改写接口返回格式无效，请检查模型兼容性后重新保存"
            if attempt == 0:
                await asyncio.sleep(0.3)
    raise RoleRewriteError(failure)


def write_role_file(path: Path, name: str, instructions: str) -> None:
    """同目录临时文件原子替换，避免读取到写了一半的角色定义。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.role-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            file.write(f'# {name}\n\n{instructions}\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
