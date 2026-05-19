import asyncio
from dataclasses import replace

from app import agent_core
from app.settings import Settings
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
        )
    )

    assert result == "Structured summary from Stagehand"
    assert calls == ["public", "stagehand"]


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
        )
    )

    assert result == "Fallback browser-use summary"
    assert calls == ["public", "stagehand", "browser_use"]


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
        )
    )

    assert result == "Best hotel option is within budget and walkable to Ushuaia."
    assert calls == ["public", "stagehand"]
