from __future__ import annotations

from pathlib import Path

import pytest

from app.workspace import Workspace


def test_workspace_resolve_allows_absolute_path_inside_root(tmp_path: Path) -> None:
    workspace = Workspace(str(tmp_path))
    target = workspace.write_text("reports/example.txt", "hello")
    resolved = workspace.resolve(str(tmp_path / target))
    assert resolved == (tmp_path / target).resolve()


def test_workspace_resolve_rejects_absolute_path_outside_root(tmp_path: Path) -> None:
    workspace = Workspace(str(tmp_path))
    with pytest.raises(ValueError):
        workspace.resolve("/tmp/not-in-workspace.txt")
