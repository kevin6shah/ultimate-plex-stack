from __future__ import annotations

import asyncio
from typing import Any, Optional

from .settings import Settings


_client_lock = asyncio.Lock()
_cached_client: Optional[Any] = None
_cached_target: tuple[str, str, str] | None = None


def temporal_backend_enabled(settings: Settings) -> bool:
    if settings.execution_backend == "temporal":
        return bool(settings.temporal_host)
    return settings.temporal_enabled and bool(settings.temporal_host)


def heavy_workflow_id(settings: Settings, job_id: str) -> str:
    prefix = settings.temporal_workflow_id_prefix or "friday-heavy-job"
    return f"{prefix}-{job_id}"


async def get_temporal_client(settings: Settings) -> Any:
    global _cached_client, _cached_target
    target = (settings.temporal_host, settings.temporal_namespace, settings.temporal_client_identity)
    async with _client_lock:
        if _cached_client is not None and _cached_target == target:
            return _cached_client
        from temporalio.client import Client

        _cached_client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
            identity=settings.temporal_client_identity,
        )
        _cached_target = target
        return _cached_client
