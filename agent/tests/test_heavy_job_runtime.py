from types import SimpleNamespace

from app.heavy_job_runtime import build_heavy_claim, durable_findings_text, progress_snapshot_text
from app.jobs import AgentConfig, AgentJob, CheckpointPayload, JobSource, TaskClass, ThreadTurn, ThreadTurnRole


def test_build_heavy_claim_includes_execution_progress_matrix_and_job_context() -> None:
    job = AgentJob(
        source=JobSource.SIRI,
        query="Find flights to Delhi",
        task_class=TaskClass.HEAVY,
        user_id="siri",
        conversation_id="siri",
    )

    state = SimpleNamespace(
        get_context_bundle=lambda **kwargs: (
            "Recent travel context",
            [ThreadTurn(role=ThreadTurnRole.USER, text="Find flights", task_class=TaskClass.HEAVY)],
            AgentConfig(),
        ),
        list_memories=lambda owner: [],
        get_latest_checkpoint=lambda job_id: None,
    )
    settings = SimpleNamespace(artifacts_bucket="bucket-name")

    claim = build_heavy_claim(state, settings, job)

    assert claim["strategy_state"]["current_strategy"] == "api_direct"
    assert claim["execution_progress_matrix"]["current_tier"] == "api_direct"
    assert claim["job_context"]["original_query"] == "Find flights to Delhi"
    assert claim["job_context"]["current_query"] == "Find flights to Delhi"


def test_build_heavy_claim_preserves_persisted_findings_context() -> None:
    job = AgentJob(
        source=JobSource.SIRI,
        query="Find flights to Delhi",
        task_class=TaskClass.HEAVY,
        user_id="siri",
        conversation_id="siri",
        metadata={
            "execution_progress_matrix": {
                "current_tier": "api_direct",
                "tiers": {
                    "api_direct": {"tier": "api_direct", "attempt_count": 1, "consecutive_no_progress": 1},
                    "stagehand_stealth_act": {"tier": "stagehand_stealth_act"},
                    "browser_use_visual_pivot": {"tier": "browser_use_visual_pivot"},
                },
                "last_meaningful_artifact": "The structured provider was rate-limited, so I switched to the next approach.",
            },
            "job_context": {
                "original_query": "Find flights to Delhi",
                "current_query": "Find flights to Delhi",
                "conversation_id": "siri",
                "source": "siri",
                "latest_findings_summary": "The structured provider was rate-limited, so I switched to the next approach.",
                "latest_checkpoint_summary": "checking live flight options and collecting candidate itineraries",
            },
        },
    )

    state = SimpleNamespace(
        get_context_bundle=lambda **kwargs: ("Recent travel context", [], AgentConfig()),
        list_memories=lambda owner: [],
        get_latest_checkpoint=lambda job_id: None,
    )
    settings = SimpleNamespace(artifacts_bucket="bucket-name")

    claim = build_heavy_claim(state, settings, job)

    assert claim["execution_progress_matrix"]["last_meaningful_artifact"] == (
        "The structured provider was rate-limited, so I switched to the next approach."
    )
    assert claim["job_context"]["latest_findings_summary"] == (
        "The structured provider was rate-limited, so I switched to the next approach."
    )


def test_progress_snapshot_text_ignores_cross_domain_status_leak() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Create an account with a free trial for Willow TV",
        task_class=TaskClass.HEAVY,
        current_step="running_agent",
        latest_checkpoint_summary="checking live flight options and collecting candidate itineraries",
        last_status_sent_text="checking live flight options and collecting candidate itineraries.",
    )

    text = progress_snapshot_text(job, None)

    assert text == "running agent"


def test_durable_findings_text_ignores_cross_domain_findings_leak() -> None:
    job = AgentJob(
        source=JobSource.TELEGRAM,
        query="Create an account with a free trial for Willow TV",
        task_class=TaskClass.HEAVY,
        error_message="status_code: 429 rate limit",
        metadata={
            "execution_progress_matrix": {
                "last_meaningful_artifact": "checking live flight options and collecting candidate itineraries",
            },
            "job_context": {
                "latest_findings_summary": "checking live flight options and collecting candidate itineraries",
            },
        },
    )
    checkpoint = CheckpointPayload(summary="checking live flight options and collecting candidate itineraries")

    text = durable_findings_text(
        job=job,
        checkpoint=checkpoint,
        strategy_name="api_direct",
        error_message=job.error_message or "",
    )

    assert "flight options" not in text
    assert "rate-limited" in text
