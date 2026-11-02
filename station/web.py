"""Web search and page extraction through Tavily, metered by `api` (the key comes from `secrets`)."""

from __future__ import annotations

from . import api
from .runner import clip


class WebError(RuntimeError):
    pass


def _call(cfg: dict, ledger, endpoint: str, payload: dict, what: str) -> dict:
    try:
        return api.request(cfg, ledger, "tavily", "POST", f"/{endpoint}", json_body=payload, what=what,
                           timeout=60).json()
    except api.ApiError as e:
        detail = e.body[:300].decode(errors="replace") if e.body else ""
        raise WebError(f"{e}: {detail}".rstrip(": ")) from None
    except ValueError:
        raise WebError("Tavily returned something that is not JSON") from None


def search(cfg: dict, ledger, query: str, max_results: int = 5) -> str:
    """`cfg` is the full Station config."""
    if not query.strip():
        raise WebError("query is empty")
    data = _call(cfg, ledger, "search", {"query": query, "max_results": max(1, min(int(max_results), 10))},
                 f"web search: {query}")
    rows = []
    for i, r in enumerate(data.get("results") or [], 1):
        rows.append(f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n   {clip(r.get('content', '').strip(), 500)}")
    return "\n".join(rows) or "no results"


def extract(cfg: dict, ledger, urls: list[str], max_chars: int = 8000) -> str:
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))][:5]
    if not urls:
        raise WebError("need 1 to 5 http(s) urls")
    data = _call(cfg, ledger, "extract", {"urls": urls}, f"web extract: {len(urls)} url(s)")
    parts = []
    for r in data.get("results") or []:
        parts.append(f"# {r.get('url', '')}\n{clip((r.get('raw_content') or '').strip(), max_chars)}")
    for f in data.get("failed_results") or []:
        parts.append(f"# {f.get('url', '')}\n(failed: {f.get('error', '')})")
    return "\n\n".join(parts) or "no content"
