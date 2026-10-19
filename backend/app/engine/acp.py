"""可插拔 Agent 接入引擎（ACP 协议 / pi RPC 适配）。

两种模式（acp.mode）：
- acp：标准 Agent Client Protocol（JSON-RPC 2.0 over stdio，按行分隔 JSON）。
  单个 Agent 进程承载多个会话：initialize → session/new（每个联系人一个 sessionId）
  → session/prompt（回复通过 session/update 的 agent_message_chunk 增量返回）。
  适用于 claude-code-acp / codex-acp / hermes 等原生 ACP Agent 与任意自定义 Agent。
- pi：pi coding agent 的 RPC 模式（pi --mode rpc，自定义 JSONL 协议）。
  每个联系人一个独立子进程，用 --session-id 绑定会话：多次问答同一联系人
  自动复用同一 session（会话文件落盘，服务重启后上下文不丢）。

同一联系人保持一个 Agent 会话：多轮问答上下文由 Agent 自己维护。
安全提示：pi / claude code / codex 具备工具执行（含 shell）能力，
请把工作空间（acp.cwd）指向专用目录，避免聊天消息驱动敏感项目。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shlex
from pathlib import Path

from ..config import ACPSettings
from ..models import ChatMessage
from .base import GenerationResult, ReplyGenerator

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


class ACPAgentError(RuntimeError):
    """Agent 调用失败（启动失败 / 超时 / 协议错误）。调用方会降级为错误占位草稿。"""


def _split_args(raw: str) -> list[str]:
    """按空白拆分参数；保留 Windows 路径反斜杠，去掉成对引号。"""
    return [t.strip('"') for t in shlex.split(raw or "", posix=False) if t.strip('"')]


class ACPAgentGenerator(ReplyGenerator):
    """把外部 Agent（ACP 协议 / pi RPC）作为回复生成引擎，按联系人保持会话。"""

    name = "acp"

    def __init__(self, cfg: ACPSettings) -> None:
        self.cfg = cfg
        self._req_lock = asyncio.Lock()
        self._pi_lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._notify_chunks: list[str] | None = None
        self._sessions: dict[tuple[str, str], str] = {}
        self._next_id = 0

    # ---------- 公共 ----------
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        name, key = self.cfg.env_name.strip(), self.cfg.env_key.strip()
        if name and key:
            env[name] = key
        return env

    def _cwd(self) -> str:
        c = self.cfg.cwd.strip()
        return c if c and Path(c).is_dir() else os.getcwd()

    def _prompt_text(self, message: ChatMessage) -> str:
        content = message.content.strip()
        sender = (message.sender or "").strip()
        return f"{sender}：{content}" if sender else content

    async def generate(
        self, message: ChatMessage, history, kb=None
    ) -> GenerationResult:  # noqa: ANN001, ANN001
        if self.cfg.mode == "pi":
            return await self._generate_pi(message)
        return await self._generate_acp(message)

    # ---------- ACP 模式 ----------
    async def _acp_start(self) -> None:
        argv = [self.cfg.command.strip()] + _split_args(self.cfg.args)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self._cwd(),
                env=self._env(),
                limit=1 << 20,
            )
        except FileNotFoundError as exc:
            raise ACPAgentError(
                f"找不到 Agent 命令（{self.cfg.command.strip()}），请确认已安装并加入 PATH"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ACPAgentError(f"启动 Agent 失败: {exc}") from exc
        self._read_task = asyncio.create_task(self._acp_read_loop())

    async def _acp_read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        proc = self._proc
        try:
            while proc.returncode is None:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.pop(int(msg["id"]), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif msg.get("method") == "session/update":
                    upd = (msg.get("params") or {}).get("update") or {}
                    if upd.get("sessionUpdate") == "agent_message_chunk" and self._notify_chunks is not None:
                        content = upd.get("content") or {}
                        if content.get("type") == "text":
                            self._notify_chunks.append(str(content.get("text") or ""))
                elif "id" in msg and msg.get("method"):
                    # Agent 反向请求（权限/文件系统等）：拒绝，避免 Agent 挂起等待
                    with contextlib.suppress(Exception):
                        proc.stdin.write(
                            (json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                         "error": {"code": -32601,
                                                   "message": "ChatAgent client does not support this request"}}) + "\n").encode()
                        )
                        await proc.stdin.drain()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[acp] Agent 读流出错: %s", exc)

    async def _acp_request(self, method: str, params: dict, timeout: float, collect: list[str] | None = None) -> dict:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        if collect is not None:
            self._notify_chunks = collect
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)
            if collect is not None and self._notify_chunks is collect:
                self._notify_chunks = None

    async def _acp_kill(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            self._read_task = None
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, asyncio.TimeoutError):
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
        self._proc = None

    async def _generate_acp(self, message: ChatMessage) -> GenerationResult:
        if not self.cfg.command.strip():
            raise ACPAgentError("ACP Agent 启动命令未配置（acp_command）")
        async with self._req_lock:
            if self._proc is None or self._proc.returncode is not None:
                self._sessions.clear()
                await self._acp_start()
                init = await self._acp_request(
                    "initialize",
                    {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
                    timeout=min(60.0, self.cfg.timeout),
                )
                if "error" in init:
                    raise ACPAgentError(f"Agent initialize 失败: {init['error']}")
            key = (message.platform.value, message.chat_id)
            sid = self._sessions.get(key)
            if not sid:
                res = await self._acp_request(
                    "session/new",
                    {"cwd": self._cwd(), "mcpServers": []},
                    timeout=min(60.0, self.cfg.timeout),
                )
                if "error" in res:
                    raise ACPAgentError(f"Agent session/new 失败: {res['error']}")
                sid = str((res.get("result") or {}).get("sessionId") or "")
                if not sid:
                    raise ACPAgentError("Agent 未返回 sessionId")
                self._sessions[key] = sid
                logger.info("[acp] 新建 Agent 会话 %s -> %s", key, sid)
            chunks: list[str] = []
            try:
                res = await self._acp_request(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": self._prompt_text(message)}]},
                    timeout=self.cfg.timeout,
                    collect=chunks,
                )
            except asyncio.TimeoutError:
                await self._acp_kill()
                raise ACPAgentError(f"Agent 响应超时（>{self.cfg.timeout}s），已终止进程") from None
            if "error" in res:
                raise ACPAgentError(f"Agent prompt 失败: {res['error']}")
            content = "".join(chunks).strip()
            meta = {"engine": "acp", "mode": "acp", "agent": self.cfg.command.strip(),
                    "session": sid, "stop_reason": (res.get("result") or {}).get("stopReason")}
            logger.info("[acp] Agent 回复 %s/%s: %s", key[0], key[1], content[:60])
            return GenerationResult(content=content or "（Agent 未返回文本内容）", meta=meta)

    # ---------- pi 模式 ----------
    def _pi_session_id(self, key: tuple[str, str]) -> str:
        return "chatagent-" + hashlib.sha1(f"{key[0]}:{key[1]}".encode()).hexdigest()[:16]

    async def _pi_prompt(self, proc: asyncio.subprocess.Process, text: str, timeout: float) -> str:
        loop = asyncio.get_running_loop()
        settled = asyncio.Event()
        failed: list[str] = []
        responses: dict[str, asyncio.Future] = {}

        async def reader() -> None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    if not settled.is_set():
                        failed.append("pi 进程意外退出")
                        settled.set()
                    return
                try:
                    ev = json.loads(line.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "response" and ev.get("id"):
                    rid = str(ev["id"])
                    fut = responses.get(rid)
                    if fut and not fut.done():
                        fut.set_result(ev)
                    if rid == "ca-prompt" and ev.get("success") is False:
                        failed.append(f"pi prompt 失败: {ev.get('error') or '未知原因'}")
                        settled.set()
                elif ev.get("type") == "agent_settled":
                    settled.set()

        reader_task = asyncio.create_task(reader())
        try:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps({"id": "ca-prompt", "type": "prompt", "message": text}) + "\n").encode())
            await proc.stdin.drain()
            try:
                await asyncio.wait_for(settled.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise ACPAgentError(f"pi 响应超时（>{timeout}s）") from None
            if failed:
                raise ACPAgentError(failed[0])
            fut: asyncio.Future = loop.create_future()
            responses["ca-text"] = fut
            proc.stdin.write((json.dumps({"id": "ca-text", "type": "get_last_assistant_text"}) + "\n").encode())
            await proc.stdin.drain()
            resp = await asyncio.wait_for(fut, timeout=min(30.0, timeout))
            return str((resp.get("data") or {}).get("text") or "")
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task

    async def _generate_pi(self, message: ChatMessage) -> GenerationResult:
        if not self.cfg.command.strip():
            raise ACPAgentError("pi 启动命令未配置（acp_command，如 pi）")
        key = (message.platform.value, message.chat_id)
        sid = self._pi_session_id(key)
        argv = [self.cfg.command.strip()] + _split_args(self.cfg.args) + [
            "--mode", "rpc", "--session-id", sid,
        ]
        async with self._pi_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=self._cwd(),
                    env=self._env(),
                    limit=1 << 20,
                )
            except FileNotFoundError as exc:
                raise ACPAgentError(
                    f"找不到 pi 命令（{self.cfg.command.strip()}），请确认已安装并加入 PATH"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise ACPAgentError(f"启动 pi 失败: {exc}") from exc
            try:
                text = await self._pi_prompt(proc, self._prompt_text(message), self.cfg.timeout)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            logger.info("[acp] pi 回复 %s/%s (session=%s): %s", key[0], key[1], sid, text[:60])
            meta = {"engine": "acp", "mode": "pi", "agent": self.cfg.command.strip(), "session": sid}
            return GenerationResult(content=text.strip() or "（pi 未返回文本内容）", meta=meta)

    # ---------- 退出 ----------
    async def aclose(self) -> None:
        await self._acp_kill()
