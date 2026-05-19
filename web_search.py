"""Web search integration for tansaibot (#38).

Uses DuckDuckGo Instant Answer API (no API key needed) as primary.
Falls back to a simple page title scrape if Instant Answer returns empty.

Usage:
    from web_search import search
    results = await search("cuaca jakarta hari ini", max_results=3)
    # returns list of {"title": ..., "snippet": ..., "url": ...}
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_DDG_URL = "https://api.duckduckgo.com/"
_DDG_SEARCH_URL = "https://duckduckgo.com/html/"

_HTTP_CLIENT: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"User-Agent": "tansaibot/2.0 (web search)"},
            http2=True,
        )
    return _HTTP_CLIENT


async def search(query: str, max_results: int = 3) -> list[dict[str, str]]:
    """Search using DuckDuckGo. Returns list of result dicts."""
    results = await _ddg_instant(query, max_results)
    if not results:
        results = await _ddg_html(query, max_results)
    return results


async def _ddg_instant(query: str, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo Instant Answer API — fast, no key needed."""
    client = _get_client()
    try:
        resp = await client.get(
            _DDG_URL,
            params={"q": query, "format": "json", "no_html": "1", "t": "tansaibot"},
        )
        data: dict[str, Any] = resp.json()
    except Exception as exc:
        logger.debug("DDG instant failed: %s", exc)
        return []

    results: list[dict[str, str]] = []

    # Abstract (encyclopedia-style answers)
    abstract = data.get("AbstractText", "").strip()
    abstract_url = data.get("AbstractURL", "").strip()
    if abstract:
        results.append({
            "title": data.get("Heading", query),
            "snippet": abstract[:400],
            "url": abstract_url,
        })

    # Related topics
    for topic in data.get("RelatedTopics", []):
        if len(results) >= max_results:
            break
        if isinstance(topic, dict) and topic.get("Text") and topic.get("FirstURL"):
            results.append({
                "title": topic.get("Text", "")[:80],
                "snippet": topic.get("Text", "")[:300],
                "url": topic.get("FirstURL", ""),
            })

    return results[:max_results]


async def _ddg_html(query: str, max_results: int) -> list[dict[str, str]]:
    """Fallback: scrape DuckDuckGo HTML results."""
    client = _get_client()
    try:
        resp = await client.post(
            _DDG_SEARCH_URL,
            data={"q": query},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        body = resp.text
    except Exception as exc:
        logger.debug("DDG HTML fallback failed: %s", exc)
        return []

    # Rough HTML scrape — extract result titles and URLs
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>(.+?)</a>.*?'
        r'<a class="result__snippet"[^>]*>(.+?)</a>',
        re.DOTALL,
    )
    for m in pattern.finditer(body):
        url = html.unescape(m.group(1)).strip()
        title = re.sub(r"<[^>]+>", "", html.unescape(m.group(2))).strip()
        snippet = re.sub(r"<[^>]+>", "", html.unescape(m.group(3))).strip()
        if url and title:
            results.append({"title": title[:80], "snippet": snippet[:300], "url": url})
        if len(results) >= max_results:
            break

    return results


def format_results_for_prompt(results: list[dict[str, str]], query: str) -> str:
    """Format search results as a prompt-injectable context block."""
    if not results:
        return f"[Web Search: tidak ada hasil untuk '{query}']"
    lines = [f"[Web Search hasil untuk: {query}]"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   URL: {r['url']}")
    return "\n".join(lines)


async def summarize_url(url: str, max_chars: int = 2000) -> str:
    """Fetch a URL and return its text content (for URL summarizer #34)."""
    client = _get_client()
    try:
        resp = await client.get(url, follow_redirects=True)
        body = resp.text
        # Strip HTML tags
        text = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.DOTALL)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as exc:
        return f"Gagal mengambil konten dari URL: {exc}"
