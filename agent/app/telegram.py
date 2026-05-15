from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from .settings import Settings


class TelegramChat(BaseModel):
    id: int


class TelegramUser(BaseModel):
    id: int
    is_bot: bool = False


class TelegramDocument(BaseModel):
    file_id: str
    file_unique_id: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class TelegramMessage(BaseModel):
    message_id: int
    chat: TelegramChat
    from_: Optional[TelegramUser] = None
    text: Optional[str] = None
    caption: Optional[str] = None
    document: Optional[TelegramDocument] = None

    model_config = {"populate_by_name": True}

    @property
    def effective_text(self) -> str:
        return (self.text or self.caption or "").strip()


class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessage] = None


class TelegramFile(BaseModel):
    file_id: str
    file_unique_id: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None


def parse_telegram_update(payload: dict[str, Any]) -> TelegramUpdate:
    if "message" in payload and isinstance(payload["message"], dict) and "from" in payload["message"]:
        payload = dict(payload)
        payload["message"] = dict(payload["message"])
        payload["message"]["from_"] = payload["message"].pop("from")
    return TelegramUpdate.model_validate(payload)


class TelegramClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._token: Optional[str] = None

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self.settings.secret(self.settings.telegram_bot_token_param)
        return self._token

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self.token:
            raise RuntimeError("Telegram bot token is not configured")
        import httpx

        async with httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": "true",
                },
            )
            response.raise_for_status()

    async def send_document_bytes(self, chat_id: str, file_name: str, content: bytes, caption: str = "") -> None:
        if not self.token:
            raise RuntimeError("Telegram bot token is not configured")
        import httpx

        files = {"document": (file_name, content, "application/octet-stream")}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=max(self.settings.telegram_timeout_seconds, 60)) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendDocument",
                data=data,
                files=files,
            )
            response.raise_for_status()

    async def get_file(self, file_id: str) -> TelegramFile:
        import httpx

        async with httpx.AsyncClient(timeout=self.settings.telegram_timeout_seconds) as client:
            response = await client.get(f"https://api.telegram.org/bot{self.token}/getFile", params={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
            return TelegramFile.model_validate(payload["result"])

    async def download_file_bytes(self, file_path: str) -> bytes:
        import httpx

        async with httpx.AsyncClient(timeout=max(self.settings.telegram_timeout_seconds, 30)) as client:
            response = await client.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}")
            response.raise_for_status()
            return response.content
