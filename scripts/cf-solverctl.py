#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
PROWLARR_DB = ROOT_DIR / "config" / "prowlarr" / "prowlarr.db"
PROWLARR_CONFIG = ROOT_DIR / "config" / "prowlarr" / "config.xml"
DEFAULT_INDEXERS = ["1337x", "EZTV", "TorrentGalaxyClone", "The Pirate Bay"]
TAG_LABEL = "cf-bypass"
PROXY_NAME = "CF Solver"
PROXY_SETTINGS = {
    "flaresolverr": "http://127.0.0.1:8191/",
    "byparr": "http://127.0.0.1:8192/",
}
HELPER_SERVICES = tuple(PROXY_SETTINGS.keys())


def read_api_key() -> str:
    return ET.parse(PROWLARR_CONFIG).getroot().findtext("ApiKey", "")


def run_cmd(*args: str) -> None:
    subprocess.run(args, cwd=ROOT_DIR, check=True)


def run_cmd_capture(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def wait_for_service(service: str, timeout: int = 60) -> None:
    started = time.time()
    while time.time() - started < timeout:
        running = {
            line.strip()
            for line in run_cmd_capture("docker", "compose", "ps", "--services", "--status", "running").stdout.splitlines()
            if line.strip()
        }
        if service in running:
            return
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for service {service}")


def wait_for_http(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> None:
    started = time.time()
    while time.time() - started < timeout:
        request = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if 200 <= response.status < 400:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {url}")


def backup_db() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = PROWLARR_DB.with_name(f"prowlarr.db.bak-{stamp}")
    backup.write_bytes(PROWLARR_DB.read_bytes())
    return backup


def ensure_tag_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("select Id from Tags where Label = ?", (TAG_LABEL,)).fetchone()
    if row:
        return int(row[0])
    connection.execute("insert into Tags(Label) values (?)", (TAG_LABEL,))
    row = connection.execute("select Id from Tags where Label = ?", (TAG_LABEL,)).fetchone()
    return int(row[0])


def merge_tag_list(raw_tags: str | None, tag_id: int, enabled: bool) -> str:
    tags = []
    if raw_tags:
        try:
            tags = [int(tag) for tag in json.loads(raw_tags)]
        except json.JSONDecodeError:
            tags = []
    if enabled and tag_id not in tags:
        tags.append(tag_id)
    if not enabled:
        tags = [tag for tag in tags if tag != tag_id]
    return json.dumps(sorted(tags))


def switch_solver(mode: str) -> None:
    if mode not in (*PROXY_SETTINGS.keys(), "off"):
        raise ValueError(f"Unsupported mode: {mode}")

    if mode != "off":
        run_cmd("docker", "compose", "up", "-d", "wireguard", "transmission", "prowlarr", "vpn-web-proxy", mode)
        wait_for_service(mode, timeout=60)

    run_cmd("docker", "compose", "stop", "prowlarr")
    backup = backup_db()

    connection = sqlite3.connect(PROWLARR_DB)
    try:
        tag_id = ensure_tag_id(connection)
        proxy_tags = json.dumps([tag_id]) if mode != "off" else json.dumps([])

        row = connection.execute(
            "select Id from IndexerProxies where Name = ? limit 1",
            (PROXY_NAME,),
        ).fetchone()

        if mode == "off":
            if row:
                connection.execute(
                    "update IndexerProxies set Tags = ? where Id = ?",
                    (proxy_tags, int(row[0])),
                )
        else:
            settings = json.dumps(
                {
                    "host": PROXY_SETTINGS[mode],
                    "requestTimeout": 60,
                }
            )
            if row:
                connection.execute(
                    """
                    update IndexerProxies
                    set Settings = ?, Implementation = ?, ConfigContract = ?, Tags = ?
                    where Id = ?
                    """,
                    (settings, "FlareSolverr", "FlareSolverrSettings", proxy_tags, int(row[0])),
                )
            else:
                connection.execute(
                    """
                    insert into IndexerProxies(Name, Settings, Implementation, ConfigContract, Tags)
                    values(?, ?, ?, ?, ?)
                    """,
                    (PROXY_NAME, settings, "FlareSolverr", "FlareSolverrSettings", proxy_tags),
                )

        for indexer_name in DEFAULT_INDEXERS:
            row = connection.execute(
                "select Id, Tags from Indexers where Name = ? limit 1",
                (indexer_name,),
            ).fetchone()
            if not row:
                continue
            connection.execute(
                "update Indexers set Tags = ? where Id = ?",
                (merge_tag_list(row[1], tag_id, mode != "off"), int(row[0])),
            )

        connection.commit()
    finally:
        connection.close()

    run_cmd("docker", "compose", "up", "-d", "prowlarr")
    wait_for_http("http://localhost:9696/ping", timeout=60)

    inactive_helpers = [service for service in HELPER_SERVICES if service != mode]
    if inactive_helpers:
        run_cmd("docker", "compose", "stop", *inactive_helpers)

    print(f"Switched CF solver mode to {mode}. Backup: {backup}")


def status() -> None:
    proxy_data = api_request("GET", "/api/v1/indexerProxy")
    active_proxy = next((item for item in proxy_data if item.get("name") == PROXY_NAME), None)
    running = set(
        line.strip()
        for line in run_cmd_capture("docker", "compose", "ps", "--services", "--status", "running").stdout.splitlines()
        if line.strip()
    )

    if active_proxy:
        host = next(
            (field.get("value") for field in active_proxy.get("fields", []) if field.get("name") == "host"),
            "",
        )
        print(f"proxy_host\t{host}")
        print(f"proxy_tags\t{json.dumps(active_proxy.get('tags', []))}")
    else:
        print("proxy_host\toff")
        print("proxy_tags\t[]")

    for service in HELPER_SERVICES:
        print(f"{service}\t{'running' if service in running else 'stopped'}")


def api_request(method: str, path: str, payload: dict | None = None) -> dict | list:
    headers = {
        "X-Api-Key": read_api_key(),
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://localhost:9696{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def existing_indexer(name: str) -> dict | None:
    for indexer in api_request("GET", "/api/v1/indexer"):
        if indexer.get("name") == name:
            return api_request("GET", f"/api/v1/indexer/{indexer['id']}")
    return None


def schema_indexer(name: str, tag_id: int) -> dict:
    schemas = api_request("GET", "/api/v1/indexer/schema")
    for schema in schemas:
        if schema.get("name") == name:
            schema["appProfileId"] = 1
            schema["tags"] = [tag_id]
            return schema
    raise RuntimeError(f"Indexer schema not found: {name}")


def test_indexers(mode: str, names: list[str]) -> int:
    switch_solver(mode)
    connection = sqlite3.connect(PROWLARR_DB)
    try:
        tag_id = ensure_tag_id(connection)
    finally:
        connection.close()
    exit_code = 0
    for name in names:
        payload = existing_indexer(name) or schema_indexer(name, tag_id)
        payload["tags"] = [tag_id]
        request = urllib.request.Request(
            "http://localhost:9696/api/v1/indexer/test",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "X-Api-Key": read_api_key(),
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read().decode() or "{}"
                print(f"{name}\t{response.status}\t{body}")
        except urllib.error.HTTPError as error:
            body = error.read().decode().replace("\n", " ")
            print(f"{name}\t{error.code}\t{body}")
            exit_code = 1
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Switch and test Cloudflare solver helpers for Prowlarr.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    switch_parser = subparsers.add_parser("switch")
    switch_parser.add_argument("mode", choices=["flaresolverr", "byparr", "off"])

    subparsers.add_parser("status")

    test_parser = subparsers.add_parser("test")
    test_parser.add_argument("mode", choices=["flaresolverr", "byparr"])
    test_parser.add_argument("names", nargs="*", default=DEFAULT_INDEXERS)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "switch":
        switch_solver(args.mode)
        return 0
    if args.command == "status":
        status()
        return 0
    if args.command == "test":
        return test_indexers(args.mode, args.names)
    return 1


if __name__ == "__main__":
    sys.exit(main())
