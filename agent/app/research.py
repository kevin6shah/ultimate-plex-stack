from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape
from urllib.parse import quote_plus, unquote, urlparse

import httpx

from .settings import Settings


logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|section|article|li|h1|h2|h3|h4|h5|h6)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _normalize_whitespace(unescape(text))


def _decode_duckduckgo_href(href: str) -> str:
    if "uddg=" in href:
        encoded = href.split("uddg=", 1)[1].split("&", 1)[0]
        return unquote(encoded)
    return href


def _is_web_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _extract_duckduckgo_links(html: str, *, max_results: int) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
        candidate = _decode_duckduckgo_href(match.group(1).strip())
        if not _is_web_url(candidate):
            continue
        host = urlparse(candidate).netloc.lower()
        if "duckduckgo.com" in host:
            continue
        if candidate in links:
            continue
        links.append(candidate)
        if len(links) >= max_results:
            break
    return links


async def _http_get_json_or_text(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str | int],
    parse_json: bool,
) -> dict | str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=True) as client:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code in TRANSIENT_HTTP_STATUS_CODES:
                    raise httpx.HTTPStatusError(
                        f"transient status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json() if parse_json else response.text
            except Exception as exc:
                last_exc = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, httpx.TransportError) or status_code in TRANSIENT_HTTP_STATUS_CODES
                if attempt >= 3 or not retryable:
                    raise
                delay = float(attempt)
                logger.warning("research http retry url=%s attempt=%s/3 delay=%.1fs error=%s", url, attempt, delay, exc)
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc


async def search_web(query: str, *, settings: Settings, max_results: int = 5) -> str:
    brave_key = ""
    if settings.brave_search_api_key_param:
        try:
            brave_key = settings.secret(settings.brave_search_api_key_param)
        except Exception as exc:
            logger.info("research brave key unavailable, falling back to public search: %s", exc)
    if brave_key:
        payload = await _http_get_json_or_text(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": DEFAULT_USER_AGENT,
                "X-Subscription-Token": brave_key,
            },
            params={
                "q": query,
                "count": min(max(max_results, 1), 10),
                "country": "us",
                "search_lang": "en",
            },
            parse_json=True,
        )
        assert isinstance(payload, dict)
        web_results = payload.get("web", {}).get("results", [])
        lines = [f"Deterministic web search results for: {query}"]
        for index, item in enumerate(web_results[:max_results], start=1):
            title = str(item.get("title") or item.get("meta_title") or "").strip()
            url = str(item.get("url") or "").strip()
            description = _normalize_whitespace(str(item.get("description") or item.get("meta_description") or ""))
            lines.append(f"\n[{index}] {title or url}")
            lines.append(f"URL: {url}")
            if description:
                lines.append(f"Snippet: {description[:900]}")
        if len(lines) == 1:
            return f"Deterministic web search returned no results for: {query}"
        return "\n".join(lines)[:8000]

    html = await _http_get_json_or_text(
        DUCKDUCKGO_HTML_URL,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        params={"q": query},
        parse_json=False,
    )
    assert isinstance(html, str)
    links = _extract_duckduckgo_links(html, max_results=max_results)
    if not links:
        return f"Deterministic web search returned no results for: {query}"
    lines = [f"Deterministic web search links for: {query}"]
    for index, link in enumerate(links, start=1):
        lines.append(f"[{index}] {link}")
    return "\n".join(lines)[:4000]


async def fetch_page_content(url: str, *, max_chars: int = 6000) -> str:
    html = await _http_get_json_or_text(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        params={},
        parse_json=False,
    )
    assert isinstance(html, str)
    return _strip_html(html)[:max_chars]


def sanitize_tool_output(text: str, *, limit: int = 8000) -> str:
    redacted = re.sub(r"(?i)(authorization:?\s*bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", text)
    redacted = re.sub(r"(?i)(x-subscription-token:?\s*)[A-Za-z0-9._-]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(api[_-]?key['\"=:\s]+)[A-Za-z0-9._-]+", r"\1[REDACTED]", redacted)
    return redacted[:limit]
