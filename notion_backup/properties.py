"""데이터베이스 속성과 시각 형식(명세서 6.3절, 6.4절)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

KST = timezone(timedelta(hours=9))
SYSTEM_KEYS = ("notion_id", "notion_url", "created", "last_edited")


def to_kst(iso: str | None) -> str | None:
    """노션 ISO 시각을 한국 시간(+09:00) 초 단위 문자열로."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).replace(microsecond=0).isoformat()


def parse_time(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_value(date: dict | None) -> str | None:
    if not date:
        return None
    start, end = date.get("start"), date.get("end")

    def one(value: str | None) -> str | None:
        if not value:
            return None
        return value if len(value) == 10 else to_kst(value)

    if end:
        return f"{one(start)} ~ {one(end)}"
    return one(start)


def plain_text(rich: list[dict] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (rich or []))


def _user_name(user: dict) -> str:
    return user.get("name") or "알 수 없음"


TitleLookup = Callable[[str], str]


def format_value(prop: dict, title_of: TitleLookup) -> Any:
    """속성 하나를 front matter와 CSV에 쓸 값으로."""
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in ("title", "rich_text"):
        return plain_text(value)
    if kind in ("number", "url", "email", "phone_number"):
        return value
    if kind == "checkbox":
        return bool(value)
    if kind in ("select", "status"):
        return value.get("name") if value else None
    if kind == "multi_select":
        return [opt.get("name") for opt in value or []]
    if kind == "people":
        return [_user_name(u) for u in value or []]
    if kind == "date":
        return _date_value(value)
    if kind == "relation":
        return [title_of(rel.get("id")) for rel in value or [] if rel.get("id")]
    if kind == "files":
        return [f.get("name") for f in value or []]
    if kind in ("created_time", "last_edited_time"):
        return to_kst(value)
    if kind in ("created_by", "last_edited_by"):
        return _user_name(value or {})
    if kind == "unique_id":
        if not value:
            return None
        prefix = value.get("prefix")
        return f"{prefix}-{value.get('number')}" if prefix else value.get("number")
    if kind == "formula":
        if not value:
            return None
        ftype = value.get("type")
        if ftype == "date":
            return _date_value(value.get("date"))
        return value.get(ftype)
    if kind == "rollup":
        if not value:
            return None
        rtype = value.get("type")
        if rtype == "array":
            items = []
            for item in value.get("array") or []:
                formatted = format_value(item, title_of)
                if isinstance(formatted, list):
                    items.extend(formatted)
                elif formatted not in (None, ""):
                    items.append(formatted)
            return items
        if rtype == "date":
            return _date_value(value.get("date"))
        return value.get(rtype)
    return None


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def props_hash(values: dict[str, Any]) -> str:
    text = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def front_matter_keys(names: list[str]) -> dict[str, str]:
    """속성 이름 → front matter 키. 시스템 항목과 겹치면 뒤에 `_`를 붙인다."""
    used = {k.lower() for k in SYSTEM_KEYS}
    out = {}
    for name in names:
        key = name
        while key.lower() in used:
            key += "_"
        used.add(key.lower())
        out[name] = key
    return out


def front_matter(meta: dict[str, Any], props: dict[str, Any] | None) -> str:
    """YAML front matter. 키와 문자열 값은 항상 따옴표로 감싼다(JSON 표기는 YAML로도 유효하다)."""
    lines = ["---"]
    for key in SYSTEM_KEYS:
        lines.append(f"{json.dumps(key)}: {json.dumps(meta.get(key), ensure_ascii=False)}")
    if props:
        keys = front_matter_keys(list(props))
        for name, value in props.items():
            lines.append(f"{json.dumps(keys[name], ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False, default=str)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def read_front_matter(text: str) -> dict[str, Any] | None:
    """파일 맨 위의 front matter에서 시스템 항목을 읽는다(상태 복구용)."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    out: dict[str, Any] = {}
    for line in text[4:end].split("\n"):
        if ": " not in line:
            continue
        key, _, raw = line.partition(": ")
        try:
            key = json.loads(key)
            out[key] = json.loads(raw)
        except ValueError:
            continue
    return out


def replace_front_matter(text: str, new_front: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    return new_front + text[end + 5:]
