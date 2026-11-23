"""경로 변경 묶음, 반영과 되돌리기, 목록 파일(명세서 4.2절 6~7단계, 4.3절).

모든 파일 변경은 묶음(Bundle) 단위로 준비하고 반영한다.
- 같은 파일을 건드리는 변경, 옮겨지거나 지워지는 페이지와 그 페이지를 링크하는 파일,
  함께 옮겨지는 하위 페이지는 한 묶음으로 합친다.
- 묶음의 수정본을 모두 준비한 경우에만 반영한다. 준비하지 못하면 그 묶음의
  이동·삭제를 보류한다(호출한 쪽이 고정하고 경로를 다시 계산한다).
- 반영 도중 실패하면 그 묶음을 반영 직전 상태로 되돌린다.
"""

from __future__ import annotations

import posixpath
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .convert import resolve_links
from .ids import notion_url
from .lists import ListBuilder
from .naming import relative_link
from .pages import PageResult
from .paths import README, link_path
from .properties import front_matter, replace_front_matter, to_kst
from .state import Entry, State
from .tree import Node


class PrepareFailed(Exception):
    pass


@dataclass
class Op:
    kind: str  # write | rewrite | rename | delete | link
    page: str
    files: set[str] = field(default_factory=set)
    targets: set[str] = field(default_factory=set)  # 이 op로 경로가 바뀌거나 지워지는 노드
    rewrite_for: set[str] = field(default_factory=set)  # rewrite: 링크를 고칠 대상
    front_matter: bool = False  # rewrite: front matter도 다시 쓰기


@dataclass
class Bundle:
    ops: list[Op]
    writes: dict[str, bytes] = field(default_factory=dict)
    copies: dict[str, Path] = field(default_factory=dict)
    deletes: set[str] = field(default_factory=set)

    @property
    def targets(self) -> set[str]:
        out: set[str] = set()
        for op in self.ops:
            out |= op.targets
        return out

    @property
    def pages(self) -> set[str]:
        return {op.page for op in self.ops}


class _Union:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _folder(path: str) -> str:
    return posixpath.dirname(path)


def _img(page_path: str, file: str) -> str:
    folder = _folder(page_path)
    return f"{folder}/{file}" if folder else file


class Sync:
    def __init__(self, notes: Path, nodes: dict[str, Node], state: State, results: dict[str, PageResult]):
        self.notes = notes
        self.nodes = nodes
        self.state = state
        self.results = results
        self.paths: dict[str, str] = {}
        self.present: set[str] = set()  # 반영 뒤 파일이 있을 페이지
        self.deletions: set[str] = set()  # 확인된 삭제
        self.props_changed: set[str] = set()

    # ================================================================ 보조
    def entry(self, node_id: str) -> Entry | None:
        return self.state.entries.get(node_id)

    def moved(self, node_id: str) -> bool:
        entry = self.entry(node_id)
        return bool(entry and node_id in self.paths and entry.path != self.paths[node_id])

    def renamable(self, page_id: str) -> bool:
        """다시 받지 않고 기존 파일을 옮겨 이름만 바꿀 수 있는지.

        같은 폴더 안에서 파일 이름만 바뀌는 일반 페이지(하위 폴더가 없는 것)는 본문의 상대 링크가
        그대로 맞으므로, 다른 대상으로 가는 링크만 고쳐 옮기면 된다.
        """
        entry = self.entry(page_id)
        new = self.paths.get(page_id)
        if not entry or entry.kind != "page" or not new or entry.path == new:
            return False
        if posixpath.basename(entry.path) == README or posixpath.basename(new) == README:
            return False
        return posixpath.dirname(entry.path) == posixpath.dirname(new)

    def old_link_path(self, node_id: str) -> str | None:
        entry = self.entry(node_id)
        if not entry:
            return None
        return entry.path if entry.kind == "page" else posixpath.join(entry.path, README)

    def new_link_path(self, node_id: str) -> str | None:
        if node_id in self.deletions:
            return None
        node = self.nodes.get(node_id)
        path = self.paths.get(node_id)
        if not node or not path:
            return None
        if node.kind == "page" and node_id not in self.present:
            return None
        return link_path(node, path)

    def target_text(self, node_id: str, from_file: str, old: bool = False) -> str:
        path = self.old_link_path(node_id) if old else self.new_link_path(node_id)
        return relative_link(path, from_file) if path else notion_url(node_id)

    def links_of(self, page_id: str) -> set[str]:
        if page_id in self.results:
            return set(self.results[page_id].rendered.links)
        entry = self.entry(page_id)
        return set(entry.links) if entry else set()

    def images_of(self, page_id: str) -> list[str]:
        if page_id in self.results:
            return [f"images/{i.name}" for i in self.results[page_id].images]
        entry = self.entry(page_id)
        return [i.file for i in entry.images] if entry else []

    def referenced_images(self) -> set[str]:
        out: set[str] = set()
        for page_id in self.present:
            path = self.paths.get(page_id)
            if path:
                out |= {_img(path, f) for f in self.images_of(page_id)}
        return out

    def page_text(self, page_id: str) -> str:
        node = self.nodes[page_id]
        path = self.paths[page_id]
        meta = {
            "notion_id": page_id,
            "notion_url": node.url or notion_url(page_id),
            "created": to_kst(node.created),
            "last_edited": to_kst(node.last_edited),
        }
        body = resolve_links(self.results[page_id].rendered.body, lambda t: self.target_text(t, path))
        return front_matter(meta, node.props) + body

    def page_front_matter(self, page_id: str) -> str:
        node = self.nodes[page_id]
        meta = {
            "notion_id": page_id,
            "notion_url": node.url or notion_url(page_id),
            "created": to_kst(node.created),
            "last_edited": to_kst(node.last_edited),
        }
        return front_matter(meta, node.props)

    # ================================================================ 묶음 만들기
    def build_bundles(self) -> list[Bundle]:
        ops: list[Op] = []
        by_page: dict[str, int] = {}

        def add(op: Op) -> int:
            ops.append(op)
            return len(ops) - 1

        # 경로가 바뀌는 대상: 옮겨지는 페이지·데이터베이스·데이터 소스, 확인된 삭제
        targets: set[str] = set(self.deletions)
        for node_id, node in self.nodes.items():
            if node.frozen:
                continue
            if node.kind == "page" and node_id not in self.present:
                continue
            if self.moved(node_id):
                targets.add(node_id)

        # 렌더링한 페이지: 새 본문 쓰기(옮겨지면 이전 파일 지우기)
        for page_id in self.results:
            if page_id not in self.present or page_id not in self.paths:
                continue
            path = self.paths[page_id]
            op = Op("write", page_id, files={path})
            op.files |= {_img(path, f) for f in self.images_of(page_id)}
            entry = self.entry(page_id)
            if entry:
                if entry.path != path:
                    op.files.add(entry.path)
                    op.targets.add(page_id)
                op.files |= {_img(entry.path, i.file) for i in entry.images}
            by_page[page_id] = add(op)

        # 확인된 삭제
        for node_id in self.deletions:
            entry = self.entry(node_id)
            if not entry:
                continue
            op = Op("delete" if entry.kind == "page" else "link", node_id, targets={node_id})
            if entry.kind == "page":
                op.files = {entry.path} | {_img(entry.path, i.file) for i in entry.images}
            by_page[node_id] = add(op)

        # 옮겨지는 데이터베이스·데이터 소스: 파일은 목록 단계에서 다루고, 여기서는 링크만
        for node_id in targets:
            node = self.nodes.get(node_id)
            if node is not None and node.kind != "page" and node_id not in by_page:
                by_page[node_id] = add(Op("link", node_id, targets={node_id}))

        # 다시 렌더링하지 않는 페이지: 링크 고치기, front matter 다시 쓰기
        for page_id in self.present:
            if page_id in self.results or page_id not in self.paths:
                continue
            fix = self.links_of(page_id) & targets
            fm = page_id in self.props_changed
            if self.moved(page_id) and self.renamable(page_id):
                old = self.entry(page_id).path
                op = Op("rename", page_id, files={old, self.paths[page_id]}, targets={page_id},
                        rewrite_for=fix, front_matter=fm)
                by_page[page_id] = add(op)
            elif fix or fm:
                op = Op("rewrite", page_id, files={self.paths[page_id]}, rewrite_for=fix, front_matter=fm)
                by_page[page_id] = add(op)

        # 합치기
        uf = _Union(len(ops))
        owner: dict[str, int] = {}
        for i, op in enumerate(ops):
            for f in op.files:
                key = f.lower()
                if key in owner:
                    uf.union(owner[key], i)
                else:
                    owner[key] = i
        for i, op in enumerate(ops):
            for target in targets:
                if target in self.links_of(op.page) and target in by_page:
                    uf.union(i, by_page[target])
            node = self.nodes.get(op.page)
            parent = node.parent if node else (self.entry(op.page).parent if self.entry(op.page) else None)
            if op.targets and parent in targets and parent in by_page:
                uf.union(i, by_page[parent])
        groups: dict[int, list[Op]] = {}
        for i, op in enumerate(ops):
            groups.setdefault(uf.find(i), []).append(op)
        return [Bundle(ops=g) for g in groups.values()]

    # ================================================================ 준비
    def prepare(self, bundle: Bundle, referenced: set[str]) -> None:
        """묶음의 수정본을 모두 만든다. 하나라도 못 만들면 PrepareFailed."""
        writes: dict[str, bytes] = {}
        copies: dict[str, Path] = {}
        deletes: set[str] = set()
        image_candidates: set[str] = set()
        bundle_targets = bundle.targets
        for op in bundle.ops:
            entry = self.entry(op.page)
            if op.kind == "write":
                path = self.paths[op.page]
                writes[path] = self.page_text(op.page).encode("utf-8")
                for img in self.results[op.page].images:
                    copies[_img(path, f"images/{img.name}")] = img.local
                if entry:
                    if entry.path != path:
                        deletes.add(entry.path)
                    image_candidates |= {_img(entry.path, i.file) for i in entry.images}
            elif op.kind == "delete" and entry:
                deletes.add(entry.path)
                image_candidates |= {_img(entry.path, i.file) for i in entry.images}
            elif op.kind in ("rewrite", "rename"):
                path = self.paths[op.page]
                source = entry.path if op.kind == "rename" else path  # 이름 바꾸기: 이전 파일을 읽어 새 이름으로 쓴다
                file = self.notes / source
                if not file.is_file():
                    raise PrepareFailed(f"{source}: 파일이 없어 링크를 고치거나 옮길 수 없음")
                if op.kind == "rename":
                    deletes.add(source)
                text = file.read_text(encoding="utf-8")
                for target in sorted(op.rewrite_for & bundle_targets):
                    old = self.target_text(target, path, old=True)
                    new = self.target_text(target, path)
                    if old == new:
                        continue
                    needle = f"]({old})"
                    if needle not in text:
                        raise PrepareFailed(f"{path}: {target}로 가는 링크를 찾지 못함")
                    text = text.replace(needle, f"]({new})")
                if op.front_matter:
                    replaced = replace_front_matter(text, self.page_front_matter(op.page))
                    if replaced is None:
                        raise PrepareFailed(f"{path}: front matter를 찾지 못함")
                    text = replaced
                writes[path] = text.encode("utf-8")
        deletes |= {p for p in image_candidates if p not in referenced}
        deletes -= set(writes)
        deletes -= set(copies)
        bundle.writes, bundle.copies, bundle.deletes = writes, copies, deletes

    # ================================================================ 반영
    def apply(self, bundle: Bundle) -> None:
        """묶음을 반영한다. 도중에 실패하면 반영 직전 상태로 되돌리고 예외를 다시 낸다."""
        touched = set(bundle.writes) | set(bundle.copies) | set(bundle.deletes)
        snapshot: dict[str, bytes | None] = {}
        for rel in touched:
            path = self.notes / rel
            snapshot[rel] = path.read_bytes() if path.is_file() else None
        try:
            for rel, data in bundle.writes.items():
                _write(self.notes / rel, data)
            for rel, src in bundle.copies.items():
                dest = self.notes / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            for rel in bundle.deletes:
                path = self.notes / rel
                if path.is_file():
                    path.unlink()
        except Exception:
            for rel, data in snapshot.items():
                path = self.notes / rel
                if data is None:
                    if path.is_file():
                        path.unlink()
                else:
                    _write(path, data)
            remove_empty_dirs(self.notes)
            raise

    # ================================================================ 목록 파일
    def list_builder(self, planner, has_file) -> ListBuilder:
        return ListBuilder(self.nodes, self.paths, planner.csv_names, planner.single_source, has_file)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-write")
    tmp.write_bytes(data)
    tmp.replace(path)


def write_text(path: Path, text: str, bom: bool = False) -> None:
    _write(path, (("﻿" if bom else "") + text).encode("utf-8"))


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            next(folder.iterdir())
        except StopIteration:
            folder.rmdir()
        except OSError:
            pass
