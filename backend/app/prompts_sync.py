"""Agent 记忆与自进化：从 GitHub 拉取提示词案例到各 Agent 工作空间。

- 每次创建/更新 Agent、以及服务启动时，都会拉取并覆盖保存；
- 仓库目录：AI-Redfish/Agent@main 的 04.Agent开发/04.记忆与自进化/02.提示词案例；
- 该目录内容按相对路径映射到各 Agent 工作空间根目录覆盖保存
  （如 .agents/memory/prompts/*.md、AGENTS.md 等）；
- 拉取方式：优先 GitHub Contents API（可用 GITHUB_TOKEN 提高限额），
  失败自动回退 git sparse-checkout（无速率限制）；
- 网络失败仅记录日志，不阻断 Agent 创建与服务启动。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/AI-Redfish/Agent.git"
# 仓库内「02.提示词案例」目录（main 分支；URL 编码 = 提示词案例）
PROMPTS_DIR_REPO = "04.Agent开发/04.记忆与自进化/02.提示词案例"
PROMPTS_DIR_URL_ENC = "02.%E6%8F%90%E7%A4%BA%E8%AF%8D%E6%A1%88%E4%BE%8B"
PROMPTS_API_URL = (
    "https://api.github.com/repos/AI-Redfish/Agent/contents/"
    "04.Agent%E5%BC%80%E5%8F%91/04.%E8%AE%B0%E5%BF%86%E4%B8%8E%E8%87%AA%E8%BF%9B%E5%8C%96/"
    + PROMPTS_DIR_URL_ENC
)

_HEADERS = {"User-Agent": "ChatAgent", "Accept": "application/vnd.github+json"}


def _token_headers() -> dict[str, str]:
    import os

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return {**_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {})}


async def _fetch_via_api(client: httpx.AsyncClient, dir_url: str = PROMPTS_API_URL, prefix: str = PROMPTS_DIR_REPO + "/") -> list[tuple[str, bytes]]:
    resp = await client.get(dir_url, params={"ref": "main"}, headers=_token_headers())
    resp.raise_for_status()
    entries = resp.json()
    if not isinstance(entries, list):
        raise ValueError("GitHub 目录接口返回异常")
    out: list[tuple[str, bytes]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        etype, path = str(entry.get("type") or ""), str(entry.get("path") or "")
        if etype == "dir":
            out += await _fetch_via_api(client, f"{PROMPTS_API_URL}/{path.split('/')[-1]}", path + "/")
            continue
        if etype != "file" or not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        url = str(entry.get("download_url") or "").strip()
        if not rel or not url:
            continue
        file_resp = await client.get(url)
        file_resp.raise_for_status()
        out.append((rel, file_resp.content))
    return out


def _fetch_via_git(attempts: int = 3) -> list[tuple[str, bytes]]:
    """git sparse-checkout 拉取仓库中该目录（无 API 速率限制；可指定重试次数）。"""
    import time

    tmp = Path(tempfile.mkdtemp(prefix="chatagent-prompts-"))
    try:
        def run(*cmd: str) -> None:
            last = ""
            for attempt in range(max(1, attempts)):
                r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                   timeout=300, check=False)
                if not r.returncode:
                    return
                last = (r.stderr or r.stdout).strip()[-300:]
                logger.info("[prompts-sync] git 重试 %d/%d: %s", attempt + 1, attempts, last[-120:])
                time.sleep(2 * (attempt + 1))
            raise RuntimeError(last)

        run("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
            REPO_URL, str(tmp))
        run("git", "sparse-checkout", "set", PROMPTS_DIR_REPO)
        src = tmp / PROMPTS_DIR_REPO
        if not src.is_dir():
            raise RuntimeError(f"仓库中不存在目录: {PROMPTS_DIR_REPO}")
        out: list[tuple[str, bytes]] = []
        for p in sorted(src.rglob("*")):
            if p.is_file():
                out.append((p.relative_to(src).as_posix(), p.read_bytes()))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def sync_workspace_prompts(
    workspace: str,
    transport: httpx.AsyncBaseTransport | None = None,
    prefer_git: bool = True,
    attempts: int = 3,
) -> dict[str, Any]:
    """拉取提示词案例并按相对路径覆盖写入工作空间；返回 {synced, files, via}。"""
    ws = Path(workspace).expanduser()
    if not ws.is_dir():
        raise ValueError(f"工作空间目录不存在: {workspace}")

    files: list[tuple[str, bytes]] = []
    via = "git"
    if prefer_git:
        try:
            files = await asyncio.to_thread(_fetch_via_git, attempts)
        except Exception as git_exc:  # noqa: BLE001 —— git 不可用/失败时回退 API
            logger.info("[prompts-sync] git 拉取失败(%s)，改用 Contents API", git_exc)
    if not files:
        async with httpx.AsyncClient(timeout=30, headers=_HEADERS, transport=transport) as client:
            files = await _fetch_via_api(client)
        via = "api"

    written: list[str] = []
    for rel, content in files:
        rel_norm = str(PurePosixPath(rel))
        dest = (ws / rel_norm).resolve()
        if not dest.is_relative_to(ws.resolve()):
            continue
        # Agent 的角色由 AI 改写维护；远程模板不能覆盖已有角色（含重启同步）。
        if dest == (ws / ".agents" / "memory" / "role.md").resolve():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)  # 直接覆盖保存
        written.append(rel_norm)

    logger.info("[prompts-sync] 已同步 %d 个文件到 %s (via %s)", len(written), ws, via)
    return {"synced": len(written), "files": written, "target": str(ws), "via": via}


def prompts_recent(workspace: str, minutes: int = 10) -> bool:
    """10 分钟内已成功同步过则跳过，避免重复保存时的无谓等待。"""
    import time

    marker = Path(workspace).expanduser() / ".agents" / "memory" / "prompts" / ".last_sync"
    try:
        return (time.time() - marker.stat().st_mtime) < minutes * 60
    except OSError:
        return False


def mark_synced(workspace: str) -> None:
    import time

    marker = Path(workspace).expanduser() / ".agents" / "memory" / "prompts" / ".last_sync"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:  # noqa: BLE001
        pass


async def sync_all_agent_profiles(agent) -> None:
    """服务启动时：为每个 Agent 配置的工作空间做一次同步（相同工作空间只同步一次）。"""
    workspaces: list[str] = []
    for profile in agent.agent_profiles.values():
        ws = str(profile.get("workspace") or "").strip()
        if ws and ws not in workspaces:
            workspaces.append(ws)
    for ws in workspaces:
        try:
            await sync_workspace_prompts(ws)
        except Exception as exc:  # noqa: BLE001 —— 网络失败不阻断启动
            logger.warning("[prompts-sync] 同步失败(%s): %s", ws, exc)
