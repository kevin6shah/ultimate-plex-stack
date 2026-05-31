from app.jobs import TaskClass
from app.agent_core import _current_local_datetime_text, _direct_tool_mode_summary, _should_expose_browser_tools, _should_expose_travel_browser_fallback
from app.routing import (
    classify_task,
    is_long_task,
    needs_confirmation,
    needs_explicit_operator_confirmation,
    query_domain_tags,
    query_domains_compatible,
    should_offer_browser_tool,
    task_routing_profile,
)
from app.settings import Settings


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
    assert classify_task("plan me a Chicago weekend itinerary and map out the route") == TaskClass.HEAVY
    assert classify_task("build me a spreadsheet of ramen spots in Austin") == TaskClass.HEAVY
    assert classify_task("what time is it in Tokyo?") == TaskClass.LIGHT


def test_task_routing_profile_detection() -> None:
    assert task_routing_profile("build me a spreadsheet of flight prices").name == "spreadsheet_data"
    assert task_routing_profile("plan a two day itinerary in Montreal").name == "itinerary_maps"
    assert task_routing_profile("find me a dinner reservation for Friday").name == "booking_commerce"
    assert task_routing_profile("find me Indian restaurants for 8 PM tonight").name == "booking_commerce"
    assert task_routing_profile("find hotels in Chicago for June 5 to June 7").name == "itinerary_maps"
    assert task_routing_profile("find rental cars in Chicago for next weekend").name == "itinerary_maps"
    assert task_routing_profile("sign up for the site with a new account").name == "login_account"
    assert task_routing_profile("summarize this article for me").name == "general"


def test_query_domains_compatible_rejects_cross_domain_shift() -> None:
    assert not query_domains_compatible(
        "Not flights I'm thinking activities in NYC",
        "Create an account with a free trial for Willow TV",
    )
    assert not query_domains_compatible(
        "Find me the cheapest flights to Delhi",
        "How many calories are in chicken tikka masala?",
    )
    assert query_domains_compatible(
        "Find me Indian restaurants tonight",
        "Preferably ones with free cancellation",
    )
    assert "streaming" in query_domain_tags("Use a Willow TV free trial for the IPL match")


def test_plural_restaurants_request_is_heavy() -> None:
    assert classify_task("find me Indian restaurants for 8 PM tonight") == TaskClass.HEAVY


def test_availabilities_plural_request_is_heavy() -> None:
    assert classify_task("what cuisines have the most availabilities for 10:30 tonight") == TaskClass.HEAVY


def test_dinner_discovery_request_is_heavy() -> None:
    query = "Okay switch gears find me for dinner Indian tomorrow at 7:30pm instead - free cancellation"
    assert classify_task(query) == TaskClass.HEAVY
    assert task_routing_profile(query).name == "booking_commerce"


def test_browser_tools_hidden_for_structured_travel_and_booking() -> None:
    assert not _should_expose_browser_tools(
        "Find me a nonstop flight from NYC to Chicago under $300 on June 5 and return June 7",
        "booking_commerce",
    )
    assert not _should_expose_browser_tools(
        "Find me a dinner reservation in the West Village for 2 tonight",
        "booking_commerce",
    )
    assert not _should_expose_browser_tools(
        "Plan me a hotel and flight itinerary for Montreal next weekend",
        "itinerary_maps",
    )


def test_direct_tool_mode_summary_is_explicit_for_travel_and_restaurants() -> None:
    travel_summary = _direct_tool_mode_summary(
        "Find me a flight from NYC to Chicago next Friday",
        "booking_commerce",
    )
    restaurant_summary = _direct_tool_mode_summary(
        "Book me a table at Carbone tomorrow",
        "booking_commerce",
    )
    assert "direct travel tools first" in travel_summary.lower()
    assert "structured reservation task" in restaurant_summary.lower()


def test_travel_browser_fallback_only_exposed_for_structured_travel() -> None:
    assert _should_expose_travel_browser_fallback(
        "Find me a flight from NYC to Chicago next Friday",
        "booking_commerce",
    )
    assert _should_expose_travel_browser_fallback(
        "Plan me hotels and flights for Montreal next weekend",
        "itinerary_maps",
    )
    assert not _should_expose_travel_browser_fallback(
        "Find me a dinner reservation in the West Village",
        "booking_commerce",
    )
    assert not _should_expose_travel_browser_fallback(
        "Summarize this article for me",
        "general",
    )


def test_risky_actions_require_confirmation() -> None:
    assert needs_confirmation("buy this for me")
    assert needs_confirmation("send that message to Alex")
    assert needs_confirmation("delete the old account")
    assert needs_confirmation("submit this application for me")


def test_read_only_question_does_not_require_confirmation() -> None:
    assert not needs_confirmation("summarize what this article says")
    assert not needs_confirmation("research good cameras for travel and compare reviews")
    assert not needs_confirmation("draft a recommendation table for me")
    assert not needs_confirmation("Need to find hotels for Ibiza Friday check in 3rd July - 5th checkout for 4 people")


def test_travel_checkout_date_does_not_trigger_commerce_confirmation() -> None:
    assert not needs_confirmation("Find hotels in Ibiza with check-in July 3 and checkout July 5")
    assert needs_confirmation("Go to checkout and pay for the order")


def test_informational_questions_about_costs_and_architecture_do_not_require_confirmation() -> None:
    assert not needs_confirmation("Is Temporal free to use?")
    assert not needs_confirmation("For AWS infra where I have an on demand ec2 instance with docker a better system for temporal or lang graph?")
    assert not needs_confirmation("For most of these things is it better to just pay the cost for certain tools?")


def test_only_booking_and_login_profiles_use_generic_confirmation_gate() -> None:
    assert not needs_explicit_operator_confirmation(
        "Do you actually like look up these calories I'm not just asking you to figure this out by lol",
        routing_profile_name="general",
    )
    assert not needs_explicit_operator_confirmation(
        "Find me the cheapest flights to India from June 22 to July 12-15 any of those dates works\n\nContinue the same task using the user's new follow-up.\n\nNew user direction:\nNew York City area to Delhi preferably with free cancellation and lowest flight duration (decent times)",
        routing_profile_name="itinerary_maps",
    )
    assert needs_explicit_operator_confirmation(
        "submit this application for me",
        routing_profile_name="login_account",
    )
    assert needs_explicit_operator_confirmation(
        "buy this for me",
        routing_profile_name="booking_commerce",
    )


def test_current_local_datetime_text_includes_relative_date_guidance() -> None:
    rendered = _current_local_datetime_text(Settings())
    assert "Current local date/time:" in rendered
    assert "Resolve relative dates like today, tomorrow" in rendered
