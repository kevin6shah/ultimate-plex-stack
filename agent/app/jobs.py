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
    PASSWORD = "password"
    OAUTH_REFRESH_TOKEN = "oauth_refresh_token"


class PauseReason(str, Enum):
    IDENTITY_SELECTION_REQUIRED = "identity_selection_required"
    VERIFICATION_WAITING_EMAIL = "verification_waiting_email"
    CAPTCHA_REQUIRED = "captcha_required"
    SMS_OTP_REQUIRED = "sms_otp_required"
    PAYMENT_BLOCKED = "payment_blocked"
    NON_ZERO_CHECKOUT_BLOCKED = "non_zero_checkout_blocked"
    CARD_ENTRY_REQUIRED = "card_entry_required"
    CANCEL_PENDING = "cancel_pending"
    CANCEL_COMPLETED = "cancel_completed"
    MANUAL_CANCELLATION_REQUIRED = "manual_cancellation_required"


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


class IdentityRecord(BaseModel):
    identity_id: str = Field(default_factory=lambda: str(uuid4()))
    label: str
    email: str
    provider: str = "gmail"
    category: str = "general"
    site_scope: str = ""
    is_default: bool = False
    notes: str = ""
    status: str = "active"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class IdentitySecretPointer(BaseModel):
    identity_id: str
    parameter_name: str
    secret_kind: SecretKind = SecretKind.PASSWORD
    updated_at: str = Field(default_factory=utc_now_iso)


class BrowserSessionRecord(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    identity_id: str = ""
    site_scope: str
    session_s3_key: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    fingerprint_seed: str
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    status: str = "active"
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class AutomationPolicyRecord(BaseModel):
    policy_id: str = Field(default_factory=lambda: str(uuid4()))
    label: str
    site_scope: str = ""
    category: str = "general"
    allow_account_creation: bool = False
    allow_login_reuse: bool = False
    allow_zero_dollar_booking: bool = False
    pause_on_sms_or_captcha: bool = True
    default_identity_id: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class PaymentProfileRecord(BaseModel):
    payment_profile_id: str = Field(default_factory=lambda: str(uuid4()))
    label: str
    provider: str = "privacy"
    masked_last4: str = ""
    notes: str = ""
    limit_cents: int = 0
    active: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class MailboxWatchState(BaseModel):
    mailbox_email: str
    history_id: str = ""
    expiration: str = ""
    topic_name: str = ""
    watch_status: str = "unknown"
    last_watch_renewed_at: str = ""
    last_push_received_at: str = ""
    last_oauth_tested_at: str = ""
    last_oauth_error: str = ""
    updated_at: str = Field(default_factory=utc_now_iso)


class MailboxVerificationWaitRecord(BaseModel):
    wait_id: str = Field(default_factory=lambda: str(uuid4()))
    workflow_id: str
    run_id: str = ""
    job_id: str
    site_key: str
    identity_id: str = ""
    expected_sender_patterns: list[str] = Field(default_factory=list)
    expected_subject_patterns: list[str] = Field(default_factory=list)
    otp_regex: list[str] = Field(default_factory=list)
    created_after: str = Field(default_factory=utc_now_iso)
    expires_at: str = ""
    status: str = "waiting"
    matched_message_id: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class BookingRecord(BaseModel):
    booking_id: str = Field(default_factory=lambda: str(uuid4()))
    job_id: str
    site_key: str
    identity_id: str = ""
    session_id: str = ""
    external_reference: str = ""
    venue_name: str = ""
    booking_time: str = ""
    booking_total_cents: int = 0
    currency: str = "USD"
    status: str = "created"
    can_cancel: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class DashboardSessionRecord(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    telegram_user_id: str
    telegram_auth_date: str
    first_name: str = ""
    username: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: str


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
