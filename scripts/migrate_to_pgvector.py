"""
Migrate ChromaDB vectors → pgvector (Cloud SQL PostgreSQL).

What it does:
  1. Reads every collection in local ChromaDB
  2. Re-embeds each chunk with Gemini text-embedding-004
  3. Inserts into kb_chunks table in PostgreSQL (via DATABASE_URL)

Usage:
    DATABASE_URL=postgresql+asyncpg://... \
    GEMINI_API_KEY=... \
    USE_PGVECTOR=true \
    python scripts/migrate_to_pgvector.py [--dry-run]

Idempotent: skips chunks that already exist (same MD5 id).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "backend" / ".env")


CHROMA_DIR = Path.home() / "usb-assistant" / "chromadb"
GEMINI_EMBED_MODEL = "models/gemini-embedding-001"
GEMINI_EMBED_DIM = 3072


def _embed_gemini(text: str, api_key: str) -> list[float]:
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/{GEMINI_EMBED_MODEL}:embedContent?key={api_key}"
    resp = requests.post(
        url,
        json={"model": GEMINI_EMBED_MODEL, "content": {"parts": [{"text": text}]}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]["values"]


async def _ensure_schema(conn):
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


async def migrate(dry_run: bool = False):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[migrate] ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("[migrate] ERROR: DATABASE_URL not set")
        sys.exit(1)

    if not CHROMA_DIR.exists():
        print(f"[migrate] ChromaDB dir not found: {CHROMA_DIR}")
        sys.exit(1)

    import chromadb
    import asyncpg

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collections = client.list_collections()
    print(f"[migrate] Found {len(collections)} ChromaDB collection(s): {[c.name for c in collections]}")

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    async with pool.acquire() as conn:
        await _ensure_schema(conn)

    total_inserted = 0
    total_skipped = 0

    for col in collections:
        # Map collection name → shop_id
        name = col.name
        if name == "knowledge":
            shop_id = "knowledge"
        elif name.startswith("shop_"):
            shop_id = name[5:]  # strip "shop_" prefix
        else:
            shop_id = name

        print(f"\n[migrate] Collection '{name}' → shop_id='{shop_id}'")
        results = col.get(include=["documents", "metadatas"])
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []

        if not docs:
            print(f"  (empty, skipping)")
            continue

        print(f"  {len(docs)} chunks to process")

        for i, (doc, meta) in enumerate(zip(docs, metas)):
            if not doc or not doc.strip():
                continue

            source = (meta or {}).get("source", "unknown")
            chunk_id = hashlib.md5(f"{shop_id}:{doc}".encode()).hexdigest()

            async with pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT 1 FROM kb_chunks WHERE id = $1", chunk_id
                )
            if exists:
                total_skipped += 1
                if (i + 1) % 20 == 0:
                    print(f"  [{i+1}/{len(docs)}] skipped (already exists)")
                continue

            if dry_run:
                print(f"  [DRY] would insert chunk {chunk_id[:8]}... shop={shop_id} source={source}")
                total_inserted += 1
                continue

            try:
                emb = _embed_gemini(doc, api_key)
                emb_str = "[" + ",".join(str(x) for x in emb) + "]"
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO kb_chunks (id, shop_id, source, content, embedding) "
                        f"VALUES ($1,$2,$3,$4,'{emb_str}'::vector) ON CONFLICT (id) DO NOTHING",
                        chunk_id, shop_id, source, doc,
                    )
                total_inserted += 1
                if (i + 1) % 10 == 0:
                    print(f"  [{i+1}/{len(docs)}] inserted {total_inserted} so far")
                await asyncio.sleep(0.05)  # avoid SSL rate-limit disconnects
            except Exception as e:
                print(f"  [ERROR] chunk {i}: {e}")
                await asyncio.sleep(1.0)
                continue

        print(f"  done. inserted={total_inserted} skipped={total_skipped}")

    await pool.close()
    print(f"\n[migrate] Complete. Total inserted={total_inserted} skipped={total_skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(migrate(dry_run=args.dry_run))
