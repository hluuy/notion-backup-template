"""테스트용 가짜 노션. NotionClient와 같은 메서드를 메모리 위에서 흉내 낸다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from notion_backup.client import NotionError
from notion_backup.deadline import Deadline


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def text(content: str, **ann) -> dict:
    return {"type": "text", "text": {"content": content, "link": None}, "plain_text": content, "annotations": ann}


def paragraph(block_id: str, *parts: dict) -> dict:
    return {"object": "block", "id": block_id, "type": "paragraph", "has_children": False,
            "paragraph": {"rich_text": list(parts)}}


def mention(page_id: str, title: str) -> dict:
    return {"type": "mention", "mention": {"type": "page", "page": {"id": page_id}}, "plain_text": title,
            "annotations": {}}


def _url(obj_id: str) -> str:
    return "https://www.notion.so/" + obj_id.replace("-", "")


@dataclass
class FakePage:
    id: str
    title: str
    parent: str | None  # 페이지 ID 또는 데이터 소스 ID
    blocks: list[dict] = field(default_factory=list)
    trashed: bool = False
    created: str = ""
    edited: str = ""
    tags: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    date: str | None = None


@dataclass
class FakeDatabase:
    id: str
    title: str
    parent: str  # 페이지 ID
    ds: str  # 데이터 소스 ID(하나)
    created: str = ""
    incomplete: bool = False  # 목록 조회가 불완전하다고 응답


class FakeNotion:
    def __init__(self):
        self.pages: dict[str, FakePage] = {}
        self.databases: dict[str, FakeDatabase] = {}
        self.shared: set[str] = set()
        self.fail_blocks: set[str] = set()  # 내용 받기가 실패하는 페이지
        self.fail_objects: set[str] = set()  # 조회가 실패하는 데이터 소스
        self.fail_relations = False  # 관계형 속성 추가 조회 실패
        self.request_count = 0
        self.fetched: list[str] = []  # block_children을 부른 블록 ID(다시 받았는지 확인용)
        self.deadline = Deadline.unlimited()
        self._clock = 0

    # ------------------------------------------------------------------ 조작
    def _tick(self) -> str:
        self._clock += 1
        return f"2026-01-01T{self._clock // 60:02d}:{self._clock % 60:02d}:00.000Z"

    def add_page(self, page_id: str, title: str, parent: str | None = None, blocks=None, shared: bool = False,
                 tags=None, relations=None, date=None):
        now = self._tick()
        self.pages[page_id] = FakePage(page_id, title, parent, list(blocks or []), created=now, edited=now,
                                       tags=list(tags or []), relations=list(relations or []), date=date)
        if shared:
            self.shared.add(page_id)
        return page_id

    def add_database(self, db_id: str, ds_id: str, title: str, parent: str):
        self.databases[db_id] = FakeDatabase(db_id, title, parent, ds_id, created=self._tick())
        return ds_id

    def edit(self, page_id: str, title: str | None = None, blocks=None, parent: str | None = None):
        page = self.pages[page_id]
        if title is not None:
            page.title = title
        if blocks is not None:
            page.blocks = list(blocks)
        if parent is not None:
            page.parent = parent
        page.edited = self._tick()

    def trash(self, page_id: str):
        self.pages[page_id].trashed = True

    # ------------------------------------------------------------------ 구조
    def _db_of_ds(self, ds_id: str) -> FakeDatabase | None:
        return next((d for d in self.databases.values() if d.ds == ds_id), None)

    def _parent_id(self, obj_id: str) -> str | None:
        if obj_id in self.pages:
            return self.pages[obj_id].parent
        if obj_id in self.databases:
            return self.databases[obj_id].parent
        db = self._db_of_ds(obj_id)
        return db.id if db else None

    def _accessible(self, obj_id: str) -> bool:
        seen = set()
        current: str | None = obj_id
        while current and current not in seen:
            if current in self.shared:
                return True
            seen.add(current)
            current = self._parent_id(current)
        return False

    def _obj(self, page: FakePage) -> dict:
        if page.parent is None:
            parent = {"type": "workspace", "workspace": True}
        elif self._db_of_ds(page.parent):
            parent = {"type": "data_source_id", "data_source_id": page.parent}
        else:
            parent = {"type": "page_id", "page_id": page.parent}
        if self._db_of_ds(page.parent or ""):
            props = {
                "이름": {"id": "title", "type": "title", "title": [text(page.title)]},
                "태그": {"id": "t", "type": "multi_select", "multi_select": [{"name": t} for t in page.tags]},
                "날짜": {"id": "d", "type": "date", "date": {"start": page.date, "end": None} if page.date else None},
                "관련": {"id": "r", "type": "relation", "relation": [{"id": r} for r in page.relations[:1]],
                       "has_more": len(page.relations) > 1},
            }
        else:
            props = {"title": {"id": "title", "type": "title", "title": [text(page.title)]}}
        return {
            "object": "page", "id": page.id, "parent": parent, "properties": props,
            "created_time": page.created, "last_edited_time": page.edited, "in_trash": page.trashed,
            "url": _url(page.id),
        }

    def _ds_obj(self, db: FakeDatabase) -> dict:
        return {
            "object": "data_source", "id": db.ds, "title": [text(db.title)],
            "parent": {"type": "database_id", "database_id": db.id}, "created_time": db.created,
            "last_edited_time": db.created,
            "properties": {"이름": {"id": "title", "type": "title"}, "태그": {"id": "t", "type": "multi_select"},
                           "날짜": {"id": "d", "type": "date"},
                           "관련": {"id": "r", "type": "relation"}},
        }

    def _count(self):
        self.request_count += 1
        self.deadline.check()

    # ------------------------------------------------------------------ NotionClient와 같은 메서드
    def search(self) -> list[dict]:
        self._count()
        out = [self._obj(p) for p in self.pages.values() if self._accessible(p.id) and not p.trashed]
        out += [self._ds_obj(d) for d in self.databases.values() if self._accessible(d.id)]
        return out

    def retrieve_page(self, page_id: str) -> dict:
        self._count()
        page = self.pages.get(page_id)
        if not page or not self._accessible(page_id):
            raise NotionError(404, "object_not_found", "없음")
        return self._obj(page)

    def retrieve_block(self, block_id: str) -> dict:
        self._count()
        raise NotionError(404, "object_not_found", "없음")

    def block_children(self, block_id: str) -> list[dict]:
        self._count()
        self.fetched.append(block_id)
        if block_id in self.fail_blocks:
            raise NotionError(500, "internal_server_error", "테스트용 실패")
        page = self.pages.get(block_id)
        if not page:
            return []
        out = [dict(b) for b in page.blocks]
        for child in self.pages.values():
            if child.parent == block_id and not child.trashed:
                out.append({"object": "block", "id": child.id, "type": "child_page", "has_children": True,
                            "child_page": {"title": child.title}})
        for db in self.databases.values():
            if db.parent == block_id:
                out.append({"object": "block", "id": db.id, "type": "child_database", "has_children": False,
                            "child_database": {"title": db.title}})
        return out

    def retrieve_database(self, database_id: str) -> dict:
        self._count()
        db = self.databases.get(database_id)
        if not db or not self._accessible(database_id):
            raise NotionError(404, "object_not_found", "없음")
        return {
            "object": "database", "id": db.id, "title": [text(db.title)], "description": [],
            "data_sources": [{"id": db.ds, "name": db.title}],
            "parent": {"type": "page_id", "page_id": db.parent},
            "created_time": db.created, "last_edited_time": db.created, "in_trash": False, "url": _url(db.id),
        }

    def retrieve_data_source(self, data_source_id: str) -> dict:
        self._count()
        if data_source_id in self.fail_objects:
            raise NotionError(500, "internal_server_error", "테스트용 실패")
        db = self._db_of_ds(data_source_id)
        if not db or not self._accessible(db.id):
            raise NotionError(404, "object_not_found", "없음")
        return self._ds_obj(db)

    def query_data_source(self, data_source_id: str):
        self._count()
        db = self._db_of_ds(data_source_id)
        items = sorted((p for p in self.pages.values() if p.parent == data_source_id and not p.trashed),
                       key=lambda p: p.created)
        return [self._obj(p) for p in items], bool(db and db.incomplete)

    def page_property_items(self, page_id: str, property_id: str):
        self._count()
        if self.fail_relations:
            raise NotionError(502, "bad_gateway", "테스트용 실패")
        return [{"object": "property_item", "type": "relation", "relation": {"id": r}}
                for r in self.pages[page_id].relations]


class NoDownload:
    """이미지를 실제로 받지 않는 다운로더."""

    def download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"image")
