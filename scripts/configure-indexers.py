#!/usr/bin/env python3

from __future__ import annotations

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
PROWLARR_CONFIG = ROOT_DIR / "config" / "prowlarr" / "config.xml"
APP_PROFILE_ID = 1
CF_TAG_LABEL = "cf-bypass"
COMMAND_NAME = "ApplicationIndexerSync"
ARR_APPS = [
    ("radarr", 7878),
    ("sonarr", 8989),
]

DESIRED_INDEXERS = [
    {
        "name": "YTS",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [],
        "reason": "Primary movie source",
    },
    {
        "name": "1337x",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [CF_TAG_LABEL],
        "reason": "Primary TV source and movie backup",
    },
    {
        "name": "Demonoid Clone",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [],
        "reason": "Working backup source",
    },
    {
        "name": "EZTV",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [CF_TAG_LABEL],
        "reason": "TV backup source",
    },
    {
        "name": "showRSS",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [],
        "reason": "TV RSS backup source",
    },
    {
        "name": "Nyaa.si",
        "implementation": "Cardigann",
        "enable": True,
        "tags": [],
        "reason": "Existing anime backup source",
    },
    {
        "name": "The Pirate Bay",
        "implementation": "Cardigann",
        "enable": False,
        "tags": [CF_TAG_LABEL],
        "reason": "Disabled because helper-backed local tests still fail",
    },
]


def read_api_key() -> str:
    return ET.parse(PROWLARR_CONFIG).getroot().findtext("ApiKey", "")


def arr_api_key(app: str) -> str:
    return ET.parse(ROOT_DIR / "config" / app / "config.xml").getroot().findtext("ApiKey", "")


def api_request(method: str, path: str, payload: dict | list | None = None) -> dict | list:
    headers = {
        "X-Api-Key": read_api_key(),
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://localhost:9696{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def arr_api_request(app: str, port: int, method: str, path: str, payload: dict | list | None = None) -> dict | list:
    headers = {
        "X-Api-Key": arr_api_key(app),
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


def ensure_tag_id(label: str) -> int:
    for tag in api_request("GET", "/api/v1/tag"):
        if tag.get("label") == label:
            return int(tag["id"])
    created = api_request("POST", "/api/v1/tag", {"label": label})
    return int(created["id"])


def find_schema(indexers: list[dict], name: str, implementation: str) -> dict:
    for indexer in indexers:
        if indexer.get("name") == name and indexer.get("implementation") == implementation:
            return indexer
    raise RuntimeError(f"Indexer schema not found: {name} ({implementation})")


def find_existing(indexers: list[dict], name: str, implementation: str) -> dict | None:
    for indexer in indexers:
        if indexer.get("name") == name and indexer.get("implementation") == implementation:
            return indexer
    return None


def wait_for_command(command_id: int, timeout: int = 120) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        command = api_request("GET", f"/api/v1/command/{command_id}")
        if command.get("status") in {"completed", "failed", "aborted"}:
            return command
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for command {command_id}")


def field_value(fields: list[dict], name: str) -> str | None:
    for field in fields:
        if field.get("name") == name:
            return field.get("value")
    return None


def main() -> int:
    tag_ids = {
        CF_TAG_LABEL: ensure_tag_id(CF_TAG_LABEL),
    }
    schema_indexers = api_request("GET", "/api/v1/indexer/schema")
    live_indexers = api_request("GET", "/api/v1/indexer")
    results: list[str] = []

    for desired in DESIRED_INDEXERS:
        tags = [tag_ids[label] for label in desired["tags"]]
        existing = find_existing(live_indexers, desired["name"], desired["implementation"])

        if existing:
            payload = api_request("GET", f"/api/v1/indexer/{existing['id']}")
            action = "updated"
            endpoint = f"/api/v1/indexer/{existing['id']}"
            method = "PUT"
        else:
            payload = find_schema(schema_indexers, desired["name"], desired["implementation"])
            action = "created"
            endpoint = "/api/v1/indexer"
            method = "POST"

        payload["appProfileId"] = APP_PROFILE_ID
        payload["enable"] = desired["enable"]
        payload["tags"] = tags

        api_request(method, endpoint, payload)
        results.append(
            f"{action}\t{desired['name']}\tenabled={desired['enable']}\ttags={json.dumps(tags)}\t{desired['reason']}"
        )

    command = api_request("POST", "/api/v1/command", {"name": COMMAND_NAME, "forceSync": True})
    command = wait_for_command(int(command["id"]))
    live_indexers = api_request("GET", "/api/v1/indexer")
    disabled_ids = {
        int(indexer["id"])
        for indexer in live_indexers
        if not indexer.get("enable", True)
    }

    for app, port in ARR_APPS:
        for indexer in arr_api_request(app, port, "GET", "/api/v3/indexer"):
            if not indexer.get("name", "").endswith("(Prowlarr)"):
                continue
            base_url = field_value(indexer.get("fields", []), "baseUrl") or ""
            if not base_url.startswith("http://vpn-web-proxy:9696/"):
                continue
            try:
                prowlarr_id = int(base_url.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                continue
            if prowlarr_id in disabled_ids:
                arr_api_request(app, port, "DELETE", f"/api/v3/indexer/{indexer['id']}")
                results.append(f"pruned\t{app}\t{indexer['name']}\tdisabled upstream")

    for line in results:
        print(line)
    print(f"command\t{COMMAND_NAME}\tid={command.get('id', 'unknown')}\tstatus={command.get('status')}")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
