from app.artifacts import (
    is_browser_step_screenshot,
    query_requests_browser_images,
    query_requests_output_files,
    visible_output_files,
)


def test_detects_browser_step_screenshot_paths() -> None:
    assert is_browser_step_screenshot("browser/screenshots/browser-use-step-1.png")
    assert is_browser_step_screenshot(r"browser\screenshots\browser-use-step-2.png")
    assert not is_browser_step_screenshot("reports/final-summary.pdf")


def test_query_requests_browser_images_only_when_explicit() -> None:
    assert query_requests_browser_images("Take screenshots of each step and send them to me")
    assert query_requests_browser_images("Return a png of the result")
    assert not query_requests_browser_images("Research cameras and send me a PDF")


def test_query_requests_output_files_only_when_explicit() -> None:
    assert query_requests_output_files("Save a txt file with the results")
    assert query_requests_output_files("Export a PDF report")
    assert query_requests_output_files("Return a csv")
    assert not query_requests_output_files("Find me the best nonstop flights to Chicago")


def test_visible_output_files_hides_unrequested_outputs_by_default() -> None:
    output_files = [
        "browser/screenshots/browser-use-step-1.png",
        "reports/final-summary.pdf",
        "tables/results.csv",
    ]
    assert visible_output_files(
        output_files,
        include_browser_step_screenshots=False,
        include_other_output_files=False,
    ) == []
    assert visible_output_files(
        output_files,
        include_browser_step_screenshots=False,
        include_other_output_files=True,
    ) == [
        "reports/final-summary.pdf",
        "tables/results.csv",
    ]
    assert visible_output_files(
        output_files,
        include_browser_step_screenshots=True,
        include_other_output_files=True,
    ) == output_files
