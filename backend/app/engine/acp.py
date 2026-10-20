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
import shutil
import subprocess
import uuid
from pathlib import Path

from ..config import ACPSettings
from ..models import ChatMessage
from .base import GenerationResult, ReplyGenerator
from .output import clean_agent_reply, is_pi_notification

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


def _progress(callback, phase: str, **extra) -> None:
    if callback is not None:
        callback(phase, **extra)


class ACPAgentError(RuntimeError):
    """Agent 调用失败（启动失败 / 超时 / 协议错误）。调用方会降级为错误占位草稿。"""


def _split_args(raw: str) -> list[str]:
    """按空白拆分参数；保留 Windows 路径反斜杠，去掉成对引号。"""
    return [t.strip('"') for t in shlex.split(raw or "", posix=False) if t.strip('"')]


def _command_argv(command: str, args: list[str]) -> list[str]:
    """Windows npm 全局命令是 .cmd 包装器，需通过命令解释器启动。"""
    resolved = shutil.which(command) or command
    argv = [resolved, *args]
    if os.name == "nt" and Path(resolved).suffix.lower() in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return argv


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    """Windows .cmd 包装器会启动子进程；结束整棵进程树以关闭管道。"""
    if proc.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


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
        self._notify_progress = None
        self._notify_session: str | None = None
        self._sessions: dict[tuple[str, str], str] = {}
        self._pi_resets: dict[tuple[str, str], str] = {}
        self._next_id = 0

    # ---------- 公共 ----------
    def reset_session(self, platform: str, chat_id: str) -> None:
        """清空指定联系人的 Agent 会话：下条消息将开启全新会话。"""
        key = (platform, chat_id)
        self._sessions.pop(key, None)
        self._pi_resets[key] = uuid.uuid4().hex

    def set_session_token(self, platform: str, chat_id: str, token: str) -> None:
        """服务重启/引擎重建后恢复清空分界，避免续接清空前的会话。"""
        key = (platform, chat_id)
        if self._pi_resets.get(key) != token:
            self._sessions.pop(key, None)
            self._pi_resets[key] = token

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
        prompt = f"{sender}：{content}" if sender else content
        context = []
        if self.cfg.instructions.strip():
            context.append("Agent 指令：\n" + self.cfg.instructions.strip())
        if self.cfg.skills:
            context.append("Skills（已安装在工作空间 .agents/skills/ 目录）：\n" + "\n".join(f"- {skill}" for skill in self.cfg.skills))
        if self.cfg.capabilities:
            context.append("可用能力：" + "、".join(self.cfg.capabilities))
        if self.cfg.plan_before_execute:
            context.append("执行前先简要制定计划，再按计划完成任务。")
        if self.cfg.knowledge_dir.strip():
            context.append(self._knowledge_context())
        context.append(
            "回复用于即时通信。对外答复只包含用户需要的结论和必要说明，默认简洁中文；"
            "不要在答复中附带 Context/Skills/Extensions 列表、工作空间路径、启动日志、"
            "内部推理或‘我先查询/接下来执行’等过程旁白。过程通过进度事件呈现。"
        )
        return "\n\n".join([*context, prompt])

    def _knowledge_context(self) -> str:
        root = Path(self.cfg.knowledge_dir).expanduser()
        if not root.is_dir():
            return "Agent 知识目录当前不可用。"
        sections = []
        total = 0
        for path in sorted((*root.rglob("*.md"), *root.rglob("*.txt"))):
            try:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                piece = f"### {path.name}\n{text[:12000]}"
                if total + len(piece) > 40000:
                    break
                sections.append(piece)
                total += len(piece)
            except (OSError, UnicodeError):
                continue
        return "Agent 知识库（只读参考；可按指令更新指定文件）：\n" + "\n\n".join(sections) if sections else "Agent 知识库目录为空。"

    async def generate(
        self, message: ChatMessage, history, kb=None, *, on_progress=None
    ) -> GenerationResult:  # noqa: ANN001, ANN001
        if self.cfg.mode == "pi":
            return await self._generate_pi(message, on_progress)
        return await self._generate_acp(message, on_progress)

    # ---------- ACP 模式 ----------
    async def _acp_start(self) -> None:
        command = self.cfg.command.strip()
        argv = _command_argv(command, _split_args(self.cfg.args))
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
                f"找不到 Agent 命令（{command}），请确认服务运行账户已安装该命令并加入 PATH"
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
                    params = msg.get("params") or {}
                    if params.get("sessionId") != self._notify_session:
                        continue
                    upd = params.get("update") or {}
                    kind = upd.get("sessionUpdate")
                    if kind == "agent_message_chunk" and self._notify_chunks is not None:
                        content = upd.get("content") or {}
                        if content.get("type") == "text":
                            text = str(content.get("text") or "")
                            if (upd.get("_meta") or {}).get("piAcp", {}).get("notify") or is_pi_notification(text):
                                _progress(self._notify_progress, "thinking", detail="Agent 返回运行状态，正在继续处理")
                                continue
                            self._notify_chunks.append(text)
                            # 启动横幅可能与第一条正文一起返回；预览和最终草稿统一过滤。
                            preview = clean_agent_reply("".join(self._notify_chunks))
                            _progress(self._notify_progress, "writing", detail="正在生成回复", preview=preview[-2400:])
                    elif kind == "agent_thought_chunk":
                        # 只报告推理状态；不转发模型的内部推理正文。
                        _progress(self._notify_progress, "thinking", detail="AI 正在分析问题")
                    elif kind in ("tool_call", "tool_call_update"):
                        status = upd.get("status") or "in_progress"
                        title = str(upd.get("title") or "工具")[:120]
                        label = {"completed": "执行完成", "failed": "执行失败", "pending": "等待执行"}.get(status, "正在执行")
                        _progress(self._notify_progress, "tool", detail=f"{label}：{title}")
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
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ACPAgentError("Agent 连接已断开，请检查启动命令或重新生成"))

    async def _acp_request(self, method: str, params: dict, timeout: float, collect: list[str] | None = None, on_progress=None) -> dict:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        if collect is not None:
            self._notify_chunks = collect
            self._notify_progress = on_progress
            self._notify_session = params.get("sessionId")
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            # 包括握手和外层单次超时，防止迟到输出污染下一次生成。
            await self._acp_kill()
            raise
        finally:
            self._pending.pop(rid, None)
            if collect is not None and self._notify_chunks is collect:
                self._notify_chunks = None
                self._notify_progress = None
                self._notify_session = None

    async def _acp_kill(self) -> None:
        if self._read_task is not None:
            task, self._read_task = self._read_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._proc is not None:
            await _stop_process(self._proc)
        self._proc = None

    async def _generate_acp(self, message: ChatMessage, on_progress=None) -> GenerationResult:
        if not self.cfg.command.strip():
            raise ACPAgentError("ACP Agent 启动命令未配置（acp_command）")
        _progress(on_progress, "queued", detail="等待 Agent 空闲")
        async with self._req_lock:
            if self._proc is None or self._proc.returncode is not None:
                self._sessions.clear()
                _progress(on_progress, "starting", detail="正在启动 Agent 并建立连接")
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
                _progress(on_progress, "session", detail="正在创建联系人会话")
                res = await self._acp_request(
                    "session/new",
                    {"cwd": self._cwd(), "mcpServers": self.cfg.mcp_servers},
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
            _progress(on_progress, "thinking", detail="问题已提交，等待 Agent 响应")
            try:
                res = await self._acp_request(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": self._prompt_text(message)}]},
                    timeout=self.cfg.timeout,
                    collect=chunks, on_progress=on_progress,
                )
            except asyncio.TimeoutError:
                await self._acp_kill()
                raise ACPAgentError(f"Agent 响应超时（>{self.cfg.timeout}s），已终止进程") from None
            if "error" in res:
                raise ACPAgentError(f"Agent prompt 失败: {res['error']}")
            content = clean_agent_reply("".join(chunks))
            meta = {"engine": "acp", "mode": "acp", "agent": self.cfg.command.strip(),
                    "session": sid, "stop_reason": (res.get("result") or {}).get("stopReason")}
            logger.info("[acp] Agent 回复 %s/%s: %s", key[0], key[1], content[:60])
            return GenerationResult(content=content or "（Agent 未返回文本内容）", meta=meta)

    # ---------- pi 模式 ----------
    def _pi_session_id(self, key: tuple[str, str]) -> str:
        gen = self._pi_resets.get(key, 0)
        raw = f"{key[0]}:{key[1]}" if not gen else f"{key[0]}:{key[1]}:r{gen}"
        return "chatagent-" + hashlib.sha1(raw.encode()).hexdigest()[:16]

    async def _pi_prompt(self, proc: asyncio.subprocess.Process, text: str, timeout: float, on_progress=None) -> str:
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
                elif ev.get("type") == "message_update":
                    update = ev.get("assistantMessageEvent") or {}
                    if update.get("type") == "text_delta":
                        _progress(on_progress, "writing", detail="正在生成回复", preview_delta=str(update.get("delta") or ""))
                    elif str(update.get("type", "")).startswith("thinking"):
                        _progress(on_progress, "thinking", detail="AI 正在分析问题")
                elif ev.get("type") in ("tool_execution_start", "tool_execution_end"):
                    label = "正在执行" if ev["type"] == "tool_execution_start" else "执行完成"
                    if ev.get("isError"):
                        label = "执行失败"
                    _progress(on_progress, "tool", detail=f"{label}：{str(ev.get('toolName') or '工具')[:120]}")

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

    async def _generate_pi(self, message: ChatMessage, on_progress=None) -> GenerationResult:
        if not self.cfg.command.strip():
            raise ACPAgentError("pi 启动命令未配置（acp_command，如 pi）")
        key = (message.platform.value, message.chat_id)
        sid = self._pi_session_id(key)
        command = self.cfg.command.strip()
        args = _split_args(self.cfg.args) + ["--mode", "rpc", "--session-id", sid]
        argv = _command_argv(command, args)
        _progress(on_progress, "queued", detail="等待 Agent 空闲")
        async with self._pi_lock:
            _progress(on_progress, "starting", detail="正在启动 Agent")
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
                    f"找不到 pi 命令（{command}），请确认服务运行账户已安装该命令并加入 PATH"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise ACPAgentError(f"启动 pi 失败: {exc}") from exc
            try:
                _progress(on_progress, "thinking", detail="问题已提交，等待 Agent 响应")
                text = await self._pi_prompt(proc, self._prompt_text(message), self.cfg.timeout, on_progress)
            finally:
                await _stop_process(proc)
            logger.info("[acp] pi 回复 %s/%s (session=%s): %s", key[0], key[1], sid, text[:60])
            meta = {"engine": "acp", "mode": "pi", "agent": self.cfg.command.strip(), "session": sid}
            return GenerationResult(content=clean_agent_reply(text) or "（pi 未返回文本内容）", meta=meta)

    # ---------- 退出 ----------
    async def aclose(self) -> None:
        await self._acp_kill()
