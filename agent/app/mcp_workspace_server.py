from __future__ import annotations

import os

from .workspace import Workspace


def _workspace() -> Workspace:
    root = os.environ.get("FRIDAY_WORKSPACE_ROOT", "/workspace")
    return Workspace(root)


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        name="friday-workspace",
        instructions=(
            "Friday-owned workspace MCP server. "
            "Operate only inside the provided workspace root. "
            "Use these tools for file inspection, file writing, markdown conversion, and PDF generation."
        ),
        log_level="WARNING",
    )

    @server.tool(description="List visible files in the Friday workspace.")
    def list_workspace_files() -> str:
        workspace = _workspace()
        files = workspace.list_files()
        return "\n".join(files)[:4000]

    @server.tool(description="Read a text-like workspace file.")
    def read_workspace_file(relative_path: str, limit: int = 6000) -> str:
        workspace = _workspace()
        return workspace.read_text(relative_path, limit=limit)

    @server.tool(description="Preview CSV, XLSX, PDF, JSON, or text content from the workspace.")
    def preview_workspace_file(relative_path: str, rows: int = 10) -> str:
        workspace = _workspace()
        return workspace.preview_table(relative_path, rows=rows)

    @server.tool(description="Write or overwrite a text file inside the workspace.")
    def write_workspace_file(relative_path: str, content: str) -> str:
        workspace = _workspace()
        return workspace.write_text(relative_path, content)

    @server.tool(description="Convert a workspace document or image into markdown using MarkItDown.")
    def convert_workspace_file_to_markdown(relative_path: str, output_relative_path: str = "") -> str:
        workspace = _workspace()
        normalized_output = output_relative_path.strip() or None
        return workspace.convert_to_markdown(relative_path, normalized_output)

    @server.tool(description="Create a simple PDF report inside the workspace.")
    def write_workspace_pdf_report(relative_path: str, title: str, body_text: str) -> str:
        workspace = _workspace()
        return workspace.write_pdf(relative_path, title, body_text)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
