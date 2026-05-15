from __future__ import annotations

import logging
import re
import asyncio
from html import unescape
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional, TypeVar
from urllib.parse import quote_plus, unquote, urlparse

import httpx


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
BOT_BLOCK_PATTERNS = (
    "captcha",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "temporarily blocked",
    "robot or human",
    "enable javascript and cookies to continue",
)
TEXT_SELECTORS = ("main", "article", "[role='main']", "body")
MAX_EXCERPT_CHARS = 1600
RETRYABLE_BROWSER_ERROR_PATTERNS = (
    "net::err_http2_protocol_error",
    "net::err_connection_reset",
    "net::err_connection_closed",
    "net::err_connection_aborted",
    "net::err_network_changed",
    "net::err_internet_disconnected",
    "net::err_timed_out",
    "net::err_name_not_resolved",
    "timeout",
    "target page, context or browser has been closed",
)
TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
T = TypeVar("T")

logger = logging.getLogger(__name__)


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _extract_urls(task: str) -> list[str]:
    return re.findall(r"https?://[^\s)>\]]+", task)


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


def _is_bot_blocked(text: str, title: str = "") -> bool:
    haystack = f"{title}\n{text}".lower()
    return any(pattern in haystack for pattern in BOT_BLOCK_PATTERNS)


def _search_url(task: str) -> str:
    return f"https://html.duckduckgo.com/html/?q={quote_plus(task)}"


def _is_retryable_browser_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(pattern in message for pattern in RETRYABLE_BROWSER_ERROR_PATTERNS)


async def _run_with_retries(
    label: str,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 1.0,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_retryable_browser_error(exc):
                raise
            delay = base_delay_seconds * attempt
            logger.warning(
                "browser retry label=%s attempt=%s/%s delay=%.1fs error=%s",
                label,
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _selector_candidates(selector: str) -> list[str]:
    parts = [part.strip() for part in selector.split(",")]
    return [part for part in parts if part] or [selector]


async def _http_fetch_text(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0, connect=6.0),
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True,
    ) as client:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = await client.get(url)
                if response.status_code in TRANSIENT_HTTP_STATUS_CODES:
                    raise httpx.HTTPStatusError(
                        f"transient status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return _strip_html(response.text)
            except Exception as exc:
                last_exc = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = isinstance(exc, httpx.TransportError) or status_code in TRANSIENT_HTTP_STATUS_CODES
                if attempt >= 3 or not retryable:
                    raise
                delay = float(attempt)
                logger.warning(
                    "browser http retry url=%s attempt=%s/3 delay=%.1fs error=%s",
                    url,
                    attempt,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc


async def _extract_page_text_via_js(page) -> str:
    text = await page.evaluate(
        """() => {
            const body = document.body;
            const root = document.documentElement;
            const value =
              (body && (body.innerText || body.textContent)) ||
              (root && (root.innerText || root.textContent)) ||
              '';
            return value;
        }"""
    )
    return _normalize_whitespace(str(text or ""))


async def _extract_selector_text(page, selector: str, *, timeout_ms: int) -> str:
    for candidate in _selector_candidates(selector):
        locator = page.locator(candidate).first
        try:
            if await locator.count():
                text = await locator.inner_text(timeout=timeout_ms)
                normalized = _normalize_whitespace(text)
                if normalized:
                    return normalized
        except Exception as exc:
            logger.info("browser selector read fallback selector=%s error=%s", candidate, exc)
            continue
    return ""


async def _extract_page_text(page, selector: str = "body") -> str:
    if selector == "body":
        for candidate in TEXT_SELECTORS:
            text = await _extract_selector_text(page, candidate, timeout_ms=2000)
            if text:
                return text
        text = await _extract_page_text_via_js(page)
        if text:
            return text
        return ""

    text = await _extract_selector_text(page, selector, timeout_ms=3000)
    if text:
        return text

    fallback = await _extract_page_text_via_js(page)
    if fallback:
        return fallback
    return ""


class BrowserSession:
    def __init__(self, workspace=None) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._workspace = workspace

    async def start(self, start_url: str = "") -> str:
        if self._page is not None:
            if start_url:
                await self.goto(start_url)
            return await self.describe()
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--single-process",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        self._page = await self._context.new_page()
        if start_url:
            await self.goto(start_url)
        return "Browser session started."

    async def ensure_started(self) -> None:
        if self._page is None:
            await self.start()

    async def goto(self, url: str) -> str:
        await self.ensure_started()
        logger.info("browser goto url=%s", url)
        await _run_with_retries(
            f"goto:{url}",
            lambda: self._page.goto(url, wait_until="domcontentloaded", timeout=20000),
        )
        await self._page.wait_for_timeout(800)
        return await self.describe()

    async def click(self, selector: str) -> str:
        await self.ensure_started()
        logger.info("browser click selector=%s", selector)
        await self._page.locator(selector).first.click(timeout=10000)
        await self._page.wait_for_timeout(800)
        return await self.describe()

    async def type_text(self, selector: str, text: str, submit: bool = False) -> str:
        await self.ensure_started()
        logger.info("browser type selector=%s submit=%s", selector, submit)
        locator = self._page.locator(selector).first
        await locator.fill(text, timeout=10000)
        if submit:
            await locator.press("Enter")
        await self._page.wait_for_timeout(800)
        return await self.describe()

    async def press(self, selector: str, key: str) -> str:
        await self.ensure_started()
        logger.info("browser press selector=%s key=%s", selector, key)
        await self._page.locator(selector).first.press(key, timeout=10000)
        await self._page.wait_for_timeout(800)
        return await self.describe()

    async def read(self, selector: str = "body", limit: int = 3500) -> str:
        await self.ensure_started()
        text = await _extract_page_text(self._page, selector=selector)
        return text[:limit]

    async def wait_for_text(self, text: str, timeout_seconds: int = 10) -> str:
        await self.ensure_started()
        await self._page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_seconds * 1000)
        return await self.describe()

    async def upload_file(self, selector: str, local_path: str) -> str:
        await self.ensure_started()
        logger.info("browser upload selector=%s path=%s", selector, local_path)
        await self._page.locator(selector).first.set_input_files(local_path, timeout=10000)
        await self._page.wait_for_timeout(800)
        return await self.describe()

    async def screenshot(self, relative_path: str = "browser/current-page.png", full_page: bool = True) -> str:
        await self.ensure_started()
        if self._workspace is None:
            raise RuntimeError("workspace is required for screenshots")
        target: Path = self._workspace.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(target), full_page=full_page)
        return f"Saved screenshot to {relative_path}"

    async def current_url(self) -> str:
        await self.ensure_started()
        return self._page.url

    async def list_links(self, limit: int = 20) -> str:
        await self.ensure_started()
        links = await self._page.locator("a[href]").evaluate_all(
            """(els, limit) => els.slice(0, limit).map((el) => ({
                text: (el.innerText || '').trim(),
                href: el.href || ''
            }))""",
            limit,
        )
        lines = []
        for index, link in enumerate(links, start=1):
            lines.append(f"[{index}] {link.get('text') or '(no text)'} -> {link.get('href')}")
        return "\n".join(lines)[:3500]

    async def describe(self) -> str:
        await self.ensure_started()
        title = await self._page.title()
        url = self._page.url
        body = await self.read(limit=1800)
        return f"URL: {url}\nTitle: {title}\nBody excerpt: {body}"

    async def close(self) -> None:
        if self._page is not None:
            await self._page.close()
            self._page = None
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


async def _visit_url(page, url: str) -> tuple[str, str, str]:
    logger.info("browser visiting url=%s", url)
    await _run_with_retries(
        f"visit:{url}",
        lambda: page.goto(url, wait_until="domcontentloaded", timeout=15000),
    )
    await page.wait_for_timeout(750)
    title = await page.title()
    text = await _extract_page_text(page)
    if _is_bot_blocked(text, title):
        raise RuntimeError(f"bot detection page at {url}")
    return page.url, title, text


async def _search_result_links(page, task: str, *, max_pages: int) -> list[str]:
    logger.info("browser searching public web task=%s", task)
    await _run_with_retries(
        f"search:{task[:80]}",
        lambda: page.goto(_search_url(task), wait_until="domcontentloaded", timeout=15000),
    )
    await page.wait_for_timeout(500)
    links: list[str] = []
    for href in await page.locator("a[href]").evaluate_all("(els) => els.map((el) => el.getAttribute('href') || '')"):
        candidate = _decode_duckduckgo_href(href.strip())
        if not _is_web_url(candidate):
            continue
        host = urlparse(candidate).netloc.lower()
        if "duckduckgo.com" in host:
            continue
        if candidate not in links:
            links.append(candidate)
        if len(links) >= max_pages:
            break
    return links


def _format_observations(task: str, observations: Iterable[tuple[str, str, str]]) -> str:
    lines = [f"Read-only browser notes for: {task}"]
    for index, (url, title, text) in enumerate(observations, start=1):
        excerpt = text[:MAX_EXCERPT_CHARS]
        lines.append(f"\n[{index}] {title or url}")
        lines.append(f"URL: {url}")
        lines.append(f"Excerpt: {excerpt}")
    return "\n".join(lines)


async def run_browser_task(task: str, *, max_pages: int, max_steps: int) -> str:
    del max_steps
    from playwright.async_api import async_playwright

    direct_urls = _extract_urls(task)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--single-process",
            ],
        )
        page = await browser.new_page(
            user_agent=DEFAULT_USER_AGENT,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        observations: list[tuple[str, str, str]] = []
        failures: list[str] = []

        try:
            targets = direct_urls[:max_pages]
            if not targets:
                targets = await _search_result_links(page, task, max_pages=max_pages)
            logger.info("browser targets=%s", targets)

            for target in targets[:max_pages]:
                try:
                    url, title, text = await _visit_url(page, target)
                except Exception as exc:
                    logger.warning("browser playwright visit failed url=%s error=%s", target, exc)
                    failures.append(f"{target}: {exc}")
                    try:
                        fallback_text = await _http_fetch_text(target)
                    except Exception as fallback_exc:
                        logger.warning("browser http fallback failed url=%s error=%s", target, fallback_exc)
                        failures.append(f"{target} fallback: {fallback_exc}")
                        continue
                    logger.info("browser http fallback succeeded url=%s", target)
                    observations.append((target, target, fallback_text))
                    continue
                observations.append((url, title, text))

            if observations:
                logger.info("browser collected observations=%s", len(observations))
                return _format_observations(task, observations)

            failure_block = "; ".join(failures[:4]) or "no public pages could be read"
            logger.warning("browser task failed task=%s failures=%s", task, failure_block)
            return (
                "Browser task could not read a public page for this request. "
                f"Last failures: {failure_block}"
            )
        finally:
            await page.close()
            await browser.close()
