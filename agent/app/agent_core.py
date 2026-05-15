from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from pydantic_ai import Agent, RunContext

from .browser import BrowserSession, run_browser_task
from .budget import estimate_deepseek_cost, usage_from_pydantic_ai
from .jobs import AgentConfig, AgentResult, ThreadTurn, ThreadTurnRole
from .jobs import CheckpointPayload
from .prompts import STATIC_SYSTEM_PROMPT
from .routing import needs_confirmation
from .settings import Settings
from .storage import StateStore
from .workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass
class AgentDependencies:
    settings: Settings
    workspace: Optional[Workspace] = None
    browser: Optional[BrowserSession] = None


def _select_model(query: str, settings: Settings) -> str:
    lowered = query.lower()
    if any(token in lowered for token in ("think deeply", "reason", "plan carefully", "complex")):
        return settings.reasoner_model
    return settings.agent_model


def _render_user_query(
    query: str,
    *,
    agent_name: str,
    persona_summary: str,
    context_summary: str,
    recent_turns: list[ThreadTurn],
    durable_memories: list[str],
    attachments: list[str],
    mode: str,
    resume_checkpoint: Optional[CheckpointPayload],
) -> str:
    parts = []
    if persona_summary.strip():
        parts.append(f"{agent_name} persona:\n{persona_summary.strip()[:1200]}")
    if durable_memories:
        parts.append("Durable memory:\n" + "\n".join(f"- {memory}" for memory in durable_memories[:20]))
    if context_summary:
        parts.append("Recent 48-hour context:\n" + context_summary[:3500])
    if recent_turns:
        rendered_turns = []
        for turn in recent_turns[-12:]:
            speaker = "User" if turn.role == ThreadTurnRole.USER else "Friday"
            rendered_turns.append(f"{speaker}: {turn.text}")
        parts.append("Recent turns:\n" + "\n".join(rendered_turns)[-5000:])
    if attachments:
        parts.append("Workspace inputs:\n" + "\n".join(f"- {name}" for name in attachments[:50]))
    if resume_checkpoint is not None:
        resume_parts = [f"Previous checkpoint summary:\n{resume_checkpoint.summary[:3000]}"]
        if resume_checkpoint.current_step:
            resume_parts.append(f"Previous step: {resume_checkpoint.current_step[:500]}")
        if resume_checkpoint.resume_instructions:
            resume_parts.append(f"Resume instructions:\n{resume_checkpoint.resume_instructions[:3000]}")
        if resume_checkpoint.workspace_files:
            resume_parts.append("Prior workspace files:\n" + "\n".join(f"- {name}" for name in resume_checkpoint.workspace_files[:50]))
        if resume_checkpoint.tool_outputs:
            resume_parts.append("Prior tool outputs:\n" + "\n".join(f"- {value}" for value in resume_checkpoint.tool_outputs[:20])[:3500])
        parts.append("\n".join(resume_parts))
    if mode == "heavy":
        parts.append(
            "You are running inside the shared-host hands worker. "
            "Use browser and workspace tools when needed, keep actions bounded, and leave clear intermediate state."
        )
    parts.append("User request:\n" + query)
    return "\n\n".join(parts)


async def run_agent(
    query: str,
    *,
    settings: Settings,
    store: StateStore,
    mode: str = "light",
    context_summary: str = "",
    recent_turns: Optional[list[ThreadTurn]] = None,
    durable_memories: Optional[list[str]] = None,
    workspace: Optional[Workspace] = None,
    attachment_names: Optional[list[str]] = None,
    config: Optional[AgentConfig] = None,
    resume_checkpoint: Optional[CheckpointPayload] = None,
) -> AgentResult:
    if not store.budget_available():
        logger.warning("agent budget blocked request")
        return AgentResult(text="Daily Budget Reached", budget_blocked=True)

    if needs_confirmation(query):
        logger.info("agent request requires confirmation")
        return AgentResult(text="This task may change data, send information, or spend money. Reply with explicit confirmation and the exact action you want me to take.")

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not deepseek_key:
        deepseek_key = settings.secret(settings.deepseek_api_key_param)
    if deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key

    memories = durable_memories or []
    attachments = attachment_names or []
    effective_config = config or AgentConfig()
    effective_query = _render_user_query(
        query,
        agent_name=effective_config.agent_name,
        persona_summary=effective_config.persona_summary,
        context_summary=context_summary,
        recent_turns=recent_turns or [],
        durable_memories=memories,
        attachments=attachments,
        mode=mode,
        resume_checkpoint=resume_checkpoint,
    )

    deps = AgentDependencies(
        settings=settings,
        workspace=workspace,
        browser=BrowserSession(workspace=workspace) if mode == "heavy" else None,
    )
    system_prompt = STATIC_SYSTEM_PROMPT
    if effective_config.agent_name.strip() and effective_config.agent_name.strip() != "Friday":
        system_prompt = system_prompt.replace("You are Friday, Kevin's private personal task agent.", f"You are {effective_config.agent_name.strip()}, Kevin's private personal task agent.")
    if effective_config.system_prompt_suffix.strip():
        system_prompt = f"{system_prompt}\n\nAdditional operator guidance:\n{effective_config.system_prompt_suffix.strip()}"
    agent = Agent(_select_model(query, settings), deps_type=AgentDependencies, system_prompt=system_prompt)

    if mode == "heavy":
        effective_query += (
            "\n\nIf the user asks for a deliverable file such as a PDF, create it in the workspace before you finish. "
            "Prefer concise, useful files over large raw dumps."
        )

        @agent.tool
        async def web_browser_task(ctx: RunContext[AgentDependencies], task: str, max_pages: int = 3, max_steps: int = 12) -> str:
            """Read live web pages when current/public web information is needed."""
            bounded_pages = min(max(max_pages, 1), ctx.deps.settings.max_browser_pages)
            bounded_steps = min(max(max_steps, 1), ctx.deps.settings.max_browser_steps)
            return await run_browser_task(task, max_pages=bounded_pages, max_steps=bounded_steps)

        @agent.tool
        async def browser_start(ctx: RunContext[AgentDependencies], start_url: str = "") -> str:
            """Start a persistent browser session for multi-step website actions."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.start(start_url)

        @agent.tool
        async def browser_navigate(ctx: RunContext[AgentDependencies], url: str) -> str:
            """Navigate the persistent browser session to a URL."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.goto(url)

        @agent.tool
        async def browser_click(ctx: RunContext[AgentDependencies], selector: str) -> str:
            """Click an element in the persistent browser session using a CSS selector."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.click(selector)

        @agent.tool
        async def browser_type(ctx: RunContext[AgentDependencies], selector: str, text: str, submit: bool = False) -> str:
            """Fill an input in the persistent browser session."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.type_text(selector, text, submit=submit)

        @agent.tool
        async def browser_press(ctx: RunContext[AgentDependencies], selector: str, key: str) -> str:
            """Press a keyboard key on an element in the persistent browser session."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.press(selector, key)

        @agent.tool
        async def browser_read(ctx: RunContext[AgentDependencies], selector: str = "body", limit: int = 3500) -> str:
            """Read visible text from the current page or a selector in the persistent browser session."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.read(selector=selector, limit=limit)

        @agent.tool
        async def browser_wait_for_text(ctx: RunContext[AgentDependencies], text: str, timeout_seconds: int = 10) -> str:
            """Wait for specific text to appear on the current page."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.wait_for_text(text=text, timeout_seconds=timeout_seconds)

        @agent.tool
        async def browser_upload_file(ctx: RunContext[AgentDependencies], selector: str, relative_path: str) -> str:
            """Upload a workspace file through a file input selector."""
            assert ctx.deps.browser is not None
            assert ctx.deps.workspace is not None
            return await ctx.deps.browser.upload_file(selector, str(ctx.deps.workspace.resolve(relative_path)))

        @agent.tool
        async def browser_list_links(ctx: RunContext[AgentDependencies], limit: int = 20) -> str:
            """List visible links on the current page."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.list_links(limit=limit)

        @agent.tool
        async def browser_screenshot(ctx: RunContext[AgentDependencies], relative_path: str = "browser/current-page.png", full_page: bool = True) -> str:
            """Save a screenshot of the current page into the workspace."""
            assert ctx.deps.browser is not None
            return await ctx.deps.browser.screenshot(relative_path=relative_path, full_page=full_page)

        @agent.tool
        async def browser_close(ctx: RunContext[AgentDependencies]) -> str:
            """Close the persistent browser session."""
            assert ctx.deps.browser is not None
            await ctx.deps.browser.close()
            return "Browser session closed."

        if workspace is not None:

            @agent.tool
            async def workspace_list_files(ctx: RunContext[AgentDependencies]) -> str:
                """List files available in the current workspace."""
                assert ctx.deps.workspace is not None
                return "\n".join(ctx.deps.workspace.list_files())[:4000]

            @agent.tool
            async def workspace_read_file(ctx: RunContext[AgentDependencies], relative_path: str, limit: int = 6000) -> str:
                """Read a text-like workspace file."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.read_text(relative_path, limit=limit)

            @agent.tool
            async def workspace_preview_file(ctx: RunContext[AgentDependencies], relative_path: str, rows: int = 10) -> str:
                """Preview CSV, XLSX, PDF, JSON, or text content from the workspace."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.preview_table(relative_path, rows=rows)

            @agent.tool
            async def workspace_write_text_file(ctx: RunContext[AgentDependencies], relative_path: str, content: str) -> str:
                """Write or overwrite a text file inside the workspace."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.write_text(relative_path, content)

            @agent.tool
            async def workspace_write_pdf_report(ctx: RunContext[AgentDependencies], relative_path: str, title: str, body_text: str) -> str:
                """Create a simple PDF report inside the workspace."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.write_pdf(relative_path, title, body_text)

            @agent.tool
            async def workspace_run_shell(ctx: RunContext[AgentDependencies], command: str, timeout_seconds: int = 60) -> str:
                """Run a shell command inside the isolated workspace container."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.run_shell(command, timeout_seconds=timeout_seconds)

            @agent.tool
            async def workspace_run_python(ctx: RunContext[AgentDependencies], code: str, timeout_seconds: int = 60) -> str:
                """Run Python code inside the isolated workspace container."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.run_python(code, timeout_seconds=timeout_seconds)

            @agent.tool
            async def workspace_snapshot(ctx: RunContext[AgentDependencies]) -> str:
                """Summarize the current workspace contents."""
                assert ctx.deps.workspace is not None
                return ctx.deps.workspace.workspace_snapshot()

    try:
        result = await agent.run(effective_query, deps=deps)
    finally:
        if deps.browser is not None:
            await deps.browser.close()

    raw_usage = result.usage() if callable(getattr(result, "usage", None)) else getattr(result, "usage", None)
    usage = usage_from_pydantic_ai(raw_usage)
    cost = estimate_deepseek_cost(
        usage,
        input_cache_miss_per_1m=settings.deepseek_input_cache_miss_per_1m,
        input_cache_hit_per_1m=settings.deepseek_input_cache_hit_per_1m,
        output_per_1m=settings.deepseek_output_per_1m,
    )
    store.add_spend(cost)
    logger.info(
        "agent completed request mode=%s input_tokens=%s output_tokens=%s cache_hits=%s cost_usd=%s",
        mode,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        cost,
    )
    output = getattr(result, "output", None)
    return AgentResult(text=str(output or ""), cost_usd=str(cost.quantize(Decimal("0.000001"))))
