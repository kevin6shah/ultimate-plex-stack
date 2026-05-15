from app.research import _extract_duckduckgo_links, sanitize_tool_output


def test_extract_duckduckgo_links_filters_redirects_and_dupes() -> None:
    html = """
    <a href="/l/?uddg=https%3A%2F%2Fexample.com%2Fcamera">One</a>
    <a href="/l/?uddg=https%3A%2F%2Fexample.com%2Fcamera">Dup</a>
    <a href="https://duckduckgo.com/y.js">Ignore</a>
    <a href="https://www.reddit.com/r/cameras/comments/abc123/">Two</a>
    """
    assert _extract_duckduckgo_links(html, max_results=5) == [
        "https://example.com/camera",
        "https://www.reddit.com/r/cameras/comments/abc123/",
    ]


def test_sanitize_tool_output_redacts_common_secret_shapes() -> None:
    raw = "Authorization: Bearer abc123\napi_key=secret123\nX-Subscription-Token: brave-secret"
    sanitized = sanitize_tool_output(raw)
    assert "abc123" not in sanitized
    assert "secret123" not in sanitized
    assert "brave-secret" not in sanitized
    assert "[REDACTED]" in sanitized
