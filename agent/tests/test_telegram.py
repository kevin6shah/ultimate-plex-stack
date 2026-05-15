from app.telegram import parse_telegram_update


def test_parse_telegram_from_field() -> None:
    update = parse_telegram_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 2,
                "chat": {"id": 123},
                "from": {"id": 456, "is_bot": False},
                "text": "hello",
            },
        }
    )

    assert update.message is not None
    assert update.message.chat.id == 123
    assert update.message.from_ is not None
    assert update.message.from_.id == 456
    assert update.message.text == "hello"


def test_parse_telegram_document_and_caption() -> None:
    update = parse_telegram_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 3,
                "chat": {"id": 123},
                "from": {"id": 456, "is_bot": False},
                "caption": "process this",
                "document": {
                    "file_id": "abc",
                    "file_unique_id": "def",
                    "file_name": "report.csv",
                    "mime_type": "text/csv",
                    "file_size": 120,
                },
            },
        }
    )

    assert update.message is not None
    assert update.message.document is not None
    assert update.message.effective_text == "process this"
    assert update.message.document.file_name == "report.csv"
