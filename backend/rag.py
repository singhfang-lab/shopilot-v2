"""
RAG layer — dual mode:
  USE_PGVECTOR=true  → PostgreSQL + pgvector (production)
  USE_PGVECTOR=false → ChromaDB local file    (development, default)

Public API (unchanged from v1):
  add(chunks, source, shop_id) -> int
  delete_by_source(source, shop_id) -> int
  query(question, top_k, shop_id) -> list[str]
  query_multi(question, shop_id, merchant_top_k, platform_top_k) -> dict
  count(shop_id) -> int
  clear_shop_kb(shop_id) -> int
  list_sources(shop_id) -> list[dict]
  ingest_text(text, shop_id, source) -> int   [async]
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

# ── Embedding ────────────────────────────────────────────────────────────────

GEMINI_EMBED_MODEL = "models/gemini-embedding-001"
GEMINI_EMBED_DIM   = 3072
_OLLAMA_BASE       = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_URL   = f"{_OLLAMA_BASE}/api/embeddings"
OLLAMA_EMBED_MODEL = "nomic-embed-text"


def _embed(text: str) -> list[float]:
    """Generate embedding. Uses Gemini if key available, else Ollama (local dev)."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        return _embed_gemini(text, api_key)
    return _embed_ollama(text)


def _embed_gemini(text: str, api_key: str) -> list[float]:
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/{GEMINI_EMBED_MODEL}:embedContent?key={api_key}"
    resp = requests.post(url, json={"model": GEMINI_EMBED_MODEL, "content": {"parts": [{"text": text}]}}, timeout=30)
    resp.raise_for_status()
    return resp.json()["embedding"]["values"]


def _embed_ollama(text: str) -> list[float]:
    import requests
    resp = requests.post(OLLAMA_EMBED_URL, json={"model": OLLAMA_EMBED_MODEL, "prompt": text}, timeout=30)
    resp.raise_for_status()
    return resp.json()["embedding"]


# ── Mode detection ────────────────────────────────────────────────────────────

def _use_pgvector() -> bool:
    return os.environ.get("USE_PGVECTOR", "false").lower() == "true"


# ── Chunking (shared) ─────────────────────────────────────────────────────────

def _chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return [c.strip() for c in chunks if c.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# pgvector backend
# ══════════════════════════════════════════════════════════════════════════════

_pg_pool = None
_pg_pool_loop = None


async def _get_pg_pool():
    global _pg_pool, _pg_pool_loop
    import asyncio
    current_loop = asyncio.get_event_loop()
    # Reset pool if event loop changed or pool is closed
    if _pg_pool is not None and (_pg_pool_loop is not current_loop or _pg_pool._closed):
        _pg_pool = None
        _pg_pool_loop = None
    if _pg_pool is None:
        import asyncpg
        dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        _pg_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        _pg_pool_loop = current_loop
        async with _pg_pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id          TEXT PRIMARY KEY,
                    shop_id     TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    embedding   vector({GEMINI_EMBED_DIM}),
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_shop_idx ON kb_chunks (shop_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_source_idx ON kb_chunks (shop_id, source)"
            )
    return _pg_pool


def _pg_shop_id(shop_id: str | None) -> str:
    # Only use "knowledge" for explicit None (platform KB); empty string is not a valid merchant shop_id
    if shop_id is None:
        return "knowledge"
    return shop_id



async def _pg_add_async(chunks: list[str], source: str, shop_id: str | None) -> int:
    import asyncio
    pool = await _get_pg_pool()
    sid = _pg_shop_id(shop_id)

    # Deduplicate and filter
    clean: list[tuple[str, str]] = []  # (doc_id, chunk)
    seen: set[str] = set()
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        doc_id = hashlib.md5(f"{sid}:{chunk}".encode()).hexdigest()
        if doc_id not in seen:
            seen.add(doc_id)
            clean.append((doc_id, chunk))

    if not clean:
        return 0

    # Filter out already-existing IDs in one query
    ids = [doc_id for doc_id, _ in clean]
    async with pool.acquire() as conn:
        existing = set(
            r["id"] for r in await conn.fetch(
                "SELECT id FROM kb_chunks WHERE id = ANY($1::text[])", ids
            )
        )
    new_chunks = [(doc_id, chunk) for doc_id, chunk in clean if doc_id not in existing]

    if not new_chunks:
        return 0

    # Batch embed with bounded concurrency (20 concurrent Gemini calls)
    sem = asyncio.Semaphore(20)

    async def _embed_one(doc_id: str, chunk: str):
        async with sem:
            loop = asyncio.get_event_loop()
            emb = await loop.run_in_executor(None, _embed, chunk)
            return doc_id, chunk, emb

    results = await asyncio.gather(*[_embed_one(d, c) for d, c in new_chunks], return_exceptions=True)

    # Batch insert all successful embeddings
    rows = []
    for r in results:
        if isinstance(r, Exception):
            continue
        doc_id, chunk, emb = r
        emb_str = "[" + ",".join(str(x) for x in emb) + "]"
        rows.append((doc_id, sid, source, chunk, emb_str))

    if not rows:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO kb_chunks (id, shop_id, source, content, embedding) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT (id) DO NOTHING",
            rows,
        )

    return len(rows)


async def _pg_delete_by_source_async(source: str, shop_id: str | None) -> int:
    pool = await _get_pg_pool()
    sid = _pg_shop_id(shop_id)
    result = await pool.execute(
        "DELETE FROM kb_chunks WHERE shop_id = $1 AND source = $2", sid, source
    )
    return int(result.split()[-1])


async def _pg_query_async(question: str, top_k: int, shop_id: str | None) -> list[str]:
    pool = await _get_pg_pool()
    sid = _pg_shop_id(shop_id)
    cnt = await pool.fetchval("SELECT COUNT(*) FROM kb_chunks WHERE shop_id = $1", sid)
    if not cnt:
        return []
    emb = _embed(question)
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"
    rows = await pool.fetch(
        "SELECT content FROM kb_chunks WHERE shop_id = $1 ORDER BY embedding <=> $2::vector LIMIT $3",
        sid, emb_str, min(top_k, cnt),
    )
    return [r["content"] for r in rows]


async def _pg_count_async(shop_id: str | None) -> int:
    pool = await _get_pg_pool()
    return await pool.fetchval(
        "SELECT COUNT(*) FROM kb_chunks WHERE shop_id = $1", _pg_shop_id(shop_id)
    ) or 0


async def _pg_clear_async(shop_id: str) -> int:
    pool = await _get_pg_pool()
    result = await pool.execute(
        "DELETE FROM kb_chunks WHERE shop_id = $1", _pg_shop_id(shop_id)
    )
    return int(result.split()[-1])


async def _pg_list_sources_async(shop_id: str | None) -> list[dict]:
    pool = await _get_pg_pool()
    rows = await pool.fetch(
        "SELECT source, COUNT(*) as chunk_count FROM kb_chunks WHERE shop_id = $1 GROUP BY source",
        _pg_shop_id(shop_id),
    )
    return [{"source": r["source"], "chunk_count": r["chunk_count"]} for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# ChromaDB backend (local dev)
# ══════════════════════════════════════════════════════════════════════════════

CHROMA_DIR = Path.home() / "usb-assistant" / "chromadb"
DEFAULT_COLLECTION = "knowledge"

_chroma_client = None
_chroma_collections: dict[str, object] = {}


def _chroma_collection_name(shop_id: str | None) -> str:
    if not shop_id:
        return DEFAULT_COLLECTION
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", shop_id.strip())[:50] or DEFAULT_COLLECTION
    return f"shop_{safe}"


def _get_chroma_collection(shop_id: str | None = None):
    global _chroma_client, _chroma_collections
    name = _chroma_collection_name(shop_id)
    if name in _chroma_collections:
        return _chroma_collections[name]
    import chromadb
    if _chroma_client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = _chroma_client.get_or_create_collection(name)
    _chroma_collections[name] = col
    return col


def _chroma_add(chunks: list[str], source: str, shop_id: str | None) -> int:
    if not chunks:
        return 0
    collection = _get_chroma_collection(shop_id)
    added = 0
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        doc_id = hashlib.md5(f"{shop_id}:{chunk}".encode()).hexdigest()
        if collection.get(ids=[doc_id])["ids"]:
            continue
        try:
            collection.add(
                ids=[doc_id],
                embeddings=[_embed(chunk)],
                documents=[chunk],
                metadatas=[{"source": source, "shop_id": shop_id or ""}],
            )
            added += 1
        except Exception:
            continue
    return added


def _chroma_delete_by_source(source: str, shop_id: str | None) -> int:
    collection = _get_chroma_collection(shop_id)
    results = collection.get(where={"source": source})
    ids = results.get("ids", [])
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def _chroma_query(question: str, top_k: int, shop_id: str | None) -> list[str]:
    collection = _get_chroma_collection(shop_id)
    if collection.count() == 0:
        return []
    emb = _embed(question)
    results = collection.query(query_embeddings=[emb], n_results=min(top_k, collection.count()))
    return results.get("documents", [[]])[0]


def _chroma_clear(shop_id: str) -> int:
    global _chroma_client, _chroma_collections
    name = _chroma_collection_name(shop_id)
    try:
        import chromadb
        if _chroma_client is None:
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        try:
            col = _chroma_client.get_collection(name)
            n = col.count()
            _chroma_client.delete_collection(name)
        except Exception:
            n = 0
        _chroma_collections.pop(name, None)
        return n
    except Exception:
        return 0


def _chroma_list_sources(shop_id: str | None) -> list[dict]:
    try:
        collection = _get_chroma_collection(shop_id)
        if collection.count() == 0:
            return []
        results = collection.get(include=["metadatas"])
        sources: dict[str, int] = {}
        for meta in results.get("metadatas", []):
            src = meta.get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1
        return [{"source": s, "chunk_count": c} for s, c in sources.items()]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Public API — routes to the right backend automatically
# ══════════════════════════════════════════════════════════════════════════════

def _run_async(coro):
    """Run an async coroutine from sync context, compatible with running event loops."""
    import asyncio
    import concurrent.futures
    try:
        loop = asyncio.get_running_loop()
        # Already inside a running loop — run in a thread with its own loop
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def add(chunks: list[str], source: str, shop_id: str | None = None) -> int:
    if _use_pgvector():
        return _run_async(_pg_add_async(chunks, source, shop_id))
    return _chroma_add(chunks, source, shop_id)


def delete_by_source(source: str, shop_id: str | None = None) -> int:
    if _use_pgvector():
        return _run_async(_pg_delete_by_source_async(source, shop_id))
    return _chroma_delete_by_source(source, shop_id)


def query(question: str, top_k: int = 3, shop_id: str | None = None) -> list[str]:
    if _use_pgvector():
        return _run_async(_pg_query_async(question, top_k, shop_id))
    return _chroma_query(question, top_k, shop_id)


def query_multi(question: str, shop_id: str | None, merchant_top_k: int = 3, platform_top_k: int = 2) -> dict[str, list[str]]:
    emb = _embed(question)
    out: dict[str, list[str]] = {"merchant": [], "platform": []}

    if _use_pgvector():
        async def _both():
            pool = await _get_pg_pool()
            results = {}
            for key, sid, k in [
                ("merchant", _pg_shop_id(shop_id) if shop_id and shop_id.strip() else None, merchant_top_k),
                ("platform", "knowledge", platform_top_k),
            ]:
                if sid is None:
                    results[key] = []
                    continue
                cnt = await pool.fetchval("SELECT COUNT(*) FROM kb_chunks WHERE shop_id = $1", sid)
                if not cnt:
                    results[key] = []
                    continue
                emb_str = "[" + ",".join(str(x) for x in emb) + "]"
                rows = await pool.fetch(
                    "SELECT content FROM kb_chunks WHERE shop_id = $1 ORDER BY embedding <=> $2::vector LIMIT $3",
                    sid, emb_str, min(k, cnt),
                )
                results[key] = [r["content"] for r in rows]
            return results

        return _run_async(_both())

    # ChromaDB path
    if shop_id:
        col = _get_chroma_collection(shop_id)
        if col.count() > 0:
            r = col.query(query_embeddings=[emb], n_results=min(merchant_top_k, col.count()))
            out["merchant"] = r.get("documents", [[]])[0] or []

    platform_col = _get_chroma_collection(None)
    if platform_col.count() > 0:
        r = platform_col.query(query_embeddings=[emb], n_results=min(platform_top_k, platform_col.count()))
        out["platform"] = r.get("documents", [[]])[0] or []

    return out


def count(shop_id: str | None = None) -> int:
    try:
        if _use_pgvector():
            return _run_async(_pg_count_async(shop_id))
        return _get_chroma_collection(shop_id).count()
    except Exception:
        return 0


def clear_shop_kb(shop_id: str) -> int:
    if _use_pgvector():
        return _run_async(_pg_clear_async(shop_id))
    return _chroma_clear(shop_id)


async def clear_shop_kb_async(shop_id: str) -> int:
    if _use_pgvector():
        return await _pg_clear_async(shop_id)
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, _chroma_clear, shop_id)


async def list_sources_async(shop_id: str | None = None) -> list[dict]:
    if _use_pgvector():
        return await _pg_list_sources_async(shop_id)
    return _chroma_list_sources(shop_id)


async def get_onboarding_chunks_async(shop_id: str) -> list[str]:
    """Return onboarding-generated text chunks for a shop."""
    if _use_pgvector():
        pool = await _get_pg_pool()
        rows = await pool.fetch(
            "SELECT content FROM kb_chunks WHERE shop_id = $1 AND source LIKE 'onboarding:%' ORDER BY created_at",
            _pg_shop_id(shop_id),
        )
        return [r["content"] for r in rows]
    # ChromaDB fallback
    try:
        col = _get_chroma_collection(shop_id)
        data = col.get(include=["documents", "metadatas"])
        return [
            doc for doc, meta in zip(data["documents"], data["metadatas"])
            if (meta or {}).get("source", "").startswith("onboarding:")
        ]
    except Exception:
        return []


def list_sources(shop_id: str | None = None) -> list[dict]:
    if _use_pgvector():
        return _run_async(_pg_list_sources_async(shop_id))
    return _chroma_list_sources(shop_id)


async def ingest_text(text: str, shop_id: str, source: str) -> int:
    """Async: chunk and ingest text into KB."""
    chunks = _chunk_text(text)
    if _use_pgvector():
        return await _pg_add_async(chunks, source, shop_id)
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _chroma_add, chunks, source, shop_id)
