import asyncio
import sys
import types
from contextlib import nullcontext


fake_temporalio = types.ModuleType("temporalio")
fake_workflow = types.SimpleNamespace(
    defn=lambda cls: cls,
    run=lambda fn: fn,
    signal=lambda fn: fn,
    unsafe=types.SimpleNamespace(imports_passed_through=lambda: nullcontext()),
)
fake_temporalio.activity = types.SimpleNamespace(defn=lambda fn: fn)
fake_common = types.ModuleType("temporalio.common")
fake_common.RetryPolicy = lambda **kwargs: kwargs
fake_exceptions = types.ModuleType("temporalio.exceptions")
fake_exceptions.ActivityError = RuntimeError
fake_exceptions.ApplicationError = RuntimeError
fake_temporalio.workflow = fake_workflow
sys.modules["temporalio"] = fake_temporalio
sys.modules["temporalio.common"] = fake_common
sys.modules["temporalio.exceptions"] = fake_exceptions

from app.temporal_workflows import FridayHeavyJobWorkflow


def test_rehydratable_activity_error_detects_worker_exit_and_oom() -> None:
    workflow = FridayHeavyJobWorkflow()

    assert workflow._is_rehydratable_activity_error("worker exited without reporting a terminal state")
    assert workflow._is_rehydratable_activity_error("exit 137 from worker container")
    assert not workflow._is_rehydratable_activity_error("verification code required")


def test_interrupted_result_with_pending_followup_restarts_with_followup_claim(monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []
    execution_results = [
        {"kind": "failed", "interrupted": True, "error_message": "worker interrupted after follow-up signal"},
        {"kind": "completed", "result_text": "done", "output_files": [], "artifact_keys": []},
    ]

    async def fake_execute_activity(activity_ref, *args, **kwargs):
        if activity_ref == "execute_heavy_job_activity":
            recorded.append(("execute", args[0]))
            return execution_results.pop(0)
        name = getattr(activity_ref, "__name__", str(activity_ref))
        payload = args if args else tuple(kwargs.get("args", ()))
        recorded.append((name, payload))
        if name == "prepare_heavy_job_claim":
            return {"claim": "initial"}
        if name == "prepare_heavy_job_followup_claim":
            return {"claim": "followup"}
        if name == "finalize_heavy_job_completed":
            return None
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(fake_workflow, "execute_activity", fake_execute_activity, raising=False)

    workflow = FridayHeavyJobWorkflow()
    workflow.update_constraints("Prefer nonstop if the price difference is not too big.")

    async def run_case() -> None:
        await workflow.run(
            {
                "job_id": "job-123",
                "heavy_task_queue": "friday-heavy-activity",
                "heavy_activity_max_attempts": 1,
            }
        )

    asyncio.run(run_case())

    assert ("prepare_heavy_job_followup_claim", ("job-123", "Prefer nonstop if the price difference is not too big.")) in recorded
    assert ("finalize_heavy_job_completed", ("job-123", "done", [], [])) in recorded


def test_interrupted_result_waits_briefly_for_followup_signal_race(monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []
    execution_results = [
        {"kind": "failed", "interrupted": True, "error_message": "worker interrupted after follow-up signal"},
        {"kind": "completed", "result_text": "done", "output_files": [], "artifact_keys": []},
    ]
    workflow_instance = FridayHeavyJobWorkflow()

    async def fake_execute_activity(activity_ref, *args, **kwargs):
        if activity_ref == "execute_heavy_job_activity":
            recorded.append(("execute", args[0]))
            return execution_results.pop(0)
        name = getattr(activity_ref, "__name__", str(activity_ref))
        payload = args if args else tuple(kwargs.get("args", ()))
        recorded.append((name, payload))
        if name == "prepare_heavy_job_claim":
            return {"claim": "initial"}
        if name == "prepare_heavy_job_followup_claim":
            return {"claim": "followup"}
        if name == "finalize_heavy_job_completed":
            return None
        raise AssertionError(f"unexpected activity: {name}")

    async def fake_sleep(_delay):
        workflow_instance.pending_followup = "Prefer nonstop if the price difference is not too big."

    monkeypatch.setattr(fake_workflow, "execute_activity", fake_execute_activity, raising=False)
    monkeypatch.setattr(fake_workflow, "sleep", fake_sleep, raising=False)

    async def run_case() -> None:
        await workflow_instance.run(
            {
                "job_id": "job-456",
                "heavy_task_queue": "friday-heavy-activity",
                "heavy_activity_max_attempts": 1,
            }
        )

    asyncio.run(run_case())

    assert ("prepare_heavy_job_followup_claim", ("job-456", "Prefer nonstop if the price difference is not too big.")) in recorded
    assert ("finalize_heavy_job_completed", ("job-456", "done", [], [])) in recorded


def test_interrupted_result_recovers_followup_from_durable_claim_path(monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []
    execution_results = [
        {"kind": "failed", "interrupted": True, "error_message": "worker interrupted after follow-up signal"},
        {"kind": "completed", "result_text": "done", "output_files": [], "artifact_keys": []},
    ]

    async def fake_execute_activity(activity_ref, *args, **kwargs):
        if activity_ref == "execute_heavy_job_activity":
            recorded.append(("execute", args[0]))
            return execution_results.pop(0)
        name = getattr(activity_ref, "__name__", str(activity_ref))
        payload = args if args else tuple(kwargs.get("args", ()))
        recorded.append((name, payload))
        if name == "prepare_heavy_job_claim":
            return {"claim": "initial"}
        if name == "prepare_heavy_job_followup_claim":
            assert payload == ("job-789", "")
            return {"claim": "followup"}
        if name == "finalize_heavy_job_completed":
            return None
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(fake_workflow, "execute_activity", fake_execute_activity, raising=False)
    monkeypatch.setattr(fake_workflow, "sleep", lambda _delay: asyncio.sleep(0), raising=False)

    async def run_case() -> None:
        await FridayHeavyJobWorkflow().run(
            {
                "job_id": "job-789",
                "heavy_task_queue": "friday-heavy-activity",
                "heavy_activity_max_attempts": 1,
            }
        )

    asyncio.run(run_case())

    assert ("prepare_heavy_job_followup_claim", ("job-789", "")) in recorded
    assert ("finalize_heavy_job_completed", ("job-789", "done", [], [])) in recorded


def test_activity_cancelled_error_with_followup_restarts_instead_of_finalizing(monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []
    execute_count = {"value": 0}

    async def fake_execute_activity(activity_ref, *args, **kwargs):
        if activity_ref == "execute_heavy_job_activity":
            execute_count["value"] += 1
            recorded.append(("execute", args[0]))
            if execute_count["value"] == 1:
                raise RuntimeError("Activity cancelled")
            return {"kind": "completed", "result_text": "done", "output_files": [], "artifact_keys": []}
        name = getattr(activity_ref, "__name__", str(activity_ref))
        payload = args if args else tuple(kwargs.get("args", ()))
        recorded.append((name, payload))
        if name == "prepare_heavy_job_claim":
            return {"claim": "initial"}
        if name == "prepare_heavy_job_followup_claim":
            assert payload == ("job-999", "Prefer nonstop if the price difference is not too big.")
            return {"claim": "followup"}
        if name == "finalize_heavy_job_completed":
            return None
        raise AssertionError(f"unexpected activity: {name}")

    monkeypatch.setattr(fake_workflow, "execute_activity", fake_execute_activity, raising=False)

    workflow = FridayHeavyJobWorkflow()
    workflow.update_constraints("Prefer nonstop if the price difference is not too big.")

    async def run_case() -> None:
        await workflow.run(
            {
                "job_id": "job-999",
                "heavy_task_queue": "friday-heavy-activity",
                "heavy_activity_max_attempts": 1,
            }
        )

    asyncio.run(run_case())

    assert ("prepare_heavy_job_followup_claim", ("job-999", "Prefer nonstop if the price difference is not too big.")) in recorded
    assert ("finalize_heavy_job_completed", ("job-999", "done", [], [])) in recorded
