"""
Hybrid entity extraction for the Brain graph — rules-first, deterministic, stdlib.

Pulls the nodes of the memory graph out of entry content: technologies, products,
projects, and files. Always runs (free, offline). An optional ``ctx.llm`` enrichment
pass can add finer entities/relations later; this module is the dependable floor.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Curated technology lexicon (lowercased). Matched on word boundaries.
TECH_TERMS = frozenset({
    "postgres", "postgresql", "redis", "sqlite", "mysql", "mongodb", "qdrant",
    "pinecone", "elasticsearch", "neo4j", "duckdb", "valkey",
    "cloudflare", "vercel", "aws", "gcp", "azure", "hetzner", "fly.io", "railway",
    "docker", "kubernetes", "k8s", "terraform", "nginx", "caddy",
    "react", "next.js", "nextjs", "vue", "svelte", "astro", "remix", "tailwind",
    "framer", "framer-motion", "lenis", "vite", "webpack",
    "fastapi", "flask", "django", "express", "node", "bun", "deno", "rails",
    "python", "typescript", "javascript", "rust", "go", "golang", "java", "ruby",
    "openai", "anthropic", "claude", "gemini", "codex", "deepseek", "llama",
    "mistral", "ollama", "openrouter", "llm", "rag", "mcp", "fts5", "embeddings",
    "model2vec", "sqlite-vec", "graphrag", "hipporag",
    "razorpay", "stripe", "twilio", "upstash", "supabase", "clerk", "auth0",
    "hermes", "cron", "webhook", "oauth", "jwt", "websocket", "grpc",
})

# Known multi-word products / orgs (lowercased key -> display).
KNOWN_PRODUCTS = {
    "memory bridge": "Memory Bridge",
    "council of hats": "Council of Hats",
    "antharmaya labs": "Antharmaya Labs",
    "antharmaya": "Antharmaya",
    "photoselect": "PhotoSelect",
    "perplexity brain": "Perplexity Brain",
}

# CamelCase product names (PhotoSelect, CallCatch, MemGPT) — 2+ humps.
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
# File references by extension.
_FILE = re.compile(
    r"\b([\w./~-]+\.(?:py|ts|tsx|js|jsx|mjs|md|sql|ya?ml|json|toml|sh|css|html|rs|go))\b"
)


def _has_word(haystack_low: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack_low) is not None


def extract_entities(
    text: str,
    project: Optional[str] = None,
    max_entities: int = 14,
) -> list[tuple[str, str]]:
    """Return a deduped list of (display_name, kind) entities found in ``text``.

    kinds: project | tech | product | file. Deterministic and order-stable.
    """
    if not text:
        text = ""
    low = text.lower()
    found: dict[tuple[str, str], str] = {}  # (canonical, kind) -> display

    # Project (from the session's project path) is always a node when present.
    if project:
        pname = clean_project_name(project)
        if pname:
            found[(pname.lower(), "project")] = pname

    for term in TECH_TERMS:
        if _has_word(low, term):
            found.setdefault((term, "tech"), term)

    for key, disp in KNOWN_PRODUCTS.items():
        if key in low:
            found.setdefault((key, "product"), disp)

    for m in _CAMEL.findall(text):
        found.setdefault((m.lower(), "product"), m)

    for m in _FILE.findall(text):
        base = m.split("/")[-1]
        if len(base) <= 64:
            found.setdefault((base.lower(), "file"), base)

    out = [(disp, kind) for (_canon, kind), disp in found.items()]
    return out[:max_entities]


def canonical(name: str) -> str:
    return name.strip().lower()


# Path segments to strip when deriving a readable project name.
_PATH_NOISE = {"", "home", "users", "desktop", "downloads", "documents", "projects", "code", "src"}


def clean_project_name(project: str) -> str:
    """Turn an encoded project path into a readable name.

    Claude Code encodes project dirs as '-home-user-Desktop-bolting-photoselect'.
    Strip the home/desktop noise and keep the meaningful tail.
    """
    raw = str(project).strip()
    if not raw:
        return ""
    # Real filesystem path → take the basename.
    if "/" in raw:
        base = Path(raw).name
        return base or raw
    # Dash-encoded path (no slashes, leading dash) → drop noise segments.
    if raw.startswith("-") or raw.lower().startswith(("home-", "-home")):
        segs = [s for s in raw.split("-") if s and s.lower() not in _PATH_NOISE]
        # drop a leading username segment (the part right after home)
        if len(segs) > 1 and segs[0].islower() and len(segs[0]) <= 12:
            segs = segs[1:] or segs
        return "-".join(segs[-3:]) if segs else raw
    return raw
