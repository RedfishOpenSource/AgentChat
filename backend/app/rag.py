"""RAG 知识库：本地文件夹 → 分块 → 中文 bigram 倒排索引检索（零外部依赖）。

- 知识源：用户通过前端目录选择器指定的本地文件夹（.md / .txt，UTF-8 或 GBK）
- 更新：生成前按目录签名（文件数+最大mtime）检测变化，冷却期内不重复扫描；
  另提供 scan(force=True) 手动重扫
- 检索：字符 bigram TF-IDF 打分（对中文友好，无需分词器），返回 top-k 分块
- 注入：由 LLMReplyGenerator 拼进用户提示词「[知识库参考资料]」段落
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".md", ".txt"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             ".idea", ".vscode", "__MACOSX", "$RECYCLE.BIN", "System Volume Information"}
MAX_CHUNK_CHARS = 500  # 单块上限（段落合并后超出则再切）
SCAN_COOLDOWN = 5.0  # 两次自动重扫的最小间隔（秒）
MIN_MATCH_BIGRAMS = 2  # 至少命中 N 个不同 bigram 才算相关，避免注入噪声
TOP_K = 4


def _bigrams(text: str) -> Counter:
    """字符 bigram（仅保留中文/字母/数字），中文无需分词即可做关键词匹配。"""
    chars = [c.lower() for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff"]
    return Counter(a + b for a, b in zip(chars, chars[1:]))


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _chunk_text(text: str) -> list[str]:
    """按空行分段，相邻段合并到 MAX_CHUNK_CHARS；超长单段硬切。"""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        while len(p) > MAX_CHUNK_CHARS:  # 超长段先硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p[:MAX_CHUNK_CHARS])
            p = p[MAX_CHUNK_CHARS:]
        if not p:
            continue
        if buf and len(buf) + len(p) + 2 > MAX_CHUNK_CHARS:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


@dataclass
class Chunk:
    file: str  # 相对知识库根目录的路径
    text: str


class KnowledgeBase:
    """内存知识库索引（一个实例对应一个知识目录，可重配/重扫/重置）。"""

    def __init__(self) -> None:
        self.root: Path | None = None
        self._chunks: list[Chunk] = []
        self._postings: dict[str, dict[int, int]] = {}  # bigram -> {chunk_id: tf}
        self._signature: tuple = ()
        self._last_scan: float = 0.0
        self._last_scan_at: float = 0.0  # monotonic，冷却用
        self._last_error: str = ""
        self._scanning = False

    # ---------- 配置与状态 ----------
    def configure(self, path: str) -> dict:
        """设置知识目录并立即扫描。目录不存在抛 ValueError。"""
        p = Path(path).expanduser()
        if not path.strip():
            self.reset()
            return self.stats()
        if not p.is_dir():
            raise ValueError(f"知识库目录不存在或不是文件夹: {path}")
        self.root = p
        self._scan()
        return self.stats()

    def reset(self) -> None:
        """清空知识库（未配置/停用状态）。"""
        self.root = None
        self._chunks = []
        self._postings = {}
        self._signature = ()
        self._last_scan = 0.0
        self._last_error = ""

    def stats(self) -> dict:
        return {
            "enabled": self.root is not None,
            "path": str(self.root) if self.root else "",
            "files": len({c.file for c in self._chunks}),
            "chunks": len(self._chunks),
            "last_scan": int(self._last_scan * 1000) if self._last_scan else 0,
            "last_error": self._last_error,
        }

    # ---------- 扫描 ----------
    def scan(self, force: bool = False) -> dict:
        """手动/强制重扫。force=True 忽略冷却与签名判断。"""
        if self.root is None:
            return self.stats()
        if force:
            self._scan()
        else:
            self.ensure_fresh()
        return self.stats()

    def ensure_fresh(self) -> None:
        """目录签名变化时自动重扫（受冷却时间限制，便宜且幂等）。"""
        if self.root is None or self._scanning:
            return
        if time.monotonic() - self._last_scan_at < SCAN_COOLDOWN:
            return
        try:
            sig = self._signature_of(self.root)
        except OSError:
            return
        if sig != self._signature:
            self._scan()

    def _signature_of(self, root: Path) -> tuple:
        count = 0
        max_mtime = 0.0
        for f in self._iter_files(root):
            count += 1
            try:
                max_mtime = max(max_mtime, f.stat().st_mtime)
            except OSError:
                pass
        return (count, max_mtime)

    def _iter_files(self, root: Path):
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTS:
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in f.relative_to(root).parts[:-1]):
                continue
            yield f

    def _scan(self) -> None:
        if self.root is None or self._scanning:
            return
        self._scanning = True
        try:
            chunks: list[Chunk] = []
            for f in sorted(self._iter_files(self.root)):
                rel = f.relative_to(self.root).as_posix()
                try:
                    text = _read_text(f)
                except OSError as exc:
                    logger.warning("[rag] 读取失败 %s: %s", rel, exc)
                    continue
                for piece in _chunk_text(text):
                    chunks.append(Chunk(file=rel, text=piece))
            self._chunks = chunks
            self._build_index()
            self._signature = self._signature_of(self.root)
            self._last_scan = time.time()
            self._last_scan_at = time.monotonic()
            self._last_error = ""
            logger.info("[rag] 知识库已扫描: %s（%d 个文件 / %d 块）",
                        self.root, len({c.file for c in chunks}), len(chunks))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)[:200]
            logger.exception("[rag] 知识库扫描失败: %s", exc)
        finally:
            self._scanning = False

    def _build_index(self) -> None:
        postings: dict[str, dict[int, int]] = {}
        for cid, chunk in enumerate(self._chunks):
            for bg, tf in _bigrams(chunk.text).items():
                postings.setdefault(bg, {})[cid] = tf
        self._postings = postings

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = TOP_K) -> list[tuple[float, Chunk]]:
        """按 bigram TF-IDF 相关度返回 top-k 分块；无足够命中返回空。"""
        if self.root is None or not self._chunks:
            return []
        self.ensure_fresh()
        q = _bigrams(query)
        if not q:
            return []
        n = len(self._chunks)
        scores: dict[int, float] = {}
        hits: dict[int, int] = {}
        for bg, qtf in q.items():
            posting = self._postings.get(bg)
            if not posting:
                continue
            idf = math.log(1.0 + n / (1.0 + len(posting)))
            for cid, tf in posting.items():
                scores[cid] = scores.get(cid, 0.0) + idf * (1.0 + math.log(qtf)) * (1.0 + math.log(tf))
                hits[cid] = hits.get(cid, 0) + 1
        ranked = [
            (score / max(1, len(self._chunks[cid].text)) * 1000.0, self._chunks[cid])
            for cid, score in scores.items()
            if hits[cid] >= MIN_MATCH_BIGRAMS
        ]
        ranked.sort(key=lambda x: -x[0])
        return ranked[:top_k]
