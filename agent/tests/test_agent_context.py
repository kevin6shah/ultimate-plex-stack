from app.context_memory import build_thread_summary
from app.jobs import ThreadTurn, ThreadTurnRole


def test_thread_summary_includes_recent_turns() -> None:
    text = build_thread_summary(
        [
            ThreadTurn(role=ThreadTurnRole.USER, text="Find flights to Chicago."),
            ThreadTurn(role=ThreadTurnRole.ASSISTANT, text="I found a few ORD options."),
            ThreadTurn(role=ThreadTurnRole.USER, text="What about Midway instead?"),
        ],
        max_chars=500,
    )
    assert "User: Find flights to Chicago." in text
    assert "Friday: I found a few ORD options." in text
    assert "User: What about Midway instead?" in text
