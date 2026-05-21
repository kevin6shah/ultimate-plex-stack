from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobSource(str, Enum):
    TELEGRAM = "telegram"
    SIRI = "siri"


class TaskClass(str, Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class JobStatus(str, Enum):
    QUEUED = "queued"
    WAITING_WORKER = "waiting_worker"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED_FOR_INPUT = "paused_for_input"
    CHECKPOINTED = "checkpointed"
    PAUSED_BUDGET = "paused_budget"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class ThreadTurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ControlCommand(str, Enum):
    STOP = "stop"
    NUDGE = "nudge"


class SecretKind(str, Enum):
    COOKIE_JAR = "cookie_jar"
    BEARER_TOKEN = "bearer_token"
    SESSION_STORAGE = "session_storage"
    LOCAL_STORAGE = "local_storage"


class AttachmentRef(BaseModel):
    object_key: str
    file_name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    source: str = "telegram"


class AgentJob(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    source: JobSource
    query: str
    task_class: TaskClass = TaskClass.LIGHT
    status: JobStatus = JobStatus.QUEUED
    chat_id: Optional[str] = None
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    long_task: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    resume_from_job_id: Optional[str] = None
    latest_checkpoint_seq: int = 0
    latest_checkpoint_summary: str = ""
    last_heartbeat_at: Optional[str] = None
    current_step: str = ""
    result_preview: str = ""
    error_message: str = ""
    output_files: list[str] = Field(default_factory=list)
    artifact_keys: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    last_status_sent_at: Optional[str] = None
    last_status_sent_text: str = ""
    last_heartbeat_fingerprint: str = ""
    heartbeat_repeat_count: int = 0
    heartbeat_repeat_since: Optional[str] = None
    last_progress_at: Optional[str] = None
    loop_stop_requested_at: Optional[str] = None


class AgentResult(BaseModel):
    text: str
    cost_usd: str = "0"
    budget_blocked: bool = False


class AgentConfig(BaseModel):
    agent_name: str = "Friday"
    context_max_turns: int = 10
    context_summary_max_chars: int = 2400
    conversation_ttl_hours: int = 48
    status_update_interval_seconds: int = 300
    daily_budget_usd: Optional[float] = None
    monthly_budget_usd: Optional[float] = 12.0
    persona_summary: str = ""
    system_prompt_suffix: str = ""


class ThreadTurn(BaseModel):
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    role: ThreadTurnRole
    text: str
    created_at: str = Field(default_factory=utc_now_iso)
    task_class: TaskClass = TaskClass.LIGHT


class SecureCredentialRecord(BaseModel):
    credential_id: str = Field(default_factory=lambda: str(uuid4()))
    label: str
    site: str
    secret_kind: SecretKind = SecretKind.COOKIE_JAR
    parameter_name: str
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: Optional[str] = None
    notes: str = ""


class ControlSignal(BaseModel):
    command: ControlCommand
    note: str = ""
    created_at: str = Field(default_factory=utc_now_iso)


class CheckpointPayload(BaseModel):
    summary: str
    current_step: str = ""
    tool_outputs: list[str] = Field(default_factory=list)
    workspace_files: list[str] = Field(default_factory=list)
    artifact_keys: list[str] = Field(default_factory=list)
    browser_state: dict[str, Any] = Field(default_factory=dict)
    resume_instructions: str = ""
    spend_usd: str = "0"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerClaimResponse(BaseModel):
    ok: bool
    job: Optional[AgentJob] = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    context_summary: str = ""
    recent_turns: list[ThreadTurn] = Field(default_factory=list)
    durable_memories: list[str] = Field(default_factory=list)
    resume_checkpoint: Optional[CheckpointPayload] = None
    config: AgentConfig = Field(default_factory=AgentConfig)
    api_base_url: str = ""
    artifacts_prefix: str = ""


class WorkerHeartbeat(BaseModel):
    job_id: str
    current_step: str = ""
    summary: str = ""
    notify: bool = False


class WorkerCheckpointRequest(BaseModel):
    job_id: str
    checkpoint: CheckpointPayload


class WorkerPauseRequest(BaseModel):
    job_id: str
    question: str
    details: str = ""
    checkpoint: CheckpointPayload


class ArtifactUploadRequest(BaseModel):
    job_id: str
    file_name: str
    content_type: str = "application/octet-stream"


class WorkerCompleteRequest(BaseModel):
    job_id: str
    result_text: str
    output_files: list[str] = Field(default_factory=list)
    artifact_keys: list[str] = Field(default_factory=list)


class WorkerFailureRequest(BaseModel):
    job_id: str
    error_message: str
    interrupted: bool = False
    timed_out: bool = False


class SaveConfigRequest(BaseModel):
    config: AgentConfig


class CreateCredentialRequest(BaseModel):
    label: str
    site: str
    secret_value: str
    secret_kind: SecretKind = SecretKind.COOKIE_JAR
    expires_at: Optional[str] = None
    notes: str = ""


class JobControlRequest(BaseModel):
    command: ControlCommand
    note: str = ""
