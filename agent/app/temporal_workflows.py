from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from .temporal_control_activities import (
        finalize_heavy_job_completed,
        finalize_heavy_job_failed,
        finalize_heavy_job_paused,
        finalize_heavy_job_stop,
        prepare_heavy_job_claim,
        prepare_heavy_job_followup_claim,
        prepare_heavy_job_resume_claim,
        prepare_heavy_job_strategy_retry_claim,
        renew_gmail_watch_activity,
    )


@workflow.defn
class FridayHeavyJobWorkflow:
    def __init__(self) -> None:
        self.stop_requested = False
        self.resume_reply: str | None = None
        self.pending_followup: str | None = None
        self._running_activity: asyncio.Task | None = None

    @workflow.signal
    def stop(self, note: str = "") -> None:
        self.stop_requested = True
        if self._running_activity is not None:
            self._running_activity.cancel()

    @workflow.signal
    def answer(self, text: str) -> None:
        normalized = (text or "").strip()
        if normalized:
            self.resume_reply = normalized

    @workflow.signal
    def update_constraints(self, text: str) -> None:
        normalized = (text or "").strip()
        if not normalized:
            return
        if self.pending_followup:
            self.pending_followup = f"{self.pending_followup}\n{normalized}"
        else:
            self.pending_followup = normalized
        if self._running_activity is not None:
            self._running_activity.cancel()

    @workflow.signal
    def submit_verification_code(self, code: str) -> None:
        normalized = (code or "").strip()
        if normalized:
            self.resume_reply = f"verification code: {normalized}"

    @workflow.run
    async def run(self, payload: dict) -> None:
        job_id = str(payload["job_id"])
        heavy_task_queue = str(payload["heavy_task_queue"])
        heavy_activity_max_attempts = max(1, int(payload.get("heavy_activity_max_attempts") or 1))
        control_retry = RetryPolicy(maximum_attempts=1)
        claim = await workflow.execute_activity(
            prepare_heavy_job_claim,
            job_id,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=control_retry,
        )
        while True:
            if self.stop_requested:
                await workflow.execute_activity(
                    finalize_heavy_job_stop,
                    job_id,
                    start_to_close_timeout=timedelta(minutes=2),
                )
                return
            try:
                self._running_activity = asyncio.create_task(
                    workflow.execute_activity(
                        "execute_heavy_job_activity",
                        claim,
                        task_queue=heavy_task_queue,
                        heartbeat_timeout=timedelta(seconds=30),
                        start_to_close_timeout=timedelta(minutes=90),
                        schedule_to_close_timeout=timedelta(minutes=90),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                )
                result = await self._running_activity
            except asyncio.CancelledError:
                if self.stop_requested:
                    await workflow.execute_activity(
                        finalize_heavy_job_stop,
                        job_id,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=control_retry,
                    )
                    return
                if self.pending_followup:
                    followup = self.pending_followup
                    self.pending_followup = None
                    claim = await workflow.execute_activity(
                        prepare_heavy_job_followup_claim,
                        args=[job_id, followup],
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=control_retry,
                    )
                    continue
                await workflow.execute_activity(
                    finalize_heavy_job_stop,
                    job_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                return
            except ActivityError as exc:
                if self.stop_requested:
                    await workflow.execute_activity(
                        finalize_heavy_job_stop,
                        job_id,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=control_retry,
                    )
                    return
                await workflow.execute_activity(
                    finalize_heavy_job_failed,
                    args=[
                        job_id,
                        str(exc) or "The worker activity did not finish cleanly.",
                        False,
                        False,
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                return
            finally:
                self._running_activity = None

            kind = str(result.get("kind") or "").strip().lower()
            if kind == "completed":
                await workflow.execute_activity(
                    finalize_heavy_job_completed,
                    args=[
                        job_id,
                        str(result.get("result_text") or ""),
                        list(result.get("output_files") or []),
                        list(result.get("artifact_keys") or []),
                    ],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=control_retry,
                )
                return
            if kind == "paused":
                await workflow.execute_activity(
                    finalize_heavy_job_paused,
                    args=[
                        job_id,
                        str(result.get("question") or ""),
                        str(result.get("details") or ""),
                        dict(result.get("checkpoint") or {}),
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                await workflow.wait_condition(lambda: self.stop_requested or bool(self.resume_reply))
                if self.stop_requested:
                    await workflow.execute_activity(
                        finalize_heavy_job_stop,
                        job_id,
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=control_retry,
                    )
                    return
                reply = self.resume_reply or ""
                self.resume_reply = None
                claim = await workflow.execute_activity(
                    prepare_heavy_job_resume_claim,
                    args=[job_id, reply],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                continue
            if kind == "retry_strategy":
                claim = await workflow.execute_activity(
                    prepare_heavy_job_strategy_retry_claim,
                    args=[
                        job_id,
                        dict(result.get("strategy_state") or {}),
                        str(result.get("error_message") or ""),
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                continue
            if kind == "failed" and (self.stop_requested or bool(result.get("interrupted"))):
                await workflow.execute_activity(
                    finalize_heavy_job_stop,
                    job_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=control_retry,
                )
                return
            await workflow.execute_activity(
                finalize_heavy_job_failed,
                args=[
                    job_id,
                    str(result.get("error_message") or "The task failed before it returned a valid result."),
                    bool(result.get("interrupted")),
                    bool(result.get("timed_out")),
                ],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=control_retry,
            )
            return


@workflow.defn
class FridayGmailWatchRenewalWorkflow:
    @workflow.run
    async def run(self, payload: dict | None = None) -> None:
        config = payload or {}
        renewal_days = max(1, int(config.get("renewal_days") or 5))
        while True:
            await workflow.execute_activity(
                renew_gmail_watch_activity,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            await workflow.sleep(timedelta(days=renewal_days))
