"""진행 상황 로그.

GitHub Actions 실행 화면에서 실시간으로 보이도록 표준 출력에 바로 쓴다(flush).
토큰이나 페이지 본문은 쓰지 않는다. 페이지 제목과 개수, 요청 수, 남은 시간만 쓴다.
"""

from __future__ import annotations

import sys
import time

_start = time.monotonic()
_enabled = True


def enable(on: bool) -> None:
    global _enabled
    _enabled = on


def log(message: str) -> None:
    if not _enabled:
        return
    elapsed = int(time.monotonic() - _start)
    print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {message}", file=sys.stdout, flush=True)


def minutes(seconds: float) -> str:
    if seconds > 24 * 3600:
        return "제한 없음"
    seconds = max(0, int(seconds))
    return f"{seconds // 60}분 {seconds % 60:02d}초"


def short(title: str, limit: int = 60) -> str:
    title = (title or "제목 없음").replace("\n", " ")
    return title if len(title) <= limit else title[: limit - 1] + "…"
