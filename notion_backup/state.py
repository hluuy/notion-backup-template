"""상태 파일(명세서 4.1절)과 상태 복구."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import STATE_FILE
from .ids import normalize_id
from .properties import read_front_matter

VERSION = 1


@dataclass
class Image:
    block: str
    source: str
    file: str  # 페이지 폴더 기준 `images/<이름>`


@dataclass
class Entry:
    kind: str  # page | database | data_source
    path: str  # 페이지는 파일, 데이터베이스와 데이터 소스는 폴더(`notes/` 기준)
    title: str = ""
    parent: str | None = None
    root: str | None = None
    created: str | None = None
    last_edited: str | None = None
    url: str | None = None
    children: list[str] = field(default_factory=list)
    children_at: str | None = None
    links: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    props_hash: str | None = None
    images: list[Image] = field(default_factory=list)
    failed_images: list[str] = field(default_factory=list)
    rendered_at: str | None = None
    refetch: bool = False
    unreachable_since: str | None = None
    fail_count: int = 0
    list_fail_count: int = 0  # 데이터 소스: 목록을 끝까지 받지 못한 연속 횟수
    single_source: bool = True  # 데이터베이스: 데이터 소스가 하나인지
    files: list[str] = field(default_factory=list)  # 데이터베이스: 자동으로 만든 목록 파일(README, CSV)

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        data = dict(data)
        data["images"] = [Image(**i) for i in data.get("images", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class State:
    entries: dict[str, Entry] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)  # {"id", "kind", "parent", "root"}
    incomplete_roots: list[str] = field(default_factory=list)
    full_rebuild_since: str | None = None
    recovered: bool = False

    # ------------------------------------------------------------------ 저장
    def to_json(self) -> str:
        data = {
            "version": VERSION,
            "roots": sorted(self.roots),
            "queue": self.queue,
            "incomplete_roots": sorted(self.incomplete_roots),
            "full_rebuild_since": self.full_rebuild_since,
            "entries": {k: _clean(asdict(v)) for k, v in sorted(self.entries.items())},
        }
        return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n"

    def save(self, notes: Path) -> None:
        notes.mkdir(parents=True, exist_ok=True)
        tmp = notes / (STATE_FILE + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(notes / STATE_FILE)

    @classmethod
    def load(cls, notes: Path) -> "State | None":
        path = notes / STATE_FILE
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != VERSION:
                return None
            state = cls(
                entries={k: Entry.from_dict(v) for k, v in data.get("entries", {}).items()},
                roots=list(data.get("roots", [])),
                queue=list(data.get("queue", [])),
                incomplete_roots=list(data.get("incomplete_roots", [])),
                full_rebuild_since=data.get("full_rebuild_since"),
            )
        except (ValueError, TypeError, KeyError):
            return None
        return state


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """기본값인 항목은 빼서 상태 파일을 짧게 유지한다."""
    defaults = {
        "parent": None, "root": None, "created": None, "last_edited": None, "url": None,
        "children": [], "children_at": None, "links": [], "synced": [], "props_hash": None,
        "images": [], "failed_images": [], "rendered_at": None, "refetch": False,
        "unreachable_since": None, "fail_count": 0, "list_fail_count": 0, "single_source": True,
        "title": "", "files": [],
    }
    out = {}
    for key, value in data.items():
        if key in defaults and value == defaults[key]:
            continue
        if isinstance(value, list):
            value = sorted(value) if key in ("links", "synced", "failed_images") else value
        out[key] = value
    return out


_IMAGE_LINK = re.compile(r"\]\((images/[^)\s]+)\)")


def recover(notes: Path) -> tuple[State, list[str], list[str]]:
    """상태 파일이 없거나 깨졌을 때 `notes/`의 파일에서 상태를 다시 만든다.

    돌려주는 값: (상태, notion_id가 없는 파일, 주인을 알 수 없는 이미지)
    """
    state = State(recovered=True)
    no_id: list[str] = []
    used_images: set[str] = set()
    if not notes.exists():
        return state, no_id, []
    for md in sorted(notes.rglob("*.md")):
        rel = md.relative_to(notes).as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            no_id.append(rel)
            continue
        meta = read_front_matter(text)
        page_id = normalize_id(meta.get("notion_id")) if meta and meta.get("notion_id") else None
        if not page_id:
            if rel != "README.md" and md.name != "README.md":
                no_id.append(rel)
            elif md.name == "README.md" and rel != "README.md":
                # 데이터베이스 목록 README는 front matter가 없다. 페이지 README만 주인을 찾는다.
                pass
            continue
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        images = []
        for link in _IMAGE_LINK.findall(text):
            name = link.split("/", 1)[1]
            block = normalize_id(name.split(".")[0])
            if block:
                images.append(Image(block=block, source="", file=link))
                used_images.add(f"{folder}/{link}" if folder else link)
        state.entries[page_id] = Entry(kind="page", path=rel, images=images, refetch=True, url=meta.get("notion_url"))
    orphans = []
    for img in sorted(notes.rglob("images/*")):
        rel = img.relative_to(notes).as_posix()
        if rel not in used_images:
            orphans.append(rel)
    return state, no_id, orphans
