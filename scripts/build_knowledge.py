"""Build or incrementally update the ChromaDB knowledge base from knowledge/ directory."""

from __future__ import annotations

import sys
import requests
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
CHROMA_DIR = REPO_ROOT / "chromadb"
CHUNK_SIZE = 500
COLLECTION_NAME = "knowledge"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"


def embed(text: str) -> list[float]:
    resp = requests.post(OLLAMA_EMBED_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=30)
    resp.raise_for_status()
    return resp.json()["embedding"]


def chunk_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size
    return chunks


def doc_id(filepath: Path, idx: int) -> str:
    return f"{filepath.name}::{idx}"


def main():
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(
        list(KNOWLEDGE_DIR.glob("*.txt"))
        + list(KNOWLEDGE_DIR.glob("*.md"))
        + list(KNOWLEDGE_DIR.rglob("**/*.md"))
        + list(KNOWLEDGE_DIR.rglob("**/*.txt"))
    )
    # deduplicate while preserving order
    seen = set()
    unique_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)
    files = unique_files

    if not files:
        print("knowledge/ 目录为空，无文件可处理。")
        return

    print(f"发现 {len(files)} 个文件，正在连接 Ollama embedding 服务…")
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(COLLECTION_NAME)

    existing_ids: set[str] = set(collection.get()["ids"])
    total_added = 0
    total_files = 0

    for filepath in files:
        text = filepath.read_text(encoding="utf-8").strip()
        if not text:
            continue
        chunks = chunk_text(text)
        new_chunks = []
        new_ids = []

        for idx, chunk in enumerate(chunks):
            did = doc_id(filepath, idx)
            if did in existing_ids:
                continue
            new_chunks.append(chunk)
            new_ids.append(did)

        if not new_chunks:
            print(f"  跳过（已存在）: {filepath.name}")
            continue

        print(f"  处理: {filepath.name} → {len(new_chunks)} 块", end="", flush=True)
        embeddings = [embed(chunk) for chunk in new_chunks]
        collection.add(documents=new_chunks, embeddings=embeddings, ids=new_ids)
        total_added += len(new_chunks)
        total_files += 1
        print(f"  ✓")

    print(f"\n完成。处理了 {total_files} 个文件，新增 {total_added} 块，知识库共 {collection.count()} 块。")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    main()
