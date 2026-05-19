from __future__ import annotations

import re


BROWSER_STEP_SCREENSHOT_PREFIX = "browser/screenshots/browser-use-step-"


def normalize_artifact_path(path: str) -> str:
    return path.replace("\\", "/").strip()


def is_browser_step_screenshot(path: str) -> bool:
    normalized = normalize_artifact_path(path).lower()
    return normalized.startswith(BROWSER_STEP_SCREENSHOT_PREFIX)


def query_requests_browser_images(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\bscreenshot\b",
        r"\bscreenshots\b",
        r"\bscreen shot\b",
        r"\bscreen shots\b",
        r"\bimage\b",
        r"\bimages\b",
        r"\bpicture\b",
        r"\bpictures\b",
        r"\bphoto\b",
        r"\bphotos\b",
        r"\bpng\b",
        r"\bjpe?g\b",
        r"\bvisual proof\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def query_requests_output_files(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    patterns = (
        r"\bsave\b.{0,20}\b(file|report|pdf|txt|csv|xlsx|document|artifact)\b",
        r"\bcreate\b.{0,20}\b(file|report|pdf|txt|csv|xlsx|document|artifact)\b",
        r"\bwrite\b.{0,20}\b(file|report|pdf|txt|csv|xlsx|document|artifact)\b",
        r"\bexport\b",
        r"\bdownload\b",
        r"\battach\b",
        r"\bsend\b.{0,20}\b(file|report|pdf|txt|csv|xlsx|document|artifact)\b",
        r"\breturn\b.{0,20}\b(file|report|pdf|txt|csv|xlsx|document|artifact)\b",
        r"\bpdf\b",
        r"\btxt\b",
        r"\bcsv\b",
        r"\bxlsx\b",
        r"\breport\b",
        r"\bdocument\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def visible_output_files(
    output_files: list[str],
    *,
    include_browser_step_screenshots: bool,
    include_other_output_files: bool,
) -> list[str]:
    visible = output_files if include_browser_step_screenshots else [
        name for name in output_files if not is_browser_step_screenshot(name)
    ]
    if include_other_output_files:
        return visible
    return [name for name in visible if is_browser_step_screenshot(name)]
