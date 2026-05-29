from __future__ import annotations

import re
import sys
from pathlib import Path


def _replace_once(pattern: str, replacement: str, text: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"expected one match for pattern: {pattern!r}")
    return updated


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_strider_opentable_browser.py <browser.js>")
    target = Path(sys.argv[1])
    text = target.read_text(encoding="utf-8")
    text = _replace_once(
        r'import \{ chromium \} from "playwright";',
        'import { firefox } from "/usr/lib/node_modules/playwright/index.mjs";',
        text,
    )
    text = _replace_once(
        r"""async function initBrowser\(\) \{.*?\n\}""",
        """async function initBrowser() {\n    if (browser)\n        return;\n    browser = await firefox.launch({\n        headless: true,\n    });\n    context = await browser.newContext({\n        viewport: { width: 1280, height: 800 },\n        locale: \"en-US\",\n        timezoneId: \"America/New_York\",\n        userAgent: \"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:132.0) Gecko/20100101 Firefox/132.0\",\n        extraHTTPHeaders: {\n            \"Accept-Language\": \"en-US,en;q=0.9\",\n        },\n    });\n    await context.addInitScript(() => {\n        Object.defineProperty(navigator, \"webdriver\", { get: () => undefined });\n        Object.defineProperty(navigator, \"plugins\", {\n            get: () => [1, 2, 3, 4, 5],\n        });\n    });\n    await loadCookies(context);\n    page = await context.newPage();\n    await page.route(\"**/*.{png,jpg,jpeg,gif,svg,woff,woff2,mp4,webm}\", (route) => route.abort());\n}""",
        text,
    )
    target.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
