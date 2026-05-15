from app.jobs import TaskClass
from app.routing import classify_task, is_long_task, needs_confirmation, should_offer_browser_tool


def test_siri_short_task_can_be_spoken() -> None:
    assert not is_long_task("what time is it in Tokyo?")


def test_browser_or_research_task_is_long() -> None:
    assert is_long_task("research the best replacement router for my apartment")
    assert is_long_task("browse amazon for a cheap HDMI adapter")


def test_general_question_is_not_marked_long() -> None:
    assert not is_long_task("how does one get water in barcelona?")
    assert not is_long_task("look up the weather in Tokyo for tomorrow")


def test_browser_tool_is_only_offered_for_live_web_queries() -> None:
    assert not should_offer_browser_tool("how does one get water in barcelona?")
    assert should_offer_browser_tool("look up the weather in Tokyo for tomorrow")
    assert should_offer_browser_tool("find the current homepage headline on example.com")


def test_heavy_task_classification() -> None:
    assert classify_task("fill out this website form for me") == TaskClass.HEAVY
    assert classify_task("process this attachment", has_attachment=True) == TaskClass.HEAVY
    assert classify_task("what time is it in Tokyo?") == TaskClass.LIGHT


def test_risky_actions_require_confirmation() -> None:
    assert needs_confirmation("buy this for me")
    assert needs_confirmation("send that message to Alex")
    assert needs_confirmation("delete the old account")
    assert needs_confirmation("submit this application for me")


def test_read_only_question_does_not_require_confirmation() -> None:
    assert not needs_confirmation("summarize what this article says")
    assert not needs_confirmation("research good cameras for travel and compare reviews")
    assert not needs_confirmation("draft a recommendation table for me")
