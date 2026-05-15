from __future__ import annotations

from .jobs import ThreadTurn, ThreadTurnRole


def build_thread_summary(turns: list[ThreadTurn], *, max_chars: int) -> str:
    lines: list[str] = []
    for turn in turns:
        role = "User" if turn.role == ThreadTurnRole.USER else "Friday"
        lines.append(f"{role}: {turn.text.strip().replace(chr(10), ' ')[:260]}")
    return "\n".join(lines)[-max_chars:]
