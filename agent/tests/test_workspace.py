from app.workspace import Workspace


def test_workspace_blocks_escape(tmp_path) -> None:
    workspace = Workspace(str(tmp_path))
    try:
        workspace.resolve("../oops.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("workspace path escape should fail")


def test_workspace_csv_preview(tmp_path) -> None:
    workspace = Workspace(str(tmp_path))
    workspace.write_text("data.csv", "a,b\n1,2\n3,4\n")
    preview = workspace.preview_table("data.csv", rows=2)
    assert "a | b" in preview
    assert "1 | 2" in preview


def test_workspace_pdf_write_and_preview(tmp_path) -> None:
    workspace = Workspace(str(tmp_path))
    relative_path = workspace.write_pdf("report.pdf", "Flight Report", "Option A\nOption B")
    pdf_path = workspace.resolve(relative_path)
    pdf_bytes = pdf_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"Flight Report" in pdf_bytes
    assert b"Option A" in pdf_bytes
