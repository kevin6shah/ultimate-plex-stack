from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .budget import month_key, today_key, ttl_epoch
from .context_memory import build_thread_summary
from .jobs import AgentConfig, AgentJob, CheckpointPayload, ControlCommand, ControlSignal, JobStatus, TaskClass, ThreadTurn, ThreadTurnRole
from .settings import Settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StateStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.state_table)

    def put_session(self, *, channel: str, user_id: str, metadata: dict[str, str]) -> None:
        expires_at = int((utc_now() + timedelta(seconds=self.settings.session_ttl_seconds)).timestamp())
        self.table.put_item(
            Item={
                "PK": f"SESSION#{channel}#{user_id}",
                "SK": "META",
                "ttl": expires_at,
                "metadata": metadata,
            }
        )

    def _thread_pk(self, *, channel: str, user_id: str, conversation_id: str) -> str:
        return f"THREAD#{channel}#{user_id}#{conversation_id}"

    def _context_pk(self, *, channel: str, user_id: str, conversation_id: str) -> str:
        return f"CTX#{channel}#{user_id}#{conversation_id}"

    def get_config(self) -> AgentConfig:
        item = self.table.get_item(Key={"PK": "CONFIG", "SK": "AGENT"}).get("Item")
        if not item:
            return AgentConfig()
        return AgentConfig.model_validate(item.get("config", {}))

    def save_config(self, config: AgentConfig) -> AgentConfig:
        dynamo_config = json.loads(json.dumps(config.model_dump()), parse_float=Decimal)
        self.table.put_item(
            Item={
                "PK": "CONFIG",
                "SK": "AGENT",
                "config": dynamo_config,
                "updated_at": utc_now().isoformat(),
            }
        )
        return config

    def _conversation_ttl_seconds(self, config: Optional[AgentConfig] = None) -> int:
        effective = config or self.get_config()
        return max(1, int(effective.conversation_ttl_hours)) * 60 * 60

    def _context_summary_chars(self, config: Optional[AgentConfig] = None) -> int:
        effective = config or self.get_config()
        return max(400, int(effective.context_summary_max_chars))

    def _context_turn_limit(self, config: Optional[AgentConfig] = None) -> int:
        effective = config or self.get_config()
        return max(2, int(effective.context_max_turns))

    def record_turn(
        self,
        *,
        channel: str,
        user_id: str,
        conversation_id: str,
        role: ThreadTurnRole,
        text: str,
        task_class: TaskClass = TaskClass.LIGHT,
        config: Optional[AgentConfig] = None,
    ) -> ThreadTurn:
        normalized = text.strip()
        if not normalized:
            raise ValueError("turn text cannot be empty")
        effective_config = config or self.get_config()
        expires_at = int((utc_now() + timedelta(seconds=self._conversation_ttl_seconds(effective_config))).timestamp())
        turn = ThreadTurn(role=role, text=normalized, task_class=task_class)
        thread_pk = self._thread_pk(channel=channel, user_id=user_id, conversation_id=conversation_id)
        self.table.put_item(
            Item={
                "PK": thread_pk,
                "SK": f"TURN#{turn.created_at}#{turn.turn_id}",
                "turn_id": turn.turn_id,
                "role": turn.role.value,
                "text": turn.text,
                "created_at": turn.created_at,
                "task_class": turn.task_class.value,
                "ttl": expires_at,
            }
        )
        turns = self.get_recent_turns(
            channel=channel,
            user_id=user_id,
            conversation_id=conversation_id,
            limit=self._context_turn_limit(effective_config),
        )
        summary = build_thread_summary(turns, max_chars=self._context_summary_chars(effective_config))
        self.table.put_item(
            Item={
                "PK": self._context_pk(channel=channel, user_id=user_id, conversation_id=conversation_id),
                "SK": "LATEST",
                "ttl": expires_at,
                "updated_at": utc_now().isoformat(),
                "summary": summary,
                "task_class": task_class.value,
                "last_role": role.value,
                "last_text": normalized[:1000],
                "turn_count_hint": len(turns),
            }
        )
        return turn

    def get_recent_turns(self, *, channel: str, user_id: str, conversation_id: str, limit: int = 10) -> list[ThreadTurn]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(self._thread_pk(channel=channel, user_id=user_id, conversation_id=conversation_id))
            & Key("SK").begins_with("TURN#"),
            Limit=max(1, limit),
            ScanIndexForward=False,
        )
        items = response.get("Items", [])
        turns = [
            ThreadTurn.model_validate(
                {
                    "turn_id": item.get("turn_id"),
                    "role": item.get("role"),
                    "text": item.get("text", ""),
                    "created_at": item.get("created_at"),
                    "task_class": item.get("task_class", TaskClass.LIGHT.value),
                }
            )
            for item in items
        ]
        turns.reverse()
        return turns

    def get_context_bundle(
        self,
        *,
        channel: str,
        user_id: str,
        conversation_id: str,
    ) -> tuple[str, list[ThreadTurn], AgentConfig]:
        config = self.get_config()
        summary = self.get_context_summary(channel=channel, user_id=user_id, conversation_id=conversation_id)
        turns = self.get_recent_turns(
            channel=channel,
            user_id=user_id,
            conversation_id=conversation_id,
            limit=self._context_turn_limit(config),
        )
        return summary, turns, config

    def put_context(
        self,
        *,
        channel: str,
        user_id: str,
        conversation_id: str,
        query: str,
        response_text: str = "",
        task_class: TaskClass = TaskClass.LIGHT,
    ) -> None:
        if query.strip():
            self.record_turn(
                channel=channel,
                user_id=user_id,
                conversation_id=conversation_id,
                role=ThreadTurnRole.USER,
                text=query,
                task_class=task_class,
            )
        if response_text.strip():
            self.record_turn(
                channel=channel,
                user_id=user_id,
                conversation_id=conversation_id,
                role=ThreadTurnRole.ASSISTANT,
                text=response_text,
                task_class=task_class,
            )

    def get_context_summary(self, *, channel: str, user_id: str, conversation_id: str) -> str:
        response = self.table.get_item(Key={"PK": self._context_pk(channel=channel, user_id=user_id, conversation_id=conversation_id), "SK": "LATEST"})
        item = response.get("Item")
        if not item:
            return ""
        return str(item.get("summary", ""))

    def remember_fact(self, *, owner: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        fact_id = sha256(normalized.encode("utf-8")).hexdigest()[:24]
        self.table.put_item(
            Item={
                "PK": f"MEMORY#{owner}",
                "SK": f"FACT#{fact_id}",
                "fact": normalized,
                "created_at": utc_now().isoformat(),
            }
        )

    def list_memories(self, *, owner: str, limit: int = 20) -> list[str]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"MEMORY#{owner}") & Key("SK").begins_with("FACT#"),
            Limit=limit,
            ScanIndexForward=False,
        )
        return [str(item.get("fact", "")) for item in response.get("Items", []) if item.get("fact")]

    def forget_fact(self, *, owner: str, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        fact_id = sha256(normalized.encode("utf-8")).hexdigest()[:24]
        self.table.delete_item(Key={"PK": f"MEMORY#{owner}", "SK": f"FACT#{fact_id}"})

    def clear_thread(self, *, channel: str, user_id: str, conversation_id: str) -> None:
        thread_pk = self._thread_pk(channel=channel, user_id=user_id, conversation_id=conversation_id)
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(thread_pk),
        )
        with self.table.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            batch.delete_item(Key={"PK": self._context_pk(channel=channel, user_id=user_id, conversation_id=conversation_id), "SK": "LATEST"})

    def create_job(self, job: AgentJob) -> AgentJob:
        now = utc_now().isoformat()
        item = {
            "PK": f"JOB#{job.job_id}",
            "SK": "META",
            "job_id": job.job_id,
            "status": job.status.value,
            "task_class": job.task_class.value,
            "source": job.source.value,
            "query": job.query,
            "chat_id": job.chat_id,
            "user_id": job.user_id,
            "conversation_id": job.conversation_id,
            "long_task": job.long_task,
            "created_at": job.created_at,
            "updated_at": now,
            "attachments": [attachment.model_dump() for attachment in job.attachments],
            "resume_from_job_id": job.resume_from_job_id,
            "latest_checkpoint_seq": job.latest_checkpoint_seq,
            "latest_checkpoint_summary": job.latest_checkpoint_summary,
            "current_step": job.current_step,
            "result_preview": job.result_preview,
            "error_message": job.error_message,
            "output_files": job.output_files,
            "artifact_keys": job.artifact_keys,
            "metadata": job.metadata,
            "GSI1PK": f"JOB_STATUS#{job.status.value}",
            "GSI1SK": f"{job.created_at}#{job.job_id}",
        }
        if job.task_class == TaskClass.HEAVY:
            item["ttl"] = ttl_epoch(self.settings.interrupted_job_ttl_days)
        self.table.put_item(Item=item)
        return job

    def get_job(self, job_id: str) -> Optional[AgentJob]:
        response = self.table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "META"})
        item = response.get("Item")
        if not item:
            return None
        return self._job_from_item(item)

    def update_job_status(
        self,
        job_id: str,
        *,
        status: JobStatus,
        current_step: str = "",
        result_preview: str = "",
        error_message: str = "",
        output_files: Optional[list[str]] = None,
        artifact_keys: Optional[list[str]] = None,
    ) -> None:
        ttl = None
        if status == JobStatus.COMPLETED:
            ttl = ttl_epoch(self.settings.completed_job_ttl_days)
        elif status in (JobStatus.INTERRUPTED, JobStatus.CHECKPOINTED, JobStatus.PAUSED_FOR_INPUT, JobStatus.TIMED_OUT, JobStatus.FAILED):
            ttl = ttl_epoch(self.settings.interrupted_job_ttl_days)

        expression = [
            "SET #status = :status",
            "updated_at = :updated_at",
            "GSI1PK = :gsi1pk",
        ]
        values: dict[str, Any] = {
            ":status": status.value,
            ":updated_at": utc_now().isoformat(),
            ":gsi1pk": f"JOB_STATUS#{status.value}",
        }
        names = {"#status": "status"}
        if current_step:
            expression.append("current_step = :current_step")
            values[":current_step"] = current_step[:500]
        if result_preview:
            expression.append("result_preview = :result_preview")
            values[":result_preview"] = result_preview[:2000]
        if error_message:
            expression.append("error_message = :error_message")
            values[":error_message"] = error_message[:2000]
        if output_files is not None:
            expression.append("output_files = :output_files")
            values[":output_files"] = output_files[:50]
        if artifact_keys is not None:
            expression.append("artifact_keys = :artifact_keys")
            values[":artifact_keys"] = artifact_keys[:50]
        if ttl is not None:
            expression.append("#ttl = :ttl")
            names["#ttl"] = "ttl"
            values[":ttl"] = ttl
        self.table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression=", ".join(expression),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def update_job_heartbeat(self, job_id: str, *, current_step: str = "", summary: str = "") -> None:
        self.table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET last_heartbeat_at = :heartbeat, updated_at = :updated_at, current_step = :current_step, latest_checkpoint_summary = :summary",
            ExpressionAttributeValues={
                ":heartbeat": utc_now().isoformat(),
                ":updated_at": utc_now().isoformat(),
                ":current_step": current_step[:500],
                ":summary": summary[:2000],
            },
        )

    def should_send_status_update(self, job_id: str, *, interval_seconds: int) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        if not job.last_status_sent_at:
            return True
        try:
            last_sent = datetime.fromisoformat(job.last_status_sent_at)
        except ValueError:
            return True
        return (utc_now() - last_sent).total_seconds() >= interval_seconds

    def mark_status_update_sent(self, job_id: str) -> None:
        timestamp = utc_now().isoformat()
        self.table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET last_status_sent_at = :sent, updated_at = :updated_at",
            ExpressionAttributeValues={
                ":sent": timestamp,
                ":updated_at": timestamp,
            },
        )

    def save_checkpoint(self, job_id: str, checkpoint: CheckpointPayload) -> int:
        meta = self.table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "META"}).get("Item", {})
        next_seq = int(meta.get("latest_checkpoint_seq", 0)) + 1
        self.table.put_item(
            Item={
                "PK": f"JOB#{job_id}",
                "SK": f"CHECKPOINT#{next_seq:06d}",
                "checkpoint_seq": next_seq,
                "created_at": utc_now().isoformat(),
                "payload": checkpoint.model_dump(),
                "ttl": ttl_epoch(self.settings.interrupted_job_ttl_days),
            }
        )
        self.table.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression="SET latest_checkpoint_seq = :seq, latest_checkpoint_summary = :summary, current_step = :step, updated_at = :updated_at, #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":seq": next_seq,
                ":summary": checkpoint.summary[:2000],
                ":step": checkpoint.current_step[:500],
                ":updated_at": utc_now().isoformat(),
                ":ttl": ttl_epoch(self.settings.interrupted_job_ttl_days),
            },
        )
        return next_seq

    def get_latest_checkpoint(self, job_id: str) -> Optional[CheckpointPayload]:
        response = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"JOB#{job_id}") & Key("SK").begins_with("CHECKPOINT#"),
            Limit=1,
            ScanIndexForward=False,
        )
        items = response.get("Items", [])
        if not items:
            return None
        return CheckpointPayload.model_validate(items[0]["payload"])

    def record_approval(self, job_id: str, *, approved: bool, note: str = "") -> None:
        self.table.put_item(
            Item={
                "PK": f"JOB#{job_id}",
                "SK": "APPROVAL#LATEST",
                "approved": approved,
                "note": note[:1000],
                "updated_at": utc_now().isoformat(),
                "ttl": ttl_epoch(self.settings.interrupted_job_ttl_days),
            }
        )

    def record_control_signal(self, job_id: str, *, command: ControlCommand, note: str = "") -> ControlSignal:
        signal = ControlSignal(command=command, note=note[:1000])
        self.table.put_item(
            Item={
                "PK": f"JOB#{job_id}",
                "SK": "CONTROL#LATEST",
                "command": signal.command.value,
                "note": signal.note,
                "created_at": signal.created_at,
                "ttl": ttl_epoch(self.settings.interrupted_job_ttl_days),
            }
        )
        return signal

    def get_latest_control_signal(self, job_id: str) -> Optional[ControlSignal]:
        item = self.table.get_item(Key={"PK": f"JOB#{job_id}", "SK": "CONTROL#LATEST"}).get("Item")
        if not item:
            return None
        return ControlSignal.model_validate(
            {
                "command": item.get("command"),
                "note": item.get("note", ""),
                "created_at": item.get("created_at"),
            }
        )

    def get_latest_job_for_user(self, *, source: str, user_id: str, statuses: tuple[JobStatus, ...]) -> Optional[AgentJob]:
        response = self.table.scan(
            FilterExpression=Attr("user_id").eq(user_id)
            & Attr("source").eq(source)
            & Attr("SK").eq("META")
            & Attr("status").is_in([status.value for status in statuses]),
        )
        items = sorted(response.get("Items", []), key=lambda item: str(item.get("updated_at", "")), reverse=True)
        if not items:
            return None
        return self._job_from_item(items[0])

    def delete_job(self, job_id: str) -> None:
        response = self.table.query(KeyConditionExpression=Key("PK").eq(f"JOB#{job_id}"))
        with self.table.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        job_prefix = f"jobs/{job_id}/"
        continuation = None
        while True:
            params: dict[str, Any] = {"Bucket": self.settings.artifacts_bucket, "Prefix": job_prefix}
            if continuation:
                params["ContinuationToken"] = continuation
            result = self.settings.s3.list_objects_v2(**params)
            contents = result.get("Contents", [])
            if contents:
                self.settings.s3.delete_objects(
                    Bucket=self.settings.artifacts_bucket,
                    Delete={"Objects": [{"Key": item["Key"]} for item in contents]},
                )
            if not result.get("IsTruncated"):
                break
            continuation = result.get("NextContinuationToken")

    def list_jobs(self, *, statuses: tuple[JobStatus, ...], limit: int = 20) -> list[AgentJob]:
        response = self.table.scan(
            FilterExpression=Attr("SK").eq("META") & Attr("status").is_in([status.value for status in statuses]),
        )
        items = sorted(response.get("Items", []), key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return [self._job_from_item(item) for item in items[:limit]]

    def list_jobs_for_user(self, *, source: str, user_id: str, statuses: tuple[JobStatus, ...], limit: int = 20) -> list[AgentJob]:
        response = self.table.scan(
            FilterExpression=Attr("SK").eq("META")
            & Attr("source").eq(source)
            & Attr("user_id").eq(user_id)
            & Attr("status").is_in([status.value for status in statuses]),
        )
        items = sorted(response.get("Items", []), key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return [self._job_from_item(item) for item in items[:limit]]

    def list_recent_contexts(self, limit: int = 20) -> list[dict[str, Any]]:
        response = self.table.scan(
            FilterExpression=Attr("SK").eq("LATEST") & Attr("PK").begins_with("CTX#"),
        )
        items = sorted(response.get("Items", []), key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return [
            {
                "pk": item.get("PK"),
                "summary": item.get("summary", ""),
                "updated_at": item.get("updated_at", ""),
                "task_class": item.get("task_class", ""),
            }
            for item in items[:limit]
        ]

    def claim_next_heavy_job(self) -> Optional[AgentJob]:
        response = self.table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"JOB_STATUS#{JobStatus.QUEUED.value}"),
            Limit=10,
            ScanIndexForward=True,
        )
        for item in response.get("Items", []):
            if item.get("task_class") != TaskClass.HEAVY.value:
                continue
            job_id = str(item["job_id"])
            try:
                self.table.update_item(
                    Key={"PK": f"JOB#{job_id}", "SK": "META"},
                    UpdateExpression="SET #status = :status, updated_at = :updated_at, GSI1PK = :gsi1pk",
                    ConditionExpression="#status = :expected",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":status": JobStatus.RUNNING.value,
                        ":expected": JobStatus.QUEUED.value,
                        ":updated_at": utc_now().isoformat(),
                        ":gsi1pk": f"JOB_STATUS#{JobStatus.RUNNING.value}",
                    },
                )
                fresh = self.get_job(job_id)
                if fresh is not None:
                    return fresh
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        return None

    def claim_job(self, job_id: str) -> Optional[AgentJob]:
        current = self.get_job(job_id)
        if current is None or current.task_class != TaskClass.HEAVY or current.status != JobStatus.QUEUED:
            return None
        try:
            self.table.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression="SET #status = :status, updated_at = :updated_at, GSI1PK = :gsi1pk",
                ConditionExpression="#status = :expected",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": JobStatus.RUNNING.value,
                    ":expected": JobStatus.QUEUED.value,
                    ":updated_at": utc_now().isoformat(),
                    ":gsi1pk": f"JOB_STATUS#{JobStatus.RUNNING.value}",
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return None
        return self.get_job(job_id)

    def spend_totals(self) -> tuple[Decimal, Decimal]:
        daily = self._get_spend(f"SPEND#DAY#{today_key()}")
        monthly = self._get_spend(f"SPEND#MONTH#{month_key()}")
        return daily, monthly

    def add_spend(self, amount: Decimal) -> None:
        if amount <= 0:
            return
        ttl = ttl_epoch(self.settings.spend_ttl_days)
        for key in (f"SPEND#DAY#{today_key()}", f"SPEND#MONTH#{month_key()}"):
            self.table.update_item(
                Key={"PK": key, "SK": "MODEL"},
                UpdateExpression="ADD usd :amount SET #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":amount": amount, ":ttl": ttl},
            )

    def budget_available(self) -> bool:
        config = self.get_config()
        daily_budget = Decimal(str(config.daily_budget_usd if config.daily_budget_usd is not None else self.settings.daily_budget_usd))
        monthly_budget = Decimal(str(config.monthly_budget_usd if config.monthly_budget_usd is not None else self.settings.monthly_budget_usd))
        daily, monthly = self.spend_totals()
        return daily < daily_budget and monthly < monthly_budget

    def _get_spend(self, key: str) -> Decimal:
        response = self.table.get_item(Key={"PK": key, "SK": "MODEL"})
        item = response.get("Item")
        if not item:
            return Decimal("0")
        return Decimal(str(item.get("usd", "0")))

    def _job_from_item(self, item: dict[str, Any]) -> AgentJob:
        return AgentJob.model_validate(
            {
                "job_id": item.get("job_id"),
                "source": item.get("source"),
                "query": item.get("query", ""),
                "task_class": item.get("task_class", TaskClass.LIGHT.value),
                "status": item.get("status", JobStatus.QUEUED.value),
                "chat_id": item.get("chat_id"),
                "user_id": item.get("user_id"),
                "conversation_id": item.get("conversation_id"),
                "long_task": item.get("long_task", False),
                "created_at": item.get("created_at"),
                "attachments": item.get("attachments", []),
                "resume_from_job_id": item.get("resume_from_job_id"),
                "latest_checkpoint_seq": item.get("latest_checkpoint_seq", 0),
                "latest_checkpoint_summary": item.get("latest_checkpoint_summary", ""),
                "last_heartbeat_at": item.get("last_heartbeat_at"),
                "current_step": item.get("current_step", ""),
                "result_preview": item.get("result_preview", ""),
                "error_message": item.get("error_message", ""),
                "output_files": item.get("output_files", []),
                "artifact_keys": item.get("artifact_keys", []),
                "metadata": item.get("metadata", {}),
            }
        )
