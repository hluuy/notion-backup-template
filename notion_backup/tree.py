"""탐색: Search, 최상위 정하기, 하위 따라가기, 데이터베이스, 순환 재탐색(명세서 4.2절 1단계)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .client import NotionClient, NotionError
from .config import Config
from .deadline import Deadline, DeadlineReached
from .ids import normalize_id, notion_url
from .log import log, minutes, short
from .properties import format_value, front_matter_keys, plain_text, props_hash, read_front_matter
from .state import Entry, State

PARTIAL_MARK = "…"  # 관계형 속성을 끝까지 받지 못한 값의 끝 표시(CSV에만 나타난다)

RESCAN_MAX = 5 * 60
RESCAN_PRIORITY = 2 * 60
RESCAN_AGE = timedelta(days=14)  # 이보다 오래되면 변경 처리보다 먼저 다시 훑는다(우선 몫)
RESCAN_MIN_AGE = timedelta(days=7)  # 여유 몫도 이보다 최근에 확인한 페이지는 건너뛴다(매일 상태 파일이 바뀌지 않게)


@dataclass
class Node:
    id: str
    kind: str  # page | database | data_source
    title: str
    parent: str | None
    root: str
    created: str | None = None
    last_edited: str | None = None
    url: str | None = None
    children: list[str] = field(default_factory=list)
    props: dict | None = None
    props_hash: str | None = None
    description: str = ""
    columns: list[str] = field(default_factory=list)
    list_status: str = "ok"  # 데이터 소스: ok | error | time
    frozen: bool = False  # 이번 실행에서 손대지 않고 이전 상태를 그대로 두는 노드
    sort_prop: str | None = None  # 데이터 소스: 항목 정렬과 파일 이름 앞 날짜에 쓸 날짜 속성
    date_prefix: bool = False  # 데이터 소스: 항목 파일 이름 앞에 날짜를 붙일지


class Worker(Protocol):
    def needs_render(self, node: Node, entry: Entry | None) -> bool: ...
    def render(self, node: Node) -> list[tuple[str, str]] | None: ...
    def fetch_children(self, page_id: str) -> list[tuple[str, str]]: ...


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain_text(prop.get("title"))
    return ""


def _trashed(obj: dict) -> bool:
    return bool(obj.get("in_trash") or obj.get("archived"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class Discovery:
    def __init__(self, client: NotionClient, cfg: Config, state: State, deadline: Deadline, worker: Worker, full_rebuild: bool):
        self.client = client
        self.cfg = cfg
        self.state = state
        self.deadline = deadline
        self.worker = worker
        self.full_rebuild = full_rebuild
        self.search_map: dict[str, dict] = {}
        self.nodes: dict[str, Node] = {}
        self.roots: list[str] = []
        self.roots_before_exclude = 0
        self.excluded: set[str] = set()
        self.unreadable: set[str] = set()
        self.incomplete: dict[str, str] = {}  # root -> error | time
        self.queue_left: list[str] = []
        self.queue_items: list[dict] = []  # 상태 파일에 저장할 대기열(다음 실행이 이어 받는다)
        self.resumed: int = 0
        self.props_partial: set[str] = set()  # 관계형 속성을 끝까지 받지 못하고 이전 값도 없는 항목
        self.notes = getattr(worker, "notes", None)
        self.children_at: dict[str, str] = {}  # 이번 실행에서 하위 목록을 받은 페이지 -> 시각
        self.rescan_next: set[str] = set()
        self.errors: list[str] = []
        self._page_cache: dict[str, dict | None] = {}
        self._block_page: dict[str, str | None] = {}
        self._titles: dict[str, str] = {}
        self._item_objects: dict[str, dict] = {}
        self._pending_kinds: dict[str, str] = {}
        self._root_kinds: dict[str, str] = {}

    # ================================================================ 공용 조회
    def title_of(self, page_id: str) -> str:
        if page_id in self.nodes:
            return self.nodes[page_id].title
        if page_id in self._titles:
            return self._titles[page_id]
        if page_id in self.search_map:
            return self._title_from(self.search_map[page_id])
        entry = self.state.entries.get(page_id)
        if entry and entry.title:
            return entry.title
        page = self._page(page_id)
        title = page_title(page) if page else "링크된 페이지"
        self._titles[page_id] = title
        return title

    def _known_title(self, page_id: str) -> str:
        """API를 부르지 않고 아는 제목만 돌려준다."""
        if page_id in self.nodes:
            return self.nodes[page_id].title
        if page_id in self._titles:
            return self._titles[page_id]
        if page_id in self.search_map:
            return self._title_from(self.search_map[page_id])
        entry = self.state.entries.get(page_id)
        return entry.title if entry and entry.title else "링크된 페이지"

    @staticmethod
    def _title_from(obj: dict) -> str:
        if obj.get("object") == "data_source" or obj.get("object") == "database":
            return plain_text(obj.get("title"))
        return page_title(obj)

    def _page(self, page_id: str) -> dict | None:
        if page_id in self.search_map and self.search_map[page_id].get("object") == "page":
            return self.search_map[page_id]
        if page_id not in self._page_cache:
            try:
                self._page_cache[page_id] = self.client.retrieve_page(page_id)
            except NotionError as exc:
                if exc.status in (403, 404):
                    self._page_cache[page_id] = None
                else:
                    raise
        return self._page_cache[page_id]

    def _page_of_block(self, block_id: str) -> str | None:
        """블록의 부모를 따라 올라가 처음 만나는 페이지나 데이터베이스(명세서 4.2절 1-6단계)."""
        seen = []
        current = block_id
        while current:
            if current in self._block_page:
                result = self._block_page[current]
                break
            seen.append(current)
            try:
                block = self.client.retrieve_block(current)
            except NotionError as exc:
                if exc.status in (403, 404):
                    result = None
                    break
                raise
            parent = block.get("parent") or {}
            ptype = parent.get("type")
            if ptype in ("page_id", "database_id", "data_source_id"):
                result = normalize_id(parent.get(ptype))
                break
            if ptype == "block_id":
                current = normalize_id(parent.get("block_id"))
                continue
            result = None
            break
        else:
            result = None
        for b in seen:
            self._block_page[b] = result
        return result

    def _parent_of(self, obj: dict) -> tuple[str | None, str]:
        """(부모 ID, 부모 종류). 블록 부모는 페이지까지 올라간다. 워크스페이스면 (None, 'workspace')."""
        parent = obj.get("parent") or {}
        ptype = parent.get("type")
        if ptype == "workspace":
            return None, "workspace"
        if ptype == "block_id":
            return self._page_of_block(normalize_id(parent.get("block_id"))), "page"
        if ptype == "page_id":
            return normalize_id(parent.get("page_id")), "page"
        if ptype == "data_source_id":
            return normalize_id(parent.get("data_source_id")), "data_source"
        if ptype == "database_id":
            return normalize_id(parent.get("database_id")), "database"
        return None, "workspace"

    def _readable(self, target: str | None, kind: str) -> bool:
        if not target:
            return False
        if target in self.search_map:
            return True
        try:
            if kind == "database":
                self.client.retrieve_database(target)
            elif kind == "data_source":
                self.client.retrieve_data_source(target)
            else:
                return self._page(target) is not None
            return True
        except NotionError as exc:
            if exc.status in (403, 404):
                return False
            raise

    # ================================================================ 1-1 Search
    def search(self) -> None:
        for obj in self.client.search():
            if _trashed(obj):
                continue
            obj_id = normalize_id(obj.get("id"))
            if obj_id:
                self.search_map[obj_id] = obj

    # ================================================================ 1-2 최상위
    def find_roots(self) -> None:
        candidates: list[tuple[str, str]] = []
        databases: dict[str, None] = {}
        for obj_id, obj in self.search_map.items():
            if obj.get("object") == "data_source":
                db_id, _ = self._parent_of(obj)
                if db_id:
                    databases[db_id] = None
                continue
            parent, kind = self._parent_of(obj)
            if parent is None or not self._readable(parent, kind):
                candidates.append((obj_id, "page"))
        for db_id in databases:
            try:
                db = self.client.retrieve_database(db_id)
            except NotionError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            parent, kind = self._parent_of(db)
            if parent is None or not self._readable(parent, kind):
                candidates.append((db_id, "database"))
        for root_id in self.cfg.roots:
            candidates.append((root_id, self._kind_guess(root_id)))
        for root_id in self.state.roots:
            entry = self.state.entries.get(root_id)
            candidates.append((root_id, entry.kind if entry else "page"))

        seen: set[str] = set()
        for root_id, kind in candidates:
            if root_id in seen:
                continue
            seen.add(root_id)
            self._root_kinds[root_id] = kind
        self.roots_before_exclude = len(seen)
        self.roots = [r for r in seen if r not in self.cfg.exclude]

    def _kind_guess(self, obj_id: str) -> str:
        entry = self.state.entries.get(obj_id)
        if entry:
            return entry.kind
        obj = self.search_map.get(obj_id)
        if obj and obj.get("object") == "page":
            return "page"
        try:
            self.client.retrieve_database(obj_id)
            return "database"
        except NotionError:
            return "page"

    # ================================================================ 1-3 순환 재탐색
    def _rescan_candidates(self, older_than: datetime) -> list[str]:
        out = []
        for page_id, entry in self.state.entries.items():
            if entry.kind != "page" or page_id in self.children_at or page_id in self.excluded:
                continue
            at = entry.children_at
            when = datetime.fromisoformat(at) if at else datetime.min.replace(tzinfo=timezone.utc)
            if when < older_than:
                out.append((when, page_id))
        return [p for _, p in sorted(out)]

    def rescan(self, seconds: float, older_than: datetime) -> float:
        """하위 목록을 받은 지 오래된 페이지부터 다시 받는다. 쓴 시간을 돌려준다."""
        if seconds <= 0:
            return 0.0
        start = self.deadline.remaining()
        budget = self.deadline.sub(seconds)
        candidates = self._rescan_candidates(older_than)
        if candidates:
            log(f"순환 재탐색: 하위 목록을 오래 확인하지 않은 페이지 {len(candidates)}개 (최대 {int(seconds)}초)")
        for page_id in candidates:
            if budget.passed():
                break
            try:
                children = self.worker.fetch_children(page_id)
            except DeadlineReached:
                break
            except NotionError:
                continue
            entry = self.state.entries[page_id]
            new_ids = [c for c, _ in children]
            for child_id, kind in children:
                if child_id not in self.state.entries:
                    self._pending_kinds[child_id] = kind
            entry.children = new_ids
            entry.children_at = _iso(_now())
            self.children_at[page_id] = entry.children_at
            node = self.nodes.get(page_id)
            if node is not None:
                node.children = new_ids
        return start - self.deadline.remaining()

    # ================================================================ 1-3 ~ 1-7 탐색
    def run(self) -> None:
        log("노션 검색(Search) 중…")
        self.search()
        pages = sum(1 for o in self.search_map.values() if o.get("object") == "page")
        log(f"검색 결과: 페이지 {pages}개, 데이터 소스 {len(self.search_map) - pages}개")
        self.find_roots()
        log(f"최상위 {len(self.roots)}개: " + ", ".join(short(self._known_title(r), 30) for r in self.roots[:10])
            + (" …" if len(self.roots) > 10 else ""))
        used = self.rescan(RESCAN_PRIORITY, _now() - RESCAN_AGE)
        if self.state.queue:
            log(f"지난 실행이 남긴 대기열 {len(self.state.queue)}개")
        log(f"탐색 시작 (남은 시간 {minutes(self.deadline.remaining())})")
        self.traverse()
        self.attach_missing()
        if not self.deadline.passed():
            self.rescan(RESCAN_MAX - used, _now() - RESCAN_MIN_AGE)
        self.fill_rows()
        log(f"탐색 끝: 노드 {len(self.nodes)}개, 남은 대기열 {len(self.queue_left)}개"
            + (f", 미완료 범위 {len(self.incomplete)}개" if self.incomplete else "")
            + f" (남은 시간 {minutes(self.deadline.remaining())})")

    def _under_excluded(self, obj_id: str | None) -> bool:
        seen: set[str] = set()
        current = obj_id
        while current and current not in seen:
            if current in self.cfg.exclude:
                return True
            seen.add(current)
            entry = self.state.entries.get(current)
            current = entry.parent if entry else None
        return False

    def _resume_items(self) -> list[tuple[str, str, str | None, str]]:
        """지난 실행이 남긴 탐색 대기열. 그사이 옮겨졌거나 범위에서 빠진 페이지는 건너뛴다
        (최상위부터 따라가는 탐색이 제자리에서 다시 찾는다)."""
        out = []
        roots = set(self.roots)
        for item in self.state.queue:
            if not isinstance(item, dict) or item.get("kind") != "page":
                continue
            obj_id, parent, root = item.get("id"), item.get("parent"), item.get("root")
            if not obj_id or root not in roots or self._under_excluded(obj_id) or self._under_excluded(parent):
                continue
            obj = self.search_map.get(obj_id)
            if obj is None or obj.get("object") != "page" or _trashed(obj):
                continue
            if self._parent_of(obj)[0] != parent:
                continue
            out.append((obj_id, "page", parent, root))
        return out

    def _kind_of(self, obj_id: str) -> str:
        entry = self.state.entries.get(obj_id)
        if entry:
            return entry.kind
        return self._pending_kinds.get(obj_id, "page")

    def traverse(self, start: list[tuple[str, str, str | None, str]] | None = None) -> None:
        resumed: list[tuple[str, str, str | None, str]] = []
        if start is None:
            # 지난 실행이 남긴 대기열부터 이어 받고, 그다음 최상위부터 따라간다.
            resumed = self._resume_items()
            self.resumed = len(resumed)
            start = resumed + [(r, self._root_kinds.get(r, "page"), None, r) for r in self.roots]
        queue: deque[tuple[str, str, str | None, str]] = deque(start)
        try:
            self._drain(queue)
        finally:
            # 이어 받은 페이지를 부모의 하위 목록에 붙인다(부모를 다시 렌더링하지 않았을 수 있다).
            for obj_id, _, parent, _ in resumed:
                parent_node = self.nodes.get(parent) if parent else None
                if obj_id in self.nodes and parent_node is not None and obj_id not in parent_node.children:
                    parent_node.children.append(obj_id)

    def _drain(self, queue: deque[tuple[str, str, str | None, str]]) -> None:
        while queue:
            item = queue[0]
            obj_id, kind, parent, root = item
            if obj_id in self.nodes or obj_id in self.excluded or obj_id in self.unreadable:
                queue.popleft()
                continue
            if self.incomplete.get(root) == "error":
                queue.popleft()
                continue
            if self.deadline.passed():
                break
            try:
                children = self._process(obj_id, kind, parent, root)
            except DeadlineReached:
                log(f"시간 예산이 끝나 탐색을 멈춤 (남은 대기열 {len(queue)}개, 다음 실행에서 이어서)")
                break
            except NotionError as exc:
                if exc.status == 401:
                    raise
                self.errors.append(f"{obj_id}: {exc}")
                self.incomplete[root] = "error"
                log(f"  오류: {short(self._known_title(obj_id))} ({exc.status}) — 이 최상위 범위는 이번에 파일을 바꾸지 않음")
                queue.popleft()
                continue
            queue.popleft()
            for child_id, child_kind in children:
                queue.append((child_id, child_kind, obj_id, root))
        for index, (obj_id, kind, parent, root) in enumerate(queue):
            if obj_id in self.queue_left:
                continue
            if index > 0 and obj_id in self.nodes:
                continue  # 이미 처리한 항목. 맨 앞 항목만은 처리 도중 시간 예산에 걸린 것이라 노드가 있어도 남긴다
            if root not in self.incomplete:
                self.incomplete[root] = "time"
            self.queue_left.append(obj_id)
            self.queue_items.append({"id": obj_id, "kind": kind, "parent": parent, "root": root})

    def _process_page(self, page_id: str, parent: str | None, root: str, item: dict | None) -> list[tuple[str, str]]:
        page = item or self._page(page_id)
        if page is None:
            self.unreadable.add(page_id)
            return []
        if _trashed(page):
            return []
        node = Node(
            id=page_id, kind="page", title=page_title(page), parent=parent, root=root,
            created=page.get("created_time"), last_edited=page.get("last_edited_time"),
            url=page.get("url") or notion_url(page_id),
        )
        entry = self.state.entries.get(page_id)
        if item is not None:
            node.props, node.props_hash = self._item_props(page)
        partial = page_id in self.props_partial
        if partial:
            # 관계형 속성을 끝까지 받지 못했고 이전 값도 없다: 잘린 값으로 쓰지 않고 다음 실행에서 다시 시도한다.
            node.props_hash = entry.props_hash if entry else None
        self.nodes[page_id] = node
        # 렌더링이 시간 예산에 걸려도 이전 하위 목록을 유지하도록 먼저 채운다.
        node.children = list(entry.children) if entry else []
        children: list[tuple[str, str]] | None = None
        if partial and self.worker.needs_render(node, entry):
            self.worker.failures[page_id] = "관계형 속성을 끝까지 받지 못함(다음 실행에서 다시 시도)"
        elif self.worker.needs_render(node, entry):
            children = self.worker.render(node)
            if children is not None:
                self.children_at[page_id] = _iso(_now())
        if children is None:
            children = [(c, self._kind_of(c)) for c in node.children]
        node.children = [c for c, _ in children]
        return children

    def _process_database(self, db_id: str, parent: str | None, root: str) -> list[tuple[str, str]]:
        try:
            db = self.client.retrieve_database(db_id)
        except NotionError as exc:
            if exc.status in (403, 404):
                self.unreadable.add(db_id)
                return []
            raise
        if _trashed(db):
            return []
        sources = [normalize_id(s.get("id")) for s in db.get("data_sources") or [] if s.get("id")]
        node = Node(
            id=db_id, kind="database", title=plain_text(db.get("title")), parent=parent, root=root,
            created=db.get("created_time"), last_edited=db.get("last_edited_time"),
            url=db.get("url") or notion_url(db_id), description=plain_text(db.get("description")),
            children=sources,
        )
        self.nodes[db_id] = node
        return [(s, "data_source") for s in sources]

    def _process_data_source(self, ds_id: str, parent: str | None, root: str) -> list[tuple[str, str]]:
        ds = self.client.retrieve_data_source(ds_id)
        schema = ds.get("properties") or {}
        title_prop = next((name for name, p in schema.items() if p.get("type") == "title"), None)
        columns = ([title_prop] if title_prop else []) + sorted(n for n in schema if n != title_prop)
        node = Node(
            id=ds_id, kind="data_source", title=plain_text(ds.get("title")), parent=parent, root=root,
            created=ds.get("created_time"), last_edited=ds.get("last_edited_time"), columns=columns,
            sort_prop=self._date_property(schema, parent), date_prefix=self.cfg.date_prefix,
        )
        self.nodes[ds_id] = node
        # 목록 먼저, 렌더링은 나중(명세서 4.2절 1-4단계)
        log(f"  데이터 소스 목록 받는 중: {short(node.title)}")
        try:
            items, incomplete = self.client.query_data_source(ds_id)
        except DeadlineReached:
            node.list_status = "time"
            node.frozen = True
            log(f"  데이터 소스 목록을 받다가 시간 예산이 끝남: {short(node.title)}")
            raise
        if incomplete:
            node.list_status = "error"
            node.frozen = True
            log(f"  데이터 소스 목록이 불완전(incomplete): {short(node.title)} — 기존 목록 유지")
            return []
        log(f"  데이터 소스 목록 {len(items)}개: {short(node.title)}")
        children: list[tuple[str, str]] = []
        for item in items:
            if _trashed(item):
                continue
            item_id = normalize_id(item.get("id"))
            self.search_map.setdefault(item_id, item)
            self._item_objects[item_id] = item
            children.append((item_id, "item"))
        node.children = [c for c, _ in children]
        return children

    def _date_property(self, schema: dict, db_id: str | None) -> str | None:
        """항목 정렬과 파일 이름 앞 날짜에 쓸 날짜 속성.

        config.yml에서 지정한 속성 → 이름이 '날짜'·'Date'·'일자'인 속성 → 이름순 첫 날짜 속성.
        """
        dates = sorted(name for name, p in schema.items() if p.get("type") == "date")
        chosen = self.cfg.date_property.get(db_id or "")
        if chosen:
            if chosen in dates:
                return chosen
            self.errors.append(f"config.yml의 날짜 속성 '{chosen}'이 데이터베이스에 없거나 날짜 속성이 아님")
        for name in dates:
            if name.strip().lower() in ("날짜", "date", "일자"):
                return name
        return dates[0] if dates else None

    def _item_props(self, page: dict, expand: bool = True) -> tuple[dict, str]:
        """데이터 소스 항목의 속성 값. 관계형 속성을 끝까지 받지 못하면 이전 백업 파일의 값을 쓰고,
        이전 값도 없으면 잘린 값 끝에 표시를 붙이고 props_partial에 넣는다."""
        page_id = normalize_id(page["id"])
        props = page.get("properties") or {}
        names = sorted(n for n, p in props.items() if p.get("type") != "title")
        title_of = self.title_of if expand else self._known_title  # expand=False: API를 부르지 않는다
        values: dict = {}
        old: dict | None = None
        for name in names:
            prop = props[name]
            if prop.get("type") == "relation" and prop.get("has_more"):
                full = None
                if expand:
                    try:
                        items = self.client.page_property_items(page_id, prop.get("id"))
                        full = {"type": "relation", "relation": [i.get("relation") for i in items if i.get("relation")]}
                    except NotionError as exc:
                        if exc.status == 401:
                            raise
                        self.errors.append(f"{page_id}: 관계형 속성 '{name}' 조회 실패: {exc}")
                if full is None:
                    if old is None:
                        old = self._old_props(page_id, names)
                    if name in old:
                        values[name] = old[name]
                        continue
                    self.props_partial.add(page_id)
                    values[name] = list(format_value(prop, title_of) or []) + [PARTIAL_MARK]
                    continue
                prop = full
            values[name] = format_value(prop, title_of)
        return values, props_hash(values)

    def _old_props(self, page_id: str, names: list[str]) -> dict:
        """이전에 백업한 파일의 front matter에서 속성 값을 읽는다."""
        entry = self.state.entries.get(page_id)
        if not entry or self.notes is None:
            return {}
        try:
            fm = read_front_matter((self.notes / entry.path).read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError):
            return {}
        keys = front_matter_keys(names)
        return {name: fm[key] for name, key in keys.items() if key in fm}

    # 데이터 소스 항목("item")은 목록 조회로 이미 받은 페이지 객체로 처리한다.
    def _process(self, obj_id: str, kind: str, parent: str | None, root: str) -> list[tuple[str, str]]:
        if obj_id in self.cfg.exclude:
            self.excluded.add(obj_id)
            return []
        if kind == "database":
            return self._process_database(obj_id, parent, root)
        if kind == "data_source":
            return self._process_data_source(obj_id, parent, root)
        if kind == "item":
            return self._process_page(obj_id, parent, root, self._item_objects.get(obj_id))
        return self._process_page(obj_id, parent, root, None)

    # ================================================================ 1-5 누락 보완
    def attach_missing(self) -> None:
        extra: list[tuple[str, str, str | None, str]] = []
        for obj_id, obj in self.search_map.items():
            if obj.get("object") != "page" or obj_id in self.nodes or obj_id in self.excluded:
                continue
            parent, _ = self._parent_of(obj)
            if parent and parent in self.nodes and self.nodes[parent].kind == "page":
                parent_node = self.nodes[parent]
                if parent_node.root in self.incomplete:
                    continue
                if obj_id not in parent_node.children:
                    parent_node.children.append(obj_id)
                self.rescan_next.add(parent)
                extra.append((obj_id, "page", parent, parent_node.root))
        if extra and not self.deadline.passed():
            self.traverse(extra)

    # ================================================================ 목록 행 채우기
    def fill_rows(self) -> None:
        """목록을 끝까지 받은 데이터 소스의 항목 중 이번에 처리하지 못한 것도 노드로 만든다.

        CSV와 README 표는 조회한 전체 항목으로 만들고, 본문을 쓰지 못한 항목은 파일 경로만
        비운다(명세서 4.2절 1-4단계). 이전에 백업한 항목은 손대지 않는 노드(frozen)로 둔다.
        """
        for ds in [n for n in self.nodes.values() if n.kind == "data_source" and n.list_status == "ok"]:
            for item_id in ds.children:
                if item_id in self.nodes or item_id in self.cfg.exclude:
                    continue
                obj = self._item_objects.get(item_id)
                if obj is None:
                    continue
                entry = self.state.entries.get(item_id)
                node = Node(
                    id=item_id, kind="page", title=page_title(obj), parent=ds.id, root=ds.root,
                    created=obj.get("created_time"), last_edited=obj.get("last_edited_time"),
                    url=obj.get("url") or notion_url(item_id),
                    children=list(entry.children) if entry else [], frozen=entry is not None,
                )
                node.props, node.props_hash = self._item_props(obj, expand=False)
                self.nodes[item_id] = node

    # ================================================================ 결과
    def kept_from_state(self) -> dict[str, Entry]:
        """이번 실행에서 탐색하지 못했지만 지우면 안 되는 상태 항목(미완료 범위, 목록 미완료 데이터 소스)."""
        out: dict[str, Entry] = {}
        frozen_ds = {n.id for n in self.nodes.values() if n.kind == "data_source" and n.list_status != "ok"}
        for obj_id, entry in self.state.entries.items():
            if obj_id in self.nodes or obj_id in self.excluded:
                continue
            if entry.root in self.incomplete or obj_id in self.queue_left:
                out[obj_id] = entry
                continue
            parent = entry.parent
            while parent:
                if parent in frozen_ds:
                    out[obj_id] = entry
                    break
                pe = self.state.entries.get(parent)
                parent = pe.parent if pe else None
        return out
