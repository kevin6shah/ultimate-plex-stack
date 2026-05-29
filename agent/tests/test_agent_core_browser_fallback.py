import asyncio
from dataclasses import replace

from app import agent_core
from app.settings import Settings
from app.strategy_runtime import STRATEGY_STAGEHAND_STEALTH_ACT
from app.workspace import Workspace


def test_general_browser_task_prefers_stagehand_before_browser_use(tmp_path, monkeypatch) -> None:
    settings = replace(Settings(), stagehand_enabled=True, browser_use_enabled=True)
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_browser_task(task: str, *, max_pages: int, max_steps: int) -> str:
        calls.append("public")
        return "browser task could not read any public pages"

    async def fake_stagehand_task(task: str, *, max_steps: int, settings: Settings, workspace: Workspace) -> str:
        calls.append("stagehand")
        return "Structured summary from Stagehand"

    async def fake_browser_use_task(*args, **kwargs) -> str:
        raise AssertionError("browser-use should not run when Stagehand succeeds")

    monkeypatch.setattr(agent_core, "run_browser_task", fake_browser_task)
    monkeypatch.setattr(agent_core, "run_stagehand_task", fake_stagehand_task)
    monkeypatch.setattr(agent_core, "run_browser_use_task", fake_browser_use_task)

    result = asyncio.run(
        agent_core._run_general_browser_task(
            settings=settings,
            workspace=workspace,
            task="Find current visiting hours for this venue",
            max_pages=2,
            max_steps=6,
            strategy_mode=STRATEGY_STAGEHAND_STEALTH_ACT,
        )
    )

    assert result == "Structured summary from Stagehand"
    assert calls == ["stagehand"]


def test_general_browser_task_uses_browser_use_after_stagehand_failure(tmp_path, monkeypatch) -> None:
    settings = replace(Settings(), stagehand_enabled=True, browser_use_enabled=True)
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_browser_task(task: str, *, max_pages: int, max_steps: int) -> str:
        calls.append("public")
        return "browser task could not read any public pages"

    async def fake_stagehand_task(task: str, *, max_steps: int, settings: Settings, workspace: Workspace) -> str:
        calls.append("stagehand")
        return "STAGEHAND_BROWSER_TASK_FAILED: stagehand timed out before finishing."

    async def fake_browser_use_task(*args, **kwargs) -> str:
        calls.append("browser_use")
        return "Fallback browser-use summary"

    monkeypatch.setattr(agent_core, "run_browser_task", fake_browser_task)
    monkeypatch.setattr(agent_core, "run_stagehand_task", fake_stagehand_task)
    monkeypatch.setattr(agent_core, "run_browser_use_task", fake_browser_use_task)

    result = asyncio.run(
        agent_core._run_general_browser_task(
            settings=settings,
            workspace=workspace,
            task="Find current visiting hours for this venue",
            max_pages=2,
            max_steps=6,
            strategy_mode=STRATEGY_STAGEHAND_STEALTH_ACT,
        )
    )

    assert result == "STAGEHAND_BROWSER_TASK_FAILED: stagehand timed out before finishing."
    assert calls == ["stagehand"]


def test_travel_browser_fallback_prefers_stagehand_before_browser_use(tmp_path, monkeypatch) -> None:
    settings = replace(Settings(), stagehand_enabled=True, browser_use_enabled=True)
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_browser_task(task: str, *, max_pages: int, max_steps: int) -> str:
        calls.append("public")
        return "browser task could not read any public pages"

    async def fake_stagehand_task(task: str, *, max_steps: int, settings: Settings, workspace: Workspace) -> str:
        calls.append("stagehand")
        return "Best hotel option is within budget and walkable to Ushuaia."

    async def fake_browser_use_task(*args, **kwargs) -> str:
        raise AssertionError("browser-use should not run when Stagehand succeeds")

    monkeypatch.setattr(agent_core, "run_browser_task", fake_browser_task)
    monkeypatch.setattr(agent_core, "run_stagehand_task", fake_stagehand_task)
    monkeypatch.setattr(agent_core, "run_browser_use_task", fake_browser_use_task)

    result = asyncio.run(
        agent_core._run_travel_browser_fallback(
            settings=settings,
            workspace=workspace,
            task="Find Ibiza hotels for four people near Ushuaia on July 3-5",
            max_pages=2,
            max_steps=6,
            strategy_mode=STRATEGY_STAGEHAND_STEALTH_ACT,
        )
    )

    assert result == "Best hotel option is within budget and walkable to Ushuaia."
    assert calls == ["stagehand"]


def test_restaurant_browser_availability_task_requests_active_page_interaction() -> None:
    task = agent_core._restaurant_browser_availability_task(
        booking_url="https://resy.com/cities/ny/example",
        venue_name="Example Bistro",
        venue_city="New York",
        provider_label="Resy",
        date="2026-05-28",
        party_size=3,
    )

    assert "Do not just summarize the landing screen." in task
    assert "Actively set or confirm the requested date and party size" in task
    assert "reveal actual bookable times" in task
    assert "cancellation, deposit, or prepaid reservation language" in task


def test_restaurant_browser_availability_summary_prefers_resy_probe(tmp_path, monkeypatch) -> None:
    settings = replace(Settings(), stagehand_enabled=True, browser_use_enabled=True)
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_resy_probe(*args, **kwargs):
        calls.append("resy_probe")
        return agent_core.ResyBrowserProbeResult(
            booking_url="https://resy.com/cities/ny/example?date=2026-05-29&seats=3",
            visible_time_labels=("7:30 PM", "8:00 PM"),
            current_url="https://resy.com/cities/ny/example?date=2026-05-29&seats=3",
        )

    async def fake_general_browser_task(*args, **kwargs) -> str:
        calls.append("general_browser")
        return "generic fallback"

    monkeypatch.setattr(agent_core, "_run_resy_browser_probe", fake_resy_probe)
    monkeypatch.setattr(agent_core, "_run_general_browser_task", fake_general_browser_task)

    result = asyncio.run(
        agent_core._restaurant_browser_availability_summary(
            settings=settings,
            workspace=workspace,
            venue_id="91940",
            venue_name="Angel Indian Restaurant",
            venue_city="New York",
            venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
            provider="resy",
            date="2026-05-29",
            party_size=3,
            requested_time="ANY AVAILABLE",
            strategy_mode=STRATEGY_STAGEHAND_STEALTH_ACT,
        )
    )

    assert "BROWSER_RESY_PROBE:" in result
    assert "Live time options visible on the venue page:" in result
    assert "requested time was not present" not in result
    assert calls == ["resy_probe"]


def test_restaurant_browser_availability_summary_falls_back_when_resy_probe_fails(tmp_path, monkeypatch) -> None:
    settings = replace(Settings(), stagehand_enabled=True, browser_use_enabled=True)
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_resy_probe(*args, **kwargs):
        calls.append("resy_probe")
        return None

    async def fake_general_browser_task(*args, **kwargs) -> str:
        calls.append("general_browser")
        return "Structured summary from Stagehand"

    monkeypatch.setattr(agent_core, "_run_resy_browser_probe", fake_resy_probe)
    monkeypatch.setattr(agent_core, "_run_general_browser_task", fake_general_browser_task)

    result = asyncio.run(
        agent_core._restaurant_browser_availability_summary(
            settings=settings,
            workspace=workspace,
            venue_id="91940",
            venue_name="Angel Indian Restaurant",
            venue_city="New York",
            venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
            provider="resy",
            date="2026-05-29",
            party_size=3,
            requested_time="",
            strategy_mode=STRATEGY_STAGEHAND_STEALTH_ACT,
        )
    )

    assert "Structured summary from Stagehand" in result
    assert calls == ["resy_probe", "general_browser"]


def test_restaurant_find_availability_attempts_reuse_booking_preflight_match(tmp_path, monkeypatch) -> None:
    settings = Settings()
    workspace = Workspace(str(tmp_path))

    async def fake_search_attempts(*args, **kwargs):
        raise AssertionError("structured venue search should not rerun when booking preflight already matched the venue")

    monkeypatch.setattr(agent_core, "_restaurant_search_attempts", fake_search_attempts)

    result = asyncio.run(
        agent_core._restaurant_find_availability_attempts(
            settings=settings,
            workspace=workspace,
            query="Angel Indian Restaurant",
            provider="resy",
            city="New York",
            booking_prefill=agent_core.RestaurantBookingPrefill(
                venue_query="Angel Indian Restaurant",
                city="",
                provider="resy",
                date="2026-05-29",
                time="ANY AVAILABLE",
                party_size=3,
            ),
            booking_preflight=agent_core.RestaurantBookingPreflightResult(
                summary="preflight summary",
                matched_venue=agent_core.RestaurantBookingPreflightVenue(
                    venue_id="91940",
                    venue_name="Angel Indian Restaurant",
                    venue_city="New York",
                    venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
                    provider="resy",
                ),
            ),
        )
    )

    assert len(result) == 1
    assert result[0].best_match == {
        "id": "91940",
        "name": "Angel Indian Restaurant",
        "city": "New York",
        "url": "https://resy.com/cities/ny/angel-indian-restaurant",
    }


def test_restaurant_find_availability_attempts_ignore_invented_city_when_prefill_had_none(tmp_path, monkeypatch) -> None:
    settings = Settings()
    workspace = Workspace(str(tmp_path))

    async def fake_search_attempts(*args, **kwargs):
        raise AssertionError("preflight venue should win even if the model invents a city later")

    monkeypatch.setattr(agent_core, "_restaurant_search_attempts", fake_search_attempts)

    result = asyncio.run(
        agent_core._restaurant_find_availability_attempts(
            settings=settings,
            workspace=workspace,
            query="Angel Indian Restaurant",
            provider="resy",
            city="Washington",
            booking_prefill=agent_core.RestaurantBookingPrefill(
                venue_query="Angel Indian Restaurant",
                city="",
                provider="resy",
                date="2026-05-29",
                time="ANY AVAILABLE",
                party_size=3,
            ),
            booking_preflight=agent_core.RestaurantBookingPreflightResult(
                summary="preflight summary",
                matched_venue=agent_core.RestaurantBookingPreflightVenue(
                    venue_id="91940",
                    venue_name="Angel Indian Restaurant",
                    venue_city="New York",
                    venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
                    provider="resy",
                ),
            ),
        )
    )

    assert len(result) == 1
    assert result[0].best_match == {
        "id": "91940",
        "name": "Angel Indian Restaurant",
        "city": "New York",
        "url": "https://resy.com/cities/ny/angel-indian-restaurant",
    }


def test_restaurant_find_availability_attempts_fall_back_to_search_when_preflight_does_not_match(tmp_path, monkeypatch) -> None:
    settings = Settings()
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_search_attempts(*args, **kwargs):
        calls.append("search")
        return [
            agent_core.RestaurantSearchAttempt(
                provider="resy",
                payload={"results": [], "failures": []},
                best_match=None,
            )
        ]

    monkeypatch.setattr(agent_core, "_restaurant_search_attempts", fake_search_attempts)

    result = asyncio.run(
        agent_core._restaurant_find_availability_attempts(
            settings=settings,
            workspace=workspace,
            query="Different Restaurant",
            provider="resy",
            city="New York",
            booking_prefill=agent_core.RestaurantBookingPrefill(
                venue_query="Angel Indian Restaurant",
                city="",
                provider="resy",
                date="2026-05-29",
                time="ANY AVAILABLE",
                party_size=3,
            ),
            booking_preflight=agent_core.RestaurantBookingPreflightResult(
                summary="preflight summary",
                matched_venue=agent_core.RestaurantBookingPreflightVenue(
                    venue_id="91940",
                    venue_name="Angel Indian Restaurant",
                    venue_city="New York",
                    venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
                    provider="resy",
                ),
            ),
        )
    )

    assert len(result) == 1
    assert result[0].best_match is None
    assert calls == ["search"]


def test_restaurant_find_availability_attempts_respect_real_city_conflict_when_prefill_had_city(tmp_path, monkeypatch) -> None:
    settings = Settings()
    workspace = Workspace(str(tmp_path))
    calls: list[str] = []

    async def fake_search_attempts(*args, **kwargs):
        calls.append("search")
        return [
            agent_core.RestaurantSearchAttempt(
                provider="resy",
                payload={"results": [], "failures": []},
                best_match=None,
            )
        ]

    monkeypatch.setattr(agent_core, "_restaurant_search_attempts", fake_search_attempts)

    result = asyncio.run(
        agent_core._restaurant_find_availability_attempts(
            settings=settings,
            workspace=workspace,
            query="Angel Indian Restaurant",
            provider="resy",
            city="Washington",
            booking_prefill=agent_core.RestaurantBookingPrefill(
                venue_query="Angel Indian Restaurant",
                city="New York",
                provider="resy",
                date="2026-05-29",
                time="ANY AVAILABLE",
                party_size=3,
            ),
            booking_preflight=agent_core.RestaurantBookingPreflightResult(
                summary="preflight summary",
                matched_venue=agent_core.RestaurantBookingPreflightVenue(
                    venue_id="91940",
                    venue_name="Angel Indian Restaurant",
                    venue_city="New York",
                    venue_url="https://resy.com/cities/ny/angel-indian-restaurant",
                    provider="resy",
                ),
            ),
        )
    )

    assert len(result) == 1
    assert result[0].best_match is None
    assert calls == ["search"]
