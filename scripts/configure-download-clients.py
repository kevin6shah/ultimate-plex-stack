#!/usr/bin/env python3

from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ARR_APPS = [
    ("radarr", 7878),
    ("sonarr", 8989),
]
TARGET_HOST = "vpn-web-proxy"


def api_key_for(app: str) -> str:
    config = ROOT_DIR / "config" / app / "config.xml"
    return ET.parse(config).getroot().findtext("ApiKey", "")


def api_request(app: str, port: int, method: str, path: str, payload: dict | list | None = None) -> dict | list:
    headers = {
        "X-Api-Key": api_key_for(app),
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def update_field(fields: list[dict], name: str, value: str) -> None:
    for field in fields:
        if field.get("name") == name:
            field["value"] = value
            return
    raise RuntimeError(f"Field not found: {name}")


def main() -> int:
    for app, port in ARR_APPS:
        clients = api_request(app, port, "GET", "/api/v3/downloadclient")
        target = next((client for client in clients if client.get("name") == "Transmission"), None)
        if not target:
            raise RuntimeError(f"Transmission download client not found in {app}")

        full = api_request(app, port, "GET", f"/api/v3/downloadclient/{target['id']}")
        update_field(full["fields"], "host", TARGET_HOST)
        api_request(app, port, "PUT", f"/api/v3/downloadclient/{target['id']}", full)
        api_request(app, port, "POST", "/api/v3/downloadclient/test", full)
        print(f"updated\t{app}\tTransmission\thost={TARGET_HOST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
