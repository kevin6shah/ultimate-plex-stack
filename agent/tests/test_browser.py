import asyncio
from typing import Optional

from app.browser import (
    _extract_page_text,
    _extract_search_result_links_from_html,
    _guard_zero_dollar_before_action,
    _run_with_retries,
    _selector_or_text_suggests_terminal_action,
)


class FakeLocator:
    def __init__(self, text: str = "", *, raise_on_read: bool = False, count: int = 1) -> None:
        self._text = text
        self._raise_on_read = raise_on_read
        self._count = count
        self.first = self
        self.attributes: dict[str, str] = {}

    async def count(self) -> int:
        return self._count

    async def inner_text(self, timeout: int = 0) -> str:
        if self._raise_on_read:
            raise RuntimeError(f"timeout {timeout}")
        return self._text

    async def get_attribute(self, name: str) -> Optional[str]:
        return self.attributes.get(name)


class FakePage:
    def __init__(self, mapping: dict[str, FakeLocator], js_text: str = "") -> None:
        self.mapping = mapping
        self.js_text = js_text

    def locator(self, selector: str) -> FakeLocator:
        return self.mapping.get(selector, FakeLocator(count=0))

    async def evaluate(self, script: str) -> str:
        return self.js_text


def test_selector_or_text_suggests_terminal_action_detects_booking_submit_controls() -> None:
    assert _selector_or_text_suggests_terminal_action(selector="button.reserve-now", text="")
    assert _selector_or_text_suggests_terminal_action(selector="button", text="Complete booking")
    assert not _selector_or_text_suggests_terminal_action(selector="input.search", text="Search venues")


def test_extract_page_text_falls_back_across_comma_selectors() -> None:
    page = FakePage(
        {
            "main": FakeLocator(raise_on_read=True),
            "article": FakeLocator("Article content"),
            ".article-body": FakeLocator(count=0),
            ".content": FakeLocator(count=0),
        }
    )

    text = asyncio.run(_extract_page_text(page, selector="main, article, .article-body, .content"))
    assert text == "Article content"


def test_extract_page_text_uses_js_fallback_when_selectors_fail() -> None:
    page = FakePage(
        {
            "main": FakeLocator(raise_on_read=True),
            "article": FakeLocator(raise_on_read=True),
            "[role='main']": FakeLocator(count=0),
            "body": FakeLocator(count=0),
        },
        js_text="Body text from JS fallback",
    )

    text = asyncio.run(_extract_page_text(page, selector="body"))
    assert text == "Body text from JS fallback"


def test_run_with_retries_recovers_from_transient_browser_error() -> None:
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR")
        return "ok"

    result = asyncio.run(_run_with_retries("goto:test", flaky, attempts=3, base_delay_seconds=0))
    assert result == "ok"
    assert attempts["count"] == 2


def test_run_with_retries_does_not_retry_non_retryable_error() -> None:
    attempts = {"count": 0}

    async def broken() -> str:
        attempts["count"] += 1
        raise RuntimeError("permission denied")

    try:
        asyncio.run(_run_with_retries("goto:test", broken, attempts=3, base_delay_seconds=0))
    except RuntimeError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("expected runtime error")

    assert attempts["count"] == 1


def test_extract_search_result_links_from_html_decodes_duckduckgo_redirects() -> None:
    html = """
    <html><body>
      <a href="/l/?uddg=https%3A%2F%2Fexample.com%2Fcamera-review">Result 1</a>
      <a href="https://duckduckgo.com/y.js">ignore</a>
      <a href="https://www.reddit.com/r/cameras/comments/abc123/">Result 2</a>
    </body></html>
    """

    links = _extract_search_result_links_from_html(html, max_pages=5)
    assert links == [
        "https://example.com/camera-review",
        "https://www.reddit.com/r/cameras/comments/abc123/",
    ]


def test_guard_zero_dollar_before_action_blocks_non_zero_terminal_submit() -> None:
    page = FakePage({"button.reserve-now": FakeLocator("Reserve table")}, js_text="Reservation summary total $25.00")

    try:
        asyncio.run(_guard_zero_dollar_before_action(page, selector="button.reserve-now", locator=page.locator("button.reserve-now")))
    except RuntimeError as exc:
        assert "NON_ZERO_CHECKOUT_BLOCKED" in str(exc)
    else:
        raise AssertionError("expected non-zero checkout block")


def test_guard_zero_dollar_before_action_skips_non_terminal_controls() -> None:
    page = FakePage({"input.search": FakeLocator("Search")}, js_text="Find restaurants near me")
    asyncio.run(_guard_zero_dollar_before_action(page, selector="input.search", locator=page.locator("input.search")))
