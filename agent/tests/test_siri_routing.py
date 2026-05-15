import json

from app.jobs import AgentJob, JobSource
from app.routing import is_long_task


def test_siri_long_task_job_serialization_targets_telegram_notification() -> None:
    job = AgentJob(
        source=JobSource.SIRI,
        query="research three options for dinner near me",
        chat_id="123",
        user_id="siri",
        long_task=is_long_task("research three options for dinner near me"),
    )

    payload = json.loads(job.model_dump_json())
    assert payload["source"] == "siri"
    assert payload["chat_id"] == "123"
    assert payload["long_task"] is True
