"""노션 ID와 URL 다루기."""

from __future__ import annotations

import re

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def normalize_id(value: str) -> str | None:
    """링크, 하이픈 있는 ID, 하이픈 없는 ID를 모두 하이픈 있는 소문자 ID로 바꾼다.

    링크에서는 쿼리스트링과 블록 앵커(#...)를 버리고 마지막 ID를 쓴다.
    """
    if not value:
        return None
    text = str(value).split("?")[0].split("#")[0]
    found = _UUID.findall(text)
    if not found:
        return None
    raw = found[-1].replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def compact(page_id: str) -> str:
    return page_id.replace("-", "")


def notion_url(page_id: str) -> str:
    return "https://www.notion.so/" + compact(page_id)


def is_notion_link(href: str) -> bool:
    """노션 페이지를 가리키는 링크인지 본다. 상대 링크(`/<id>`)도 노션 내부 링크다."""
    if not href:
        return False
    if href.startswith("/") and normalize_id(href):
        return True
    lowered = href.lower()
    return ("notion.so/" in lowered or "notion.site/" in lowered) and normalize_id(href) is not None
