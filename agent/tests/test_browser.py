import asyncio

from app.browser import _extract_page_text


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
