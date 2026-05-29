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
        raise SystemExit("usage: patch_restaurant_cli_browser.py <browser.js>")
    target = Path(sys.argv[1])
    text = target.read_text(encoding="utf-8")
    text = _replace_once(
        r"""async function loadPlaywright\(\) \{.*?\n\}""",
        """async function loadPlaywright() {\n  return await import("/usr/lib/node_modules/playwright/index.mjs");\n}""",
        text,
    )
    text = _replace_once(
        r"""async function launch\(opts = \{\}\) \{.*?\n\}""",
        """async function launch(opts = {}) {\n  const pw = await loadPlaywright();\n  const profileDir = process.env["RESTAURANT_CLI_OT_PROFILE_DIR"] ?? `${process.env["HOME"]}/.cache/restaurant-cli/firefox-profile-opentable`;\n  const forceHeadless = process.env["RESTAURANT_CLI_HEADLESS"] === "1";\n  const headed = !forceHeadless && (opts.headed ?? true);\n  const context = await pw.firefox.launchPersistentContext(profileDir, {\n    headless: !headed,\n    viewport: { width: 1400, height: 900 },\n    locale: "en-US",\n    timezoneId: "America/Los_Angeles"\n  });\n  const page = context.pages()[0] ?? await context.newPage();\n  page.setDefaultTimeout(opts.timeoutMs ?? 3e4);\n  return { browser: null, context, page };\n}""",
        text,
    )
    target.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
