"""RAG 知识库：本地文件夹 → 分块 → 检索（词面 bigram 零依赖 + 可选语义向量混合）。

- 知识源：用户通过前端目录选择器指定的本地文件夹（.md / .txt，UTF-8 或 GBK）
- 更新：生成前按目录签名（文件数+最大mtime）检测变化，冷却期内不重复扫描；
  另提供 scan(force=True) 手动重扫
- 检索：
  1) 字符 bigram TF-IDF 词面打分（零外部依赖，始终可用）；
  2) 可选语义向量检索：任意 OpenAI 兼容 /embeddings 接口（embedding.enabled=true 时启用），
     分块向量带磁盘缓存（重启不重复计费），词面+向量两路排名按 RRF 融合；
     Embedding 调用失败自动降级回纯词面，并进入冷却期避免雪崩
- 注入：由 LLMReplyGenerator 拼进用户提示词「[知识库参考资料]」段落
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import EmbeddingSettings

logger = logging.getLogger(__name__)

SUPPORTED_EXTS = {".md", ".txt"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             ".idea", ".vscode", "__MACOSX", "$RECYCLE.BIN", "System Volume Information"}
MAX_CHUNK_CHARS = 500  # 单块上限（段落合并后超出则再切）
SCAN_COOLDOWN = 5.0  # 两次自动重扫的最小间隔（秒）
MIN_MATCH_BIGRAMS = 2  # 至少命中 N 个不同 bigram 才算相关，避免注入噪声
TOP_K = 4
RRF_K = 60.0  # Reciprocal Rank Fusion 常数（两路排名融合）
EMBED_COOLDOWN = 60.0  # Embedding 调用失败后的降级冷却（秒），期间只用词面检索


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；长度不一致或零向量返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class _ResolvedEmbedding:
    """解析后的 Embedding 服务配置（base_url/api_key 已回填 llm 兼省值）。"""

    model: str
    base_url: str
    api_key: str
    batch: int = 16


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
        # ---- 语义向量检索（可选）----
        self._emb: _ResolvedEmbedding | None = None
        self._client: httpx.AsyncClient | None = None
        self._vectors: dict[str, list[float]] = {}  # sha256(model+text) -> 向量（含磁盘缓存）
        self._cache_path: Path | None = None
        self._emb_error: str = ""
        self._emb_cooldown_until: float = 0.0
        self._emb_busy = False  # 并发去重：批量嵌入进行中时其他检索先复用已有向量

    # ---------- 语义检索配置 ----------
    def set_embedding(
        self,
        cfg: EmbeddingSettings | None,
        llm_base_url: str = "",
        llm_api_key: str = "",
        cache_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> dict:
        """配置语义检索（可选）。enabled=False 时关闭，退回纯词面检索。

        enabled=True 但 model 未配置时抛 ValueError（热更新会得到明确的 400，
        而不是表面上开关已开、实际未生效）。
        """
        if cfg is None or not cfg.enabled:
            self._emb = None
            return self.stats()
        model = str(cfg.model or "").strip()
        if not model:
            raise ValueError("embedding.model 未配置，无法启用语义检索（config.yaml 或 CHATAGENT_EMBEDDING_MODEL）")
        base_url = (str(cfg.base_url or "").strip() or str(llm_base_url or "").strip()).rstrip("/")
        if not base_url:
            raise ValueError("embedding.base_url 为空且 llm.base_url 也为空，无法调用 Embedding 接口")
        self._emb = _ResolvedEmbedding(
            model=model,
            base_url=base_url,
            api_key=str(cfg.api_key or "").strip() or str(llm_api_key or "").strip(),
            batch=max(1, int(cfg.batch_size or 16)),
        )
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=max(3.0, float(cfg.timeout or 15.0)), transport=transport
            )
        self._cache_path = Path(cache_path) if cache_path else None
        self._load_vector_cache()
        logger.info(
            "[rag] 语义检索已启用: model=%s base=%s 缓存向量 %d 条",
            self._emb.model, base_url, len(self._vectors),
        )
        return self.stats()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
            "embedding": {
                "enabled": self._emb is not None,
                "model": self._emb.model if self._emb else "",
                "cached": len(self._vectors),
                "last_error": self._emb_error,
            },
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
        """同步词面检索（bigram TF-IDF）：零依赖、始终可用，未启用语义检索时的唯一路径。"""
        if self.root is None or not self._chunks:
            return []
        self.ensure_fresh()
        return self._search_lexical(query, top_k)

    async def search_hybrid(self, query: str, top_k: int = TOP_K) -> list[tuple[float, Chunk]]:
        """混合检索：词面 + 语义向量按 RRF 融合；未启用/失败自动退回纯词面。"""
        if self.root is None or not self._chunks:
            return []
        self.ensure_fresh()
        lexical = self._search_lexical(query, top_k)
        if self._emb is None or self._client is None:
            return lexical
        if time.monotonic() < self._emb_cooldown_until:
            return lexical  # 最近失败在冷却期内，暂时只用词面
        try:
            qvec = (await self._embed_batch([query]))[0]
            if self._emb_busy:
                # 另一个检索正在批量嵌入同一批分块：先用现有向量（缺失的下轮补上）
                vec = self._search_vector(qvec, top_k)
            else:
                self._emb_busy = True
                try:
                    await self._ensure_vectors()
                finally:
                    self._emb_busy = False
                vec = self._search_vector(qvec, top_k)
        except Exception as exc:  # noqa: BLE001 —— 语义检索失败绝不影响回复生成
            self._emb_error = str(exc)[:200]
            self._emb_cooldown_until = time.monotonic() + EMBED_COOLDOWN
            logger.warning("[rag] Embedding 检索失败，%ss 内降级为词面检索: %s", EMBED_COOLDOWN, exc)
            return lexical
        self._emb_error = ""
        return self._rrf_fuse(lexical, vec, top_k)

    def _search_lexical(self, query: str, top_k: int) -> list[tuple[float, Chunk]]:
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

    # ---------- 语义向量检索（可选） ----------
    def _chunk_key(self, text: str) -> str:
        return hashlib.sha256(f"{self._emb.model}\x00{text}".encode("utf-8")).hexdigest()

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """调用 OpenAI 兼容 /embeddings 接口，按输入顺序返回向量。"""
        assert self._emb is not None and self._client is not None
        headers = {"Content-Type": "application/json"}
        if self._emb.api_key:
            headers["Authorization"] = f"Bearer {self._emb.api_key}"
        url = self._emb.base_url + "/embeddings"
        resp = await self._client.post(
            url, json={"model": self._emb.model, "input": texts}, headers=headers
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        data.sort(key=lambda d: int(d.get("index", 0)))
        vecs = [[float(x) for x in d["embedding"]] for d in data]
        if len(vecs) != len(texts):
            raise ValueError(f"Embedding 返回条数不符: {len(vecs)}/{len(texts)}")
        return vecs

    async def _ensure_vectors(self) -> None:
        """把尚未向量化的分块批量嵌入（命中磁盘缓存的直接复用，不重复计费）。"""
        assert self._emb is not None
        keys: list[str] = []
        texts: list[str] = []
        for c in self._chunks:
            k = self._chunk_key(c.text)
            if k not in self._vectors:
                keys.append(k)
                texts.append(c.text)
        if not texts:
            return
        logger.info("[rag] 需要嵌入 %d 个新分块（模型 %s）", len(texts), self._emb.model)
        for i in range(0, len(texts), self._emb.batch):
            batch = texts[i: i + self._emb.batch]
            vecs = await self._embed_batch(batch)
            for k, v in zip(keys[i: i + self._emb.batch], vecs):
                self._vectors[k] = v
        self._save_vector_cache()

    def _search_vector(self, qvec: list[float], top_k: int) -> list[tuple[float, int]]:
        """余弦相似度排序，返回 [(相似度, chunk_id)]；零/负相似度视为无关，不注入。"""
        scored: list[tuple[float, int]] = []
        for cid, c in enumerate(self._chunks):
            v = self._vectors.get(self._chunk_key(c.text))
            if v is None:
                continue
            cos = _cosine(qvec, v)
            if cos > 0.0:  # 正交/负相关 = 语义无关，不进候选
                scored.append((cos, cid))
        scored.sort(key=lambda x: -x[0])
        return scored[:top_k]

    def _rrf_fuse(
        self,
        lexical: list[tuple[float, Chunk]],
        vector: list[tuple[float, int]],
        top_k: int,
    ) -> list[tuple[float, Chunk]]:
        """Reciprocal Rank Fusion：两路排名倒数求和，兼顾词面精确与语义泛化。"""
        fused: dict[tuple[str, str], float] = {}
        chunks: dict[tuple[str, str], Chunk] = {}
        for rank, (_, ch) in enumerate(lexical):
            key = (ch.file, ch.text)
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            chunks[key] = ch
        for rank, (_, cid) in enumerate(vector):
            ch = self._chunks[cid]
            key = (ch.file, ch.text)
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            chunks[key] = ch
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])
        return [(score, chunks[key]) for key, score in ranked[:top_k]]

    # ---------- 向量磁盘缓存（重启不重复计费） ----------
    def _load_vector_cache(self) -> None:
        self._vectors = {}
        if self._cache_path is None or not self._cache_path.is_file():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("model") == (self._emb.model if self._emb else "") and isinstance(
                data.get("vectors"), dict
            ):
                self._vectors = {
                    str(k): [float(x) for x in v] for k, v in data["vectors"].items()
                }
        except Exception as exc:  # noqa: BLE001 —— 缓存坏了不影响功能，重新嵌入即可
            logger.warning("[rag] 向量缓存读取失败（忽略并重建）: %s", exc)

    def _save_vector_cache(self) -> None:
        if self._cache_path is None or self._emb is None:
            return
        try:
            valid = {self._chunk_key(c.text) for c in self._chunks}  # 清理已不存在的分块
            payload = {
                "version": 1,
                "model": self._emb.model,
                "vectors": {k: v for k, v in self._vectors.items() if k in valid},
            }
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[rag] 向量缓存写入失败: %s", exc)
