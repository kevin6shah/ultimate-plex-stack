from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any


STEALTH_INIT_SCRIPT = """
(() => {
  const webdriverDescriptor = {
    get: () => undefined,
  };
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', webdriverDescriptor);
  } catch (_) {}
  try {
    Object.defineProperty(window.navigator, 'webdriver', webdriverDescriptor);
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Native Client' },
      ],
    });
  } catch (_) {}
  const seed = window.__fridayFingerprintSeed || 'friday';
  const hashNumber = (value) => {
    let out = 0;
    for (let i = 0; i < value.length; i += 1) out = ((out << 5) - out) + value.charCodeAt(i);
    return Math.abs(out);
  };
  const webglVendor = ['Intel Inc.', 'Google Inc.', 'Apple Inc.'][hashNumber(seed) % 3];
  const webglRenderer = [
    'Intel Iris OpenGL Engine',
    'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)',
    'ANGLE (Apple, Apple M4, OpenGL 4.1)',
  ][hashNumber(seed + 'renderer') % 3];
  const patchGetParameter = (proto) => {
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function(parameter) {
      if (parameter === 37445) return webglVendor;
      if (parameter === 37446) return webglRenderer;
      return original.apply(this, arguments);
    };
  };
  patchGetParameter(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
  patchGetParameter(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);
})();
""".strip()


@dataclass(frozen=True)
class BrowserFingerprint:
    seed: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    locale: str = "en-US"
    timezone_id: str = "America/New_York"

    def viewport(self) -> dict[str, int]:
        return {"width": self.viewport_width, "height": self.viewport_height}

    def metadata(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "user_agent": self.user_agent,
            "viewport": self.viewport(),
            "locale": self.locale,
            "timezone_id": self.timezone_id,
        }


def browser_fingerprint_seed(task: str, *, existing: str = "") -> str:
    basis = existing.strip() or task.strip() or "friday"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def build_browser_fingerprint(*, seed: str, user_agent: str) -> BrowserFingerprint:
    rng = random.Random(seed)
    width = rng.choice((1365, 1400, 1440, 1512))
    height = rng.choice((820, 860, 900, 940))
    return BrowserFingerprint(seed=seed, user_agent=user_agent, viewport_width=width, viewport_height=height)


def common_chromium_args() -> list[str]:
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--single-process",
    ]


def stagehand_launch_options(*, fingerprint: BrowserFingerprint, executable_path: str = "", user_data_dir: str = "", downloads_path: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "headless": True,
        "args": common_chromium_args(),
        "chromiumSandbox": False,
        "locale": fingerprint.locale,
        "viewport": fingerprint.viewport(),
        "preserveUserDataDir": True,
    }
    if executable_path:
        payload["executablePath"] = executable_path
    if user_data_dir:
        payload["userDataDir"] = user_data_dir
    if downloads_path:
        payload["downloadsPath"] = downloads_path
    return payload


def browser_use_profile_kwargs(*, fingerprint: BrowserFingerprint, user_data_dir: str, downloads_path: str) -> dict[str, Any]:
    return {
        "headless": True,
        "user_agent": fingerprint.user_agent,
        "user_data_dir": user_data_dir,
        "downloads_path": downloads_path,
        "disable_security": False,
        "deterministic_rendering": False,
        "locale": fingerprint.locale,
        "viewport": fingerprint.viewport(),
        "args": common_chromium_args(),
    }


def serialize_browser_session_state(*, cookies: list[dict[str, Any]], local_storage: dict[str, str], session_storage: dict[str, str], fingerprint: BrowserFingerprint) -> dict[str, Any]:
    return {
        "cookies": cookies,
        "local_storage": local_storage,
        "session_storage": session_storage,
        "fingerprint": fingerprint.metadata(),
    }


def fingerprint_metadata_json(fingerprint: BrowserFingerprint) -> str:
    return json.dumps(fingerprint.metadata(), sort_keys=True)
