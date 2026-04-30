"""
知识库导入脚本
用法：python -m backend.ingest [--dir knowledge/路径] [--reset]

--dir   指定知识库目录，默认 knowledge/商户助手知识库_完整试投版
--reset 先清空现有向量库再导入
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

CHROMA_DIR = Path.home() / "usb-assistant" / "chromadb"
COLLECTION_NAME = "knowledge"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

DEFAULT_KB_DIR = Path(__file__).parent.parent / "knowledge" / "商户助手知识库_完整试投版"

CHUNK_SIZE = 600   # 每段最多字符数
CHUNK_OVERLAP = 80 # 段间重叠字符数


def embed(text: str) -> list[float]:
    resp = requests.post(
        OLLAMA_EMBED_URL,
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落优先切分，超长段再按字符切。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 2 <= size:
            buf = (buf + "\n\n" + para).strip()
        else:
            if buf:
                chunks.append(buf)
            if len(para) <= size:
                buf = para
            else:
                # 强制按字符切长段
                for i in range(0, len(para), size - overlap):
                    chunks.append(para[i:i + size])
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def load_markdown_files(kb_dir: Path) -> list[tuple[str, str]]:
    """返回 [(doc_id, text), ...] 列表，跳过 README/目录/说明类文件。"""
    skip_patterns = {"README", "00_给Gamma", "使用说明", "总目录", "合集"}
    docs = []
    for f in sorted(kb_dir.rglob("*.md")):
        if any(p in f.name for p in skip_patterns):
            continue
        text = f.read_text(encoding="utf-8").strip()
        if len(text) < 50:
            continue
        rel = str(f.relative_to(kb_dir))
        docs.append((rel, text))
    return docs


def ingest(kb_dir: Path, reset: bool = False) -> None:
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            print("已清空旧知识库")
        except Exception:
            pass

    collection = client.get_or_create_collection(COLLECTION_NAME)

    docs = load_markdown_files(kb_dir)
    print(f"找到 {len(docs)} 个文档，开始切分并向量化…")

    total_chunks = 0
    for doc_idx, (rel_path, text) in enumerate(docs):
        chunks = chunk_text(text)
        ids, embeddings, texts, metadatas = [], [], [], []
        for i, chunk in enumerate(chunks):
            doc_id = f"{rel_path}::{i}"
            ids.append(doc_id)
            texts.append(chunk)
            metadatas.append({"source": rel_path, "chunk": i})
            try:
                embeddings.append(embed(chunk))
            except Exception as e:
                print(f"  ⚠️  向量化失败 [{doc_id}]: {e}")
                continue
            time.sleep(0.05)  # 避免请求过快

        if ids:
            collection.upsert(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)

        total_chunks += len(chunks)
        print(f"  [{doc_idx+1}/{len(docs)}] {rel_path} → {len(chunks)} 段")

    print(f"\n完成！共导入 {total_chunks} 段，知识库总量：{collection.count()} 条")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=DEFAULT_KB_DIR)
    parser.add_argument("--reset", action="store_true", help="先清空旧知识库")
    args = parser.parse_args()

    if not args.dir.exists():
        print(f"目录不存在: {args.dir}", file=sys.stderr)
        sys.exit(1)

    ingest(args.dir, reset=args.reset)


if __name__ == "__main__":
    main()
