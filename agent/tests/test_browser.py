import asyncio

from app.browser import _extract_page_text, _run_with_retries


class FakeLocator:
    def __init__(self, text: str = "", *, raise_on_read: bool = False, count: int = 1) -> None:
        self._text = text
        self._raise_on_read = raise_on_read
        self._count = count
        self.first = self

    async def count(self) -> int:
        return self._count

    async def inner_text(self, timeout: int = 0) -> str:
        if self._raise_on_read:
            raise RuntimeError(f"timeout {timeout}")
        return self._text


class FakePage:
    def __init__(self, mapping: dict[str, FakeLocator], js_text: str = "") -> None:
        self.mapping = mapping
        self.js_text = js_text

    def locator(self, selector: str) -> FakeLocator:
        return self.mapping.get(selector, FakeLocator(count=0))

    async def evaluate(self, script: str) -> str:
        return self.js_text


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
