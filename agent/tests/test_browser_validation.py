from app.browser_validation import (
    browser_modal_dismissal_instruction,
    extract_validation_evidence,
    result_has_validation_evidence,
    task_requires_confirmation_evidence,
    validation_failure_text,
    validation_instruction_for_task,
)


def test_task_requires_confirmation_evidence_for_booking_and_cancel() -> None:
    assert task_requires_confirmation_evidence("Book the reservation on Resy for 7:30 PM")
    assert task_requires_confirmation_evidence("Cancel that reservation and confirm it is cancelled")
    assert not task_requires_confirmation_evidence("Find me Indian restaurants near Midtown")


def test_extract_validation_evidence_finds_confirmation_markers() -> None:
    evidence = extract_validation_evidence(
        "Book this reservation",
        "Reservation confirmed.\nConfirmation number: ABC123\nCurrent page: https://example.com",
    )
    assert "Reservation confirmed." in evidence
    assert "Confirmation number: ABC123" in evidence


def test_result_has_validation_evidence_uses_explicit_lines() -> None:
    assert result_has_validation_evidence(
        "Book the table",
        "Done.",
        explicit_evidence=["Confirmation number: ABC123"],
    )


def test_validation_instruction_and_failure_text_are_specific() -> None:
    assert "explicit success evidence" in validation_instruction_for_task("Book this dinner")
    assert "booking confirmation evidence" in validation_failure_text("Book this dinner")
    assert "cancellation confirmation evidence" in validation_failure_text("Cancel that reservation")
    assert "dismiss non-essential cookie banners" in browser_modal_dismissal_instruction()
