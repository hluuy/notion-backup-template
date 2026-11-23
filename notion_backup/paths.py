"""저장 경로 계산(명세서 6.1~6.3절).

경로는 모두 `notes/` 기준이고 `/`로 구분한다. 페이지는 파일 경로, 데이터베이스와
데이터 소스는 폴더 경로를 돌려준다. 고정(pin)된 노드는 이전 경로를 그대로 쓰고,
그 이름은 같은 폴더의 다른 페이지보다 먼저 차지한 것으로 본다.
"""

from __future__ import annotations

import posixpath
import re
from typing import Callable

from .ids import compact
from .naming import IMAGE_MARGIN, MAX_PATH, sanitize, shorten, with_suffix
from .tree import Node

README = "README.md"
PREFIX = "notes/"


def page_dir(path: str) -> str:
    """페이지 파일 경로에서 그 페이지의 하위 항목이 들어갈 폴더."""
    if path.endswith("/" + README) or path == README:
        return posixpath.dirname(path)
    return path[:-3] if path.endswith(".md") else path


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def item_date(item: Node, ds: Node | None) -> str | None:
    """데이터 소스 항목의 정렬용 날짜(YYYY-MM-DD). 기간이면 시작일, 시각이 있으면 날짜 부분만."""
    if ds is None or not ds.sort_prop or not item.props:
        return None
    value = item.props.get(ds.sort_prop)
    if not isinstance(value, str):
        return None
    match = _DATE.match(value)
    return match.group(0) if match else None


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _stem(path: str) -> str:
    base = posixpath.basename(path.rstrip("/"))
    if base.lower() == README.lower():
        base = posixpath.basename(posixpath.dirname(path))
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base.lower()


class PathPlanner:
    def __init__(self, nodes: dict[str, Node], roots: list[str], pins: dict[str, str],
                 include: Callable[[str], bool] | None = None):
        self.nodes = nodes
        self.roots = roots
        self.pins = pins
        # 경로를 배정할 노드인지. 본문 파일이 없을 페이지(목록의 행만 있는 항목)는 빼서 이름을 차지하지 않게 한다.
        self.include = include or (lambda _id: True)
        self.paths: dict[str, str] = {}
        self.single_source: dict[str, bool] = {}
        self.csv_names: dict[str, str] = {}  # 데이터 소스 ID -> CSV 경로

    def _present_children(self, node_id: str) -> list[str]:
        node = self.nodes.get(node_id)
        if not node:
            return []
        return [c for c in node.children if c in self.nodes and self.include(c)]

    def plan(self) -> dict[str, str]:
        reserved = {".backup-state", "readme", "images"}
        self._assign("", [r for r in self.roots if r in self.nodes and self.include(r)], reserved)
        for node_id, path in self.pins.items():
            self.paths.setdefault(node_id, path)
        return self.paths

    # ------------------------------------------------------------------ 이름 배정
    def _assign(self, folder: str, ids: list[str], reserved: set[str]) -> None:
        used = set(reserved)
        for node_id in ids:
            if node_id in self.pins:
                used.add(_stem(self.pins[node_id]))
        ordered = sorted(
            (i for i in ids if i not in self.pins),
            key=lambda i: (self.nodes[i].created or "", i),
        )
        for node_id in ids:
            if node_id in self.pins:
                self._place_pinned(node_id)
        for node_id in ordered:
            node = self.nodes[node_id]
            is_folder = node.kind in ("database", "data_source") or bool(self._present_children(node_id))
            tail = (len("/" + README) + IMAGE_MARGIN) if is_folder else len(".md")
            name = self._choose(folder, node, used, tail)
            used.add(name.lower())
            if node.kind == "page":
                path = _join(_join(folder, name), README) if is_folder else _join(folder, name + ".md")
                self.paths[node_id] = path
                if is_folder:
                    self._assign(page_dir(path), self._present_children(node_id), {"readme", "images"})
            else:
                self._place_database(node, _join(folder, name))

    def _base_name(self, node: Node) -> str:
        """파일 이름의 바탕. 날짜를 붙이는 데이터 소스의 항목이면 `YYYY-MM-DD 제목`."""
        base = sanitize(node.title)
        parent = self.nodes.get(node.parent) if node.parent else None
        if node.kind == "page" and parent is not None and parent.kind == "data_source" and parent.date_prefix:
            date = item_date(node, parent)
            if date:
                return f"{date} {base}"
        return base

    def _choose(self, folder: str, node: Node, used: set[str], tail: int) -> str:
        base = self._base_name(node)
        prefix_len = len(PREFIX) + (len(folder) + 1 if folder else 0)
        fits = lambda name: prefix_len + len(name) + tail <= MAX_PATH  # noqa: E731
        if fits(base):
            candidates = [base] + [with_suffix(base, node.id, n) for n in (4, 8, 32)]
        else:
            short = shorten(base)
            candidates = [with_suffix(short, node.id, n) for n in (4, 8, 32)]
            if not fits(candidates[0]):
                # 폴더가 이미 깊어 40자로 줄여도 넘치면 ID만으로 이름을 짓는다.
                candidates = [compact(node.id)[:8], compact(node.id)]
        for name in candidates:
            if name.lower() not in used:
                return name
        return compact(node.id)

    def _place_pinned(self, node_id: str) -> None:
        node = self.nodes[node_id]
        path = self.pins[node_id]
        self.paths[node_id] = path
        if node.kind == "page":
            children = self._present_children(node_id)
            if children:
                self._assign(page_dir(path), children, {"readme", "images"})
        else:
            self._place_database(node, path, pinned=True)

    def _place_database(self, node: Node, folder: str, pinned: bool = False) -> None:
        self.paths[node.id] = folder
        if node.kind == "data_source":
            self._place_source(node, folder, single=True)
            return
        sources = self._present_children(node.id)
        single = len(sources) <= 1
        self.single_source[node.id] = single
        if single:
            for ds_id in sources:
                self._place_source(self.nodes[ds_id], folder, single=True, db_name=posixpath.basename(folder))
            return
        used = {"readme", "images", posixpath.basename(folder).lower()}
        for ds_id in sorted(sources, key=lambda i: (self.nodes[i].created or "", i)):
            ds = self.nodes[ds_id]
            if ds_id in self.pins:
                ds_folder = self.pins[ds_id]
            else:
                name = self._choose(folder, ds, used, len("/" + README) + IMAGE_MARGIN)
                ds_folder = _join(folder, name)
            used.add(posixpath.basename(ds_folder).lower())
            self._place_source(ds, ds_folder, single=False)

    def _place_source(self, ds: Node, folder: str, single: bool, db_name: str | None = None) -> None:
        self.paths[ds.id] = folder
        csv_stem = db_name if (single and db_name) else posixpath.basename(folder)
        self.csv_names[ds.id] = _join(folder, csv_stem + ".csv")
        self._assign(folder, self._present_children(ds.id), {"readme", "images", csv_stem.lower()})


def link_path(node: Node, path: str) -> str:
    """다른 페이지가 이 노드를 링크할 때 가리킬 파일."""
    if node.kind == "page":
        return path
    return _join(path, README)
