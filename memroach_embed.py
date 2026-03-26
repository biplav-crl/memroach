#!/usr/bin/env python3
"""MemRoach Embeddings — shared module for generating and searching vector embeddings.

Supports both OpenAI and Voyage AI as embedding providers.
Configure via embed_provider and embed_api_key in memroach_config.json.
"""

import json
import os
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from memroach_crypto import encrypt_text, decrypt_text
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"
EMBED_DIM = 1024  # Must match VECTOR(1024) in schema
CHUNK_SIZE = 500  # Target tokens per chunk (approx 4 chars per token)
CHUNK_CHARS = CHUNK_SIZE * 4


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_provider(config: Optional[dict] = None) -> str:
    """Detect which embedding provider to use based on config."""
    if config is None:
        config = _load_config()

    model = config.get("embed_model", "")
    if model.startswith("text-embedding"):
        return "openai"
    elif model.startswith("voyage"):
        return "voyage"

    # Auto-detect from API key format
    api_key = config.get("embed_api_key", "")
    if api_key.startswith("sk-"):
        return "openai"
    elif api_key.startswith("pa-"):
        return "voyage"

    return "openai"  # default


def embed_texts(texts: list[str], config: Optional[dict] = None) -> list[list[float]]:
    """Generate embeddings for a list of texts.

    Returns a list of 1024-dim float vectors.
    """
    if config is None:
        config = _load_config()

    api_key = config.get("embed_api_key", "")
    if not api_key:
        raise ValueError("embed_api_key not set in memroach_config.json")

    provider = get_provider(config)

    if provider == "openai":
        return _embed_openai(texts, api_key, config)
    elif provider == "voyage":
        return _embed_voyage(texts, api_key, config)
    else:
        raise ValueError(f"Unknown embed provider: {provider}")


def _embed_openai(texts: list[str], api_key: str, config: dict) -> list[list[float]]:
    """Generate embeddings via OpenAI API."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("pip install openai  (required for OpenAI embeddings)")

    client = OpenAI(api_key=api_key)
    model = config.get("embed_model", "text-embedding-3-small")

    # OpenAI supports batching up to 2048 texts
    all_embeddings = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        response = client.embeddings.create(
            model=model,
            input=batch,
            dimensions=EMBED_DIM,
        )
        for item in response.data:
            all_embeddings.append(item.embedding)

    return all_embeddings


def _embed_voyage(texts: list[str], api_key: str, config: dict) -> list[list[float]]:
    """Generate embeddings via Voyage AI API."""
    try:
        import voyageai
    except ImportError:
        raise ImportError("pip install voyageai  (required for Voyage embeddings)")

    client = voyageai.Client(api_key=api_key)
    model = config.get("embed_model", "voyage-3")

    # Voyage supports batching up to 128 texts
    all_embeddings = []
    for i in range(0, len(texts), 128):
        batch = texts[i:i + 128]
        result = client.embed(batch, model=model)
        all_embeddings.extend(result.embeddings)

    return all_embeddings


def chunk_text(text: str, file_path: str = "") -> list[dict]:
    """Split text into chunks for embedding.

    Returns list of {chunk_index, chunk_text} dicts.
    Small files (<CHUNK_CHARS) are kept as a single chunk.
    """
    if len(text) <= CHUNK_CHARS:
        return [{"chunk_index": 0, "chunk_text": text}]

    chunks = []
    # Split on double newlines (paragraph boundaries) first
    paragraphs = text.split("\n\n")
    current = ""
    idx = 0

    for para in paragraphs:
        if len(current) + len(para) + 2 > CHUNK_CHARS and current:
            chunks.append({"chunk_index": idx, "chunk_text": current.strip()})
            idx += 1
            current = para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append({"chunk_index": idx, "chunk_text": current.strip()})

    return chunks


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)
    dot = np.dot(a_arr, b_arr)
    norm = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if norm == 0:
        return 0.0
    return float(dot / norm)


def hybrid_search(conn, user: str, query_embedding: list[float],
                  query_text: str, limit: int = 10,
                  alpha: float = 0.7, visibility: Optional[str] = None,
                  owner: Optional[str] = None) -> list[dict]:
    """Hybrid search combining vector similarity and keyword matching.

    Args:
        conn: pg8000 connection
        user: current user
        query_embedding: embedded query vector
        query_text: raw query text for keyword matching
        limit: max results
        alpha: weight for vector score (1-alpha for keyword)
        visibility: filter by visibility ('team' for team-only search)
        owner: filter by owner (None = current user)
    """
    config = _load_config()

    # Vector search: get top candidates from embeddings
    query_user = owner if owner else user
    vis_filter = ""
    params = {"user": query_user, "lim": limit * 3}

    if visibility == "team":
        vis_filter = "AND f.visibility = 'team'"
        # For team search, don't filter by user
        user_filter = ""
    else:
        user_filter = "AND e.user_name = :user"

    # Get embeddings with file metadata
    vector_results = conn.run(
        f"SELECT e.file_path, e.chunk_text, e.embedding, f.file_type, "
        f"f.file_size, f.visibility, f.synced_at "
        f"FROM memroach_embeddings e "
        f"JOIN memroach_files f ON e.user_name = f.user_name AND e.file_path = f.file_path "
        f"WHERE f.is_deleted = false {user_filter} {vis_filter} "
        f"ORDER BY e.created_at DESC LIMIT :lim",
        **params,
    )

    if not vector_results:
        # Fall back to keyword-only search
        return _keyword_search(conn, user, query_text, limit, visibility)

    # Score each result
    scored = []
    seen_paths = set()

    for row in vector_results:
        path, chunk_text_raw, embedding_str, ftype, fsize, vis, synced_at = row
        chunk_text = decrypt_text(conn, chunk_text_raw, config) if HAS_CRYPTO else chunk_text_raw

        # Parse stored embedding
        try:
            if isinstance(embedding_str, str):
                stored_embedding = json.loads(embedding_str)
            elif isinstance(embedding_str, (list, tuple)):
                stored_embedding = list(embedding_str)
            else:
                continue
        except (json.JSONDecodeError, TypeError):
            continue

        # Vector score
        vec_score = cosine_similarity(query_embedding, stored_embedding)

        # Keyword score (simple: does query appear in chunk or path?)
        query_lower = query_text.lower()
        text_lower = (chunk_text + " " + path).lower()
        keyword_score = 1.0 if query_lower in text_lower else 0.0
        # Partial keyword matching
        if keyword_score == 0:
            words = query_lower.split()
            matches = sum(1 for w in words if w in text_lower)
            keyword_score = matches / len(words) if words else 0.0

        # Combined score
        final_score = (alpha * vec_score) + ((1 - alpha) * keyword_score)

        if path not in seen_paths:
            seen_paths.add(path)
            scored.append({
                "path": path,
                "type": ftype,
                "size": fsize,
                "visibility": vis,
                "score": round(final_score, 4),
                "snippet": chunk_text[:200],
                "synced_at": synced_at.isoformat() if hasattr(synced_at, 'isoformat') else str(synced_at),
            })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def _keyword_search(conn, user: str, query_text: str, limit: int,
                    visibility: Optional[str] = None) -> list[dict]:
    """Fallback keyword-only search when no embeddings exist."""
    vis_filter = "AND f.visibility = 'team'" if visibility == "team" else ""
    user_filter = "" if visibility == "team" else "AND f.user_name = :user"

    params = {"pattern": f"%{query_text}%", "lim": limit}
    if visibility != "team":
        params["user"] = user

    results = conn.run(
        f"SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.synced_at "
        f"FROM memroach_files f "
        f"WHERE f.is_deleted = false {user_filter} {vis_filter} "
        f"AND f.file_path ILIKE :pattern "
        f"ORDER BY f.synced_at DESC LIMIT :lim",
        **params,
    )

    return [{
        "path": r[0], "type": r[1], "size": r[2], "visibility": r[3],
        "score": 1.0, "snippet": "",
        "synced_at": r[4].isoformat() if hasattr(r[4], 'isoformat') else str(r[4]),
    } for r in results]


def embed_and_store(conn, user: str, file_path: str, content: str,
                    content_hash: str, config: dict):
    """Generate embeddings for a file and store them in the database.

    Only processes memory and skill files (skips sessions, binary caches).
    Skips if embeddings already exist for this content_hash.
    """
    # Check if already embedded
    existing = conn.run(
        "SELECT COUNT(*) FROM memroach_embeddings "
        "WHERE user_name = :user AND file_path = :path AND content_hash = :hash",
        user=user,
        path=file_path,
        hash=content_hash,
    )
    if existing and existing[0][0] > 0:
        return 0  # Already embedded

    # Chunk the content
    chunks = chunk_text(content, file_path)

    # Generate embeddings
    try:
        texts = [c["chunk_text"] for c in chunks]
        embeddings = embed_texts(texts, config)
    except Exception as e:
        # Don't fail the push if embedding fails
        return -1

    # Delete old embeddings for this file (different content_hash)
    conn.run(
        "DELETE FROM memroach_embeddings "
        "WHERE user_name = :user AND file_path = :path AND content_hash != :hash",
        user=user,
        path=file_path,
        hash=content_hash,
    )

    # Store new embeddings
    stored = 0
    for chunk, embedding in zip(chunks, embeddings):
        # Convert embedding to string format for VECTOR type
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        stored_text = encrypt_text(conn, chunk["chunk_text"], config) if HAS_CRYPTO else chunk["chunk_text"]
        conn.run(
            "UPSERT INTO memroach_embeddings "
            "(user_name, file_path, content_hash, embedding, chunk_index, chunk_text) "
            "VALUES (:user, :path, :hash, :vec, :idx, :text)",
            user=user,
            path=file_path,
            hash=content_hash,
            vec=vec_str,
            idx=chunk["chunk_index"],
            text=stored_text,
        )
        stored += 1

    return stored
