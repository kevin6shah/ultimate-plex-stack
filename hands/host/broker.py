from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib import error, request


API_BASE_URL = os.environ["FRIDAY_API_BASE_URL"].rstrip("/")
WORKER_KEY = os.environ["FRIDAY_WORKER_KEY"]
WORKSPACE_ROOT = Path(os.environ.get("FRIDAY_WORKSPACE_ROOT", "/srv/friday-hands/workspaces"))
POLL_INTERVAL_SECONDS = int(os.environ.get("FRIDAY_POLL_INTERVAL_SECONDS", "15"))
WORKER_IMAGE = os.environ.get("FRIDAY_WORKER_IMAGE", "friday-hands-worker:latest")
WORKER_TIMEOUT_SECONDS = int(os.environ.get("FRIDAY_WORKER_TIMEOUT_SECONDS", "1800"))
CONTAINER_ENGINE = os.environ.get("FRIDAY_CONTAINER_ENGINE", "podman")
WORKER_CPUS = os.environ.get("FRIDAY_WORKER_CPUS", "0.50")
WORKER_MEMORY = os.environ.get("FRIDAY_WORKER_MEMORY", "512m")
WORKER_PIDS_LIMIT = os.environ.get("FRIDAY_WORKER_PIDS_LIMIT", "512")
STOP_ON_IDLE = os.environ.get("FRIDAY_STOP_ON_IDLE", "0") == "1"
IDLE_STOP_SECONDS = int(os.environ.get("FRIDAY_IDLE_STOP_SECONDS", "600"))
WORKER_CODE_ROOT = Path(os.environ.get("FRIDAY_WORKER_CODE_ROOT", "/opt/friday-hands"))


def _container_env_var(name: str) -> list[str]:
    return ["-e", f"{name}={os.environ.get(name, '')}"]


class WorkerRunError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.message = message
        self.returncode = returncode


def post_json(path: str, payload: dict) -> dict:
    req = request.Request(
        f"{API_BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-friday-worker-key": WORKER_KEY,
        },
        method="POST",
    )
    with request.urlopen(req, timeout=60) as response:
        data = response.read()
        return json.loads(data.decode("utf-8")) if data else {}


def get_json(path: str) -> dict:
    req = request.Request(
        f"{API_BASE_URL}{path}",
        headers={
            "x-friday-worker-key": WORKER_KEY,
        },
        method="GET",
    )
    with request.urlopen(req, timeout=60) as response:
        data = response.read()
        return json.loads(data.decode("utf-8")) if data else {}


def report_failure(job_id: str, error_message: str, *, interrupted: bool = False, timed_out: bool = False) -> None:
    try:
        post_json(
            "/internal/worker/fail",
            {
                "job_id": job_id,
                "error_message": error_message[:4000],
                "interrupted": interrupted,
                "timed_out": timed_out,
            },
        )
    except Exception as exc:  # pragma: no cover - best-effort reporting on the host
        print(f"worker broker could not report failure for {job_id}: {exc}", flush=True)


def verify_terminal_job_state(job_id: str) -> None:
    try:
        payload = get_json(f"/internal/worker/job-status/{job_id}")
    except Exception as exc:  # pragma: no cover - best-effort host-side verification
        print(f"worker broker could not verify terminal state for {job_id}: {exc}", flush=True)
        return
    status = str(payload.get("status") or "").strip().lower()
    if status in {"completed", "failed", "timed_out", "interrupted", "paused_for_input"}:
        return
    report_failure(job_id, f"worker exited without reporting a terminal state (last status: {status or 'unknown'})")
    print(
        f"worker broker repaired non-terminal exit for {job_id}: last status={status or 'unknown'}",
        flush=True,
    )


def ensure_workspace(job_id: str, *, resume_from_job_id: str | None = None) -> Path:
    workspace = WORKSPACE_ROOT / job_id
    if workspace.exists():
        shutil.rmtree(workspace)
    source_workspace = WORKSPACE_ROOT / resume_from_job_id if resume_from_job_id else None
    if source_workspace is not None and source_workspace.exists():
        shutil.copytree(source_workspace, workspace)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def run_worker(claim: dict) -> None:
    job = claim["job"]
    job_id = job["job_id"]
    workspace = ensure_workspace(job_id, resume_from_job_id=job.get("resume_from_job_id"))
    claim_path = workspace / "claim.json"
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    run_cmd = [
        CONTAINER_ENGINE,
        "run",
        "--rm",
        "--name",
        f"friday-hands-{job_id[:12]}",
        "--cpus",
        WORKER_CPUS,
        "--memory",
        WORKER_MEMORY,
        "--pids-limit",
        WORKER_PIDS_LIMIT,
        "--security-opt",
        "no-new-privileges",
        "--cap-drop=ALL",
        "--read-only",
        "--network",
        "bridge" if CONTAINER_ENGINE == "docker" else "slirp4netns",
        "--tmpfs",
        "/tmp:rw,size=256m,mode=1777",
        "-e",
        f"FRIDAY_API_BASE_URL={API_BASE_URL}",
        "-e",
        f"FRIDAY_WORKER_KEY={WORKER_KEY}",
        "-e",
        "FRIDAY_CLAIM_PATH=/workspace/claim.json",
        "-e",
        f"DEEPSEEK_API_KEY={os.environ['DEEPSEEK_API_KEY']}",
        "-e",
        "HOME=/workspace",
        "--mount",
        f"type=bind,src={workspace},dst=/workspace",
    ]
    host_app_dir = WORKER_CODE_ROOT / "agent" / "app"
    host_runner_path = WORKER_CODE_ROOT / "hands" / "worker" / "runner.py"
    if host_app_dir.is_dir():
        run_cmd.extend(
            [
                "--mount",
                f"type=bind,src={host_app_dir},dst=/opt/friday/app,readonly",
            ]
        )
    if host_runner_path.is_file():
        run_cmd.extend(
            [
                "--mount",
                f"type=bind,src={host_runner_path},dst=/opt/friday/runner.py,readonly",
            ]
        )
    for env_name in (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "BRAVE_SEARCH_API_KEY_PARAM",
        "BRAVE_SEARCH_API_KEY",
        "BROWSER_USE_API_KEY_PARAM",
        "BROWSER_USE_API_KEY",
        "BROWSER_USE_ENABLED",
        "BROWSER_USE_CLOUD_ENABLED",
        "BROWSER_USE_MODEL",
        "BROWSER_USE_CLOUD_MODEL",
        "BROWSER_USE_CLOUD_PROXY_COUNTRY_CODE",
        "BROWSER_USE_STEP_TIMEOUT_SECONDS",
        "BROWSER_USE_MAX_FAILURES",
        "BROWSER_USE_TASK_TIMEOUT_SECONDS",
        "MCP_REGISTRATION_TIMEOUT_SECONDS",
        "FIRECRAWL_MCP_ENABLED",
        "FIRECRAWL_API_KEY_PARAM",
        "FIRECRAWL_API_KEY",
        "SKIPLAGGED_MCP_ENABLED",
        "SKIPLAGGED_MCP_COMMAND",
        "SKIPLAGGED_MCP_ARGS",
        "RESTAURANT_CLI_ENABLED",
        "RESTAURANT_CLI_COMMAND",
        "RESTAURANT_CLI_OT_MODE",
        "RESTAURANT_CLI_TIMEZONE",
        "GOOGLE_MAPS_MCP_ENABLED",
        "GOOGLE_MAPS_API_KEY_PARAM",
        "GOOGLE_MAPS_API_KEY",
        "GOOGLE_MAPS_ENABLED_TOOLS",
        "MAPS_OPENAPI_MCP_ENABLED",
        "MAPS_OPENAPI_SPEC_URL",
        "MAPS_OPENAPI_BASE_URL",
        "MAPS_OPENAPI_HEADERS_PARAM",
        "MAPS_OPENAPI_HEADERS_JSON",
        "MAPS_OPENAPI_AUTH_TOKEN_PARAM",
        "MAPS_OPENAPI_AUTH_TOKEN",
        "RESY_MCP_ENABLED",
        "RESY_API_KEY_PARAM",
        "RESY_API_KEY",
        "RESY_AUTH_TOKEN_PARAM",
        "RESY_AUTH_TOKEN",
        "OPENTABLE_MCP_ENABLED",
        "OPENTABLE_EMAIL_PARAM",
        "OPENTABLE_EMAIL",
        "OPENTABLE_PASSWORD_PARAM",
        "OPENTABLE_PASSWORD",
        "GMAIL_MCP_ENABLED",
        "GMAIL_ACCOUNT_EMAIL_PARAM",
        "GMAIL_ACCOUNT_EMAIL",
        "GMAIL_APP_PASSWORD_PARAM",
        "GMAIL_APP_PASSWORD",
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_ID_PARAM",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_CLIENT_SECRET_PARAM",
        "GMAIL_REFRESH_TOKEN",
        "GMAIL_REFRESH_TOKEN_PARAM",
    ):
        run_cmd.extend(_container_env_var(env_name))
    run_cmd.append(WORKER_IMAGE)
    result = subprocess.run(
        run_cmd,
        check=False,
        timeout=WORKER_TIMEOUT_SECONDS + 120,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if not details:
            details = f"worker container exited with status {result.returncode}"
        raise WorkerRunError(details, returncode=result.returncode)
    verify_terminal_job_state(job_id)


def stop_instance_if_idle() -> None:
    if not STOP_ON_IDLE:
        return
    subprocess.run(["/sbin/shutdown", "-h", "now"], check=False)


def main() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    last_activity = time.time()
    while True:
        claim: dict | None = None
        try:
            claim = post_json("/internal/worker/claim", {})
            if not claim.get("ok"):
                if STOP_ON_IDLE and (time.time() - last_activity) >= IDLE_STOP_SECONDS:
                    stop_instance_if_idle()
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            last_activity = time.time()
            run_worker(claim)
            last_activity = time.time()
        except subprocess.TimeoutExpired:
            job = claim.get("job", {})
            if job.get("job_id"):
                report_failure(job["job_id"], "worker timed out", timed_out=True)
        except WorkerRunError as exc:
            job = (claim or {}).get("job", {})
            if job.get("job_id"):
                interrupted = exc.returncode in (130, 137, 143)
                report_failure(job["job_id"], exc.message if not interrupted else "worker interrupted", interrupted=interrupted)
            print(f"worker broker job failed: {exc.message}", flush=True)
        except error.HTTPError as exc:
            print(f"worker broker http error: {exc}", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception as exc:
            print(f"worker broker error: {exc}", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
