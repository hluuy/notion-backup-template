"""백업 한 번의 실행(명세서 4.2절 전체 흐름)."""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from .client import NotionError
from .config import Config
from .deadline import Deadline, DeadlineReached
from .images import Downloader
from .log import log, minutes
from .lists import ListBuilder
from .pages import PageWorker, entry_images
from .paths import README, PathPlanner
from .state import Entry, State, recover
from .summary import Summary
from .sync import Bundle, PrepareFailed, Sync, remove_empty_dirs, write_text
from .tree import Discovery, Node

MAX_PLAN_ROUNDS = 10


@dataclass
class Options:
    dry_run: bool = False
    full_rebuild: bool = False
    allow_mass_delete: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Backup:
    def __init__(self, client, cfg: Config, notes: Path, deadline: Deadline, options: Options,
                 downloader: Downloader | None = None, work: Path | None = None):
        self.client = client
        self.cfg = cfg
        self.notes = notes
        self.deadline = deadline
        self.opts = options
        self.summary = Summary()
        self.seen_children: dict[str, list[str]] = {}
        self._work_owned = work is None
        self.work = work or Path(tempfile.mkdtemp(prefix="notion-backup-"))
        self.downloader = downloader or Downloader(deadline, int(cfg.max_image_mb * 1024 * 1024))

    # ================================================================ 진입점
    def run(self) -> Summary:
        try:
            self._run()
        except NotionError as exc:
            if exc.status == 401:
                self.summary.raise_to("abort", "노션 토큰이 없거나 틀렸습니다. NOTION_TOKEN과 통합을 확인하세요.")
            else:
                self.summary.raise_to("abort", f"노션 API 오류로 중단: {exc}")
        finally:
            if self._work_owned:
                shutil.rmtree(self.work, ignore_errors=True)
        return self.summary

    def _run(self) -> None:
        s = self.summary
        state = State.load(self.notes)
        if state is None:
            state, no_id, orphans = recover(self.notes)
            s.no_id_files, s.orphan_images = no_id, orphans
        self.state = state
        if self.opts.full_rebuild and not state.full_rebuild_since:
            state.full_rebuild_since = _now_iso()
        pages_before = sum(1 for e in state.entries.values() if e.kind == "page")
        log(f"시작: 기존 백업 페이지 {pages_before}개"
            + (" (상태 파일이 없어 파일에서 복구함)" if state.recovered else "")
            + (" · full_rebuild" if state.full_rebuild_since else "")
            + (" · dry-run" if self.opts.dry_run else "")
            + f" · 시간 예산 {minutes(self.deadline.remaining())}")

        worker = PageWorker(self.client, state, self.notes, self.work, self.downloader, state.full_rebuild_since)
        disc = Discovery(self.client, self.cfg, state, self.deadline, worker, self.opts.full_rebuild)
        worker.title_of = disc.title_of
        worker.page_of_block = disc._page_of_block
        self.worker, self.disc = worker, disc

        try:
            disc.run()
        except NotionError as exc:
            log(f"중단: 노션 API 오류 {exc.status}")
            raise
        except DeadlineReached:
            s.raise_to("abort", "탐색을 시작하기 전에 시간 예산을 다 썼습니다.")
            return
        s.requests = self.client.request_count
        log(f"받은 페이지 {len(worker.results)}개, 실패 {len(worker.failures)}개, API 요청 {self.client.request_count}회")

        # 빈 목록 차단(제외 적용 전 기준)
        if disc.roots_before_exclude == 0:
            s.raise_to("abort", "탐색 결과가 0개입니다. 노션 페이지에 통합이 연결되어 있는지 확인하세요.")
            return

        nodes = disc.nodes
        self.nodes = nodes
        self._render_dependents()
        self._drop_error_roots()
        deletions, kept = self._judge_deletions()
        if s.result == "abort":
            log(f"중단: {'; '.join(s.reasons)}")
            return
        if deletions:
            log(f"삭제 확인: {len(deletions)}개")
        self._freeze(kept)
        self._prune_unwritten()
        pins = self._initial_pins()

        sync = Sync(self.notes, nodes, state, worker.results)
        sync.deletions = set(deletions)
        sync.present = self._present_pages(sync.deletions)
        sync.props_changed = self._props_changed()
        self.sync = sync

        log("경로 계산과 변경 준비 중…")
        planner, bundles, keep_lists = self._plan(sync, pins)
        s.requests = self.client.request_count
        self._report_pages()
        if s.result == "abort":
            log(f"중단: {'; '.join(s.reasons)}")
            return

        if self.opts.dry_run:
            self._report_plan(sync)
            return

        log(f"파일에 반영 중: 변경 묶음 {len(bundles)}개")
        applied_pages, rolled_back = self._apply(sync, bundles)
        if s.result == "abort":
            return
        try:
            generated = self._write_lists(sync, planner, keep_lists, rolled_back)
        except Exception as exc:  # 반영 단계에서 목록을 만들거나 저장하지 못함(명세서 4.2절 7단계)
            s.raise_to("abort", f"목록 파일을 만들거나 저장하지 못함: {type(exc).__name__}: {exc}")
            return
        remove_empty_dirs(self.notes)
        self._new_state(sync, planner, applied_pages, rolled_back, generated, keep_lists)
        self._check_links()
        s.repo_size = _dir_size(self.notes.parent / ".git")
        state.save(self.notes)
        log(f"끝: 추가 {len(s.added)}, 수정 {len(s.modified)}, 이동 {len(s.moved)}, 삭제 {len(s.deleted)}, "
            f"실패 {len(s.failures)} → 결과 {s.result} (API 요청 {self.client.request_count}회)")

    # ================================================================ 렌더링 보충
    def _render_dependents(self) -> None:
        """동기화 블록 원본이 바뀐 페이지를 쓰는 페이지도 다시 렌더링한다(명세서 4.4절)."""
        changed = set(self.worker.results)
        for page_id, entry in self.state.entries.items():
            if page_id in changed or page_id not in self.nodes or self.nodes[page_id].kind != "page":
                continue
            if set(entry.synced) & changed:
                if self.deadline.passed():
                    return
                try:
                    self.worker.render(self.nodes[page_id])
                except DeadlineReached:
                    return

    # ================================================================ 오류로 탐색을 끝내지 못한 범위
    def _drop_error_roots(self) -> None:
        """오류로 탐색을 끝내지 못한 최상위 범위는 이번에 파일을 하나도 바꾸지 않는다(명세서 4.2절 1-7단계).

        그 범위에서 이미 렌더링한 결과와 새로 찾은 페이지를 버리고, 이전에 백업한 항목은
        이전 상태 그대로(frozen) 둔다. 다른 범위의 변경만 반영된다.
        """
        error_roots = {r for r, cause in self.disc.incomplete.items() if cause == "error"}
        if not error_roots:
            return
        drop = {i for i, n in self.nodes.items() if n.root in error_roots and not n.frozen}
        for node_id in drop:
            self.worker.results.pop(node_id, None)
            del self.nodes[node_id]
        restore = {i: self.state.entries[i] for i in drop if i in self.state.entries}
        for node in self.nodes.values():
            node.children = [c for c in node.children if c not in drop or c in restore]
        self._freeze(restore)

    # ================================================================ 삭제 판정(명세서 4.5절)
    def _judge_deletions(self) -> tuple[set[str], dict[str, Entry]]:
        s, state, disc = self.summary, self.state, self.disc
        kept = disc.kept_from_state()
        confirmed: set[str] = set()
        candidates = [i for i in state.entries if i not in self.nodes and i not in kept]
        depth = {i: state.entries[i].path.count("/") for i in candidates}
        for node_id in sorted(candidates, key=lambda i: depth[i]):
            entry = state.entries[node_id]
            if node_id in self.cfg.exclude or entry.parent in confirmed or self._under_excluded(node_id):
                confirmed.add(node_id)
                continue
            if self.deadline.passed():
                kept[node_id] = entry
                continue
            try:
                if entry.kind == "database":
                    obj = self.client.retrieve_database(node_id)
                elif entry.kind == "data_source":
                    obj = self.client.retrieve_data_source(node_id)
                else:
                    obj = self.client.retrieve_page(node_id)
            except DeadlineReached:
                kept[node_id] = entry
                continue
            except NotionError as exc:
                if exc.status == 401:
                    raise
                if exc.status in (403, 404):
                    entry.unreachable_since = entry.unreachable_since or date.today().isoformat()
                    kept[node_id] = entry
                    if entry.kind == "page":
                        s.unreachable.append(f"{entry.path} (처음 확인 {entry.unreachable_since}, ID {node_id})")
                    continue
                kept[node_id] = entry
                continue
            if obj.get("in_trash") or obj.get("archived"):
                confirmed.add(node_id)
            else:
                kept[node_id] = entry  # 탐색 누락: 사본 유지, 부모를 다음 실행에서 다시 훑는다
                if entry.parent and entry.parent in state.entries:
                    state.entries[entry.parent].children_at = None
        page_total = sum(1 for e in state.entries.values() if e.kind == "page")
        page_deleted = sum(1 for i in confirmed if state.entries[i].kind == "page")
        if (page_total and page_deleted >= self.cfg.min_delete_count
                and page_deleted > page_total * self.cfg.max_delete_ratio and not self.opts.allow_mass_delete):
            s.planned_deletions = sorted(state.entries[i].path for i in confirmed if state.entries[i].kind == "page")
            s.raise_to("abort", f"대량 삭제 감지: 기존 {page_total}개 중 {page_deleted}개를 지우려 했습니다. "
                       "의도한 것이면 allow_mass_delete를 켜고 수동 실행하세요.")
        return confirmed, kept

    def _under_excluded(self, node_id: str) -> bool:
        seen = set()
        current = self.state.entries.get(node_id)
        while current and current.parent and current.parent not in seen:
            if current.parent in self.cfg.exclude:
                return True
            seen.add(current.parent)
            current = self.state.entries.get(current.parent)
        return False

    # ================================================================ 고정과 정리
    def _freeze(self, kept: dict[str, Entry]) -> None:
        """이번에 손대지 않을 상태 항목을 이전 경로 그대로 트리에 넣는다."""
        for node_id, entry in kept.items():
            if node_id in self.nodes:
                continue
            node = Node(id=node_id, kind=entry.kind, title=entry.title, parent=entry.parent, root=entry.root or node_id,
                        created=entry.created, last_edited=entry.last_edited, url=entry.url,
                        children=list(entry.children), frozen=True)
            if entry.kind == "data_source":
                node.list_status = "time"
            self.nodes[node_id] = node
        for node_id, entry in kept.items():
            parent = self.nodes.get(entry.parent) if entry.parent else None
            if parent is not None and node_id not in parent.children:
                parent.children.append(node_id)

    def _is_row(self, node: Node) -> bool:
        parent = self.nodes.get(node.parent) if node.parent else None
        return parent is not None and parent.kind == "data_source"

    def _prune_unwritten(self) -> None:
        """새 페이지인데 이번에 렌더링하지 못한 것은 이번 실행에서 없는 것으로 본다.

        데이터 소스의 항목은 예외다. 목록에서 행은 유지하고 파일 경로만 비운다(명세서 4.2절 1-4단계).
        이런 항목은 present에 들지 않아 경로를 배정받지 않는다.
        """
        drop = {i for i, n in self.nodes.items()
                if n.kind == "page" and not n.frozen and i not in self.worker.results
                and i not in self.state.entries and not self._is_row(n)}
        changed = True
        while changed:
            changed = False
            for i, n in self.nodes.items():
                if i not in drop and n.parent in drop and i not in self.state.entries:
                    drop.add(i)
                    changed = True
        for i in drop:
            del self.nodes[i]
        # 상태 파일에는 노션에서 본 하위 목록을 그대로 남긴다. 그래야 다음 실행이 이 페이지들을 다시 따라간다.
        self.seen_children = {i: list(n.children) for i, n in self.nodes.items()}
        for n in self.nodes.values():
            n.children = [c for c in n.children if c not in drop]

    def _initial_pins(self) -> dict[str, str]:
        pins: dict[str, str] = {}
        for node_id, node in self.nodes.items():
            entry = self.state.entries.get(node_id)
            if not entry:
                continue
            if (node.frozen or node.root in self.disc.incomplete or node_id in self.worker.failures
                    or self._sources_incomplete(node)):
                pins[node_id] = entry.path
        return pins

    def _sources_incomplete(self, node: Node) -> bool:
        """데이터베이스의 데이터 소스 중 목록을 끝까지 받지 못한 것이 있는지."""
        if node.kind != "database":
            return False
        return any(c not in self.nodes or self.nodes[c].list_status != "ok" for c in node.children)

    def _present_pages(self, deletions: set[str]) -> set[str]:
        out = set()
        for node_id, node in self.nodes.items():
            if node.kind != "page" or node_id in deletions:
                continue
            if node_id in self.worker.results or node_id in self.state.entries:
                out.add(node_id)
        return out

    def _props_changed(self) -> set[str]:
        out = set()
        for node_id, node in self.nodes.items():
            entry = self.state.entries.get(node_id)
            if (node.kind == "page" and not node.frozen and entry and node.props_hash
                    and node.props_hash != entry.props_hash and node_id not in self.worker.results):
                out.add(node_id)
        return out

    def _pin_subtree(self, pins: dict[str, str], node_id: str) -> None:
        stack = [node_id]
        while stack:
            current = stack.pop()
            entry = self.state.entries.get(current)
            if entry and current not in pins:
                pins[current] = entry.path
            node = self.nodes.get(current)
            if node:
                stack.extend(node.children)

    # ================================================================ 경로 계산과 묶음 준비(반복)
    def _plan(self, sync: Sync, pins: dict[str, str]):
        roots = [r for r in self.disc.roots if r in self.nodes]
        roots += [i for i, n in self.nodes.items() if n.frozen and (not n.parent or n.parent not in self.nodes) and i not in roots]
        self.plan_roots = roots
        keep_lists: set[str] = set()
        planner = None
        bundles: list[Bundle] = []
        def include(node_id: str) -> bool:
            return self.nodes[node_id].kind != "page" or node_id in sync.present

        for _ in range(MAX_PLAN_ROUNDS):
            planner = PathPlanner(self.nodes, roots, pins, include)
            sync.paths = planner.plan()
            # 최상위에서 닿지 않아 경로를 받지 못한 페이지(부모를 이번에 처리하지 못한 경우 등):
            # 이전 경로가 있으면 하위까지 고정하고, 없으면 이번에는 쓰지 않는다.
            missing = [i for i in sync.present if i not in sync.paths]
            for node_id in missing:
                if node_id in self.state.entries:
                    self._pin_subtree(pins, node_id)
                else:
                    sync.present.discard(node_id)
            if missing:
                continue
            # 옮겨야 하는데 아직 렌더링하지 않은 페이지: 지금 렌더링하거나, 못 하면 고정
            repinned = False
            for node_id, node in list(self.nodes.items()):
                if node.kind != "page" or node.frozen or node_id in pins or node_id not in sync.present:
                    continue
                if sync.moved(node_id) and node_id not in self.worker.results and not sync.renamable(node_id):
                    ok = False
                    if not self.deadline.passed():
                        try:
                            ok = self.worker.render(node) is not None and node_id in self.worker.results
                        except DeadlineReached:
                            ok = False
                    if not ok:
                        self._pin_subtree(pins, node_id)
                        repinned = True
            if repinned:
                continue
            bundles = sync.build_bundles()
            referenced = sync.referenced_images()
            held = []
            for bundle in bundles:
                try:
                    sync.prepare(bundle, referenced)
                except PrepareFailed as exc:
                    held.append((bundle, str(exc)))
            if held:
                for bundle, reason in held:
                    self.summary.warnings.append(f"경로 변경 묶음 보류: {reason}")
                    self.summary.held_bundles += 1
                    self._hold(bundle, sync, pins)
                continue
            failed_lists = self._pregenerate(sync, planner)
            new_fail = failed_lists - keep_lists
            if new_fail:
                keep_lists |= new_fail
                for owner in new_fail:
                    self._hold_list(owner, sync, pins)
                continue
            break
        else:
            self.summary.raise_to("abort", "경로 계산이 안정되지 않았습니다.")
        return planner, bundles, keep_lists

    def _hold(self, bundle: Bundle, sync: Sync, pins: dict[str, str]) -> None:
        for target in bundle.targets:
            self._hold_target(target, sync, pins)

    def _hold_target(self, target: str, sync: Sync, pins: dict[str, str]) -> None:
        """이동은 이전 경로에 고정하고, 삭제는 이번에 하지 않는다."""
        if target in sync.deletions:
            sync.deletions.discard(target)
            entry = self.state.entries.get(target)
            if entry and target not in self.nodes:
                self._freeze({target: entry})
                if entry.kind == "page":
                    sync.present.add(target)
            if entry:
                pins[target] = entry.path
        else:
            self._pin_subtree(pins, target)

    def _list_owner_targets(self, owner: str, sync: Sync) -> set[str]:
        """목록 하나(데이터베이스 ID 또는 '__root__')에 들어가는 경로를 바꾸거나 지우는 대상."""
        out = set()
        for node_id in set(sync.deletions) | {i for i in self.nodes if sync.moved(i)}:
            if owner == "__root__":
                entry = self.state.entries.get(node_id)
                if (entry and not entry.parent) or (node_id in self.nodes and not self.nodes[node_id].parent):
                    out.add(node_id)
                continue
            current = self.nodes.get(node_id)
            parent = current.parent if current else (self.state.entries[node_id].parent if node_id in self.state.entries else None)
            while parent:
                if parent == owner:
                    out.add(node_id)
                    break
                parent_node = self.nodes.get(parent)
                if parent_node and parent_node.kind == "data_source":
                    parent = parent_node.parent
                    continue
                break
        return out

    def _hold_list(self, owner: str, sync: Sync, pins: dict[str, str]) -> None:
        self.summary.raise_to("partial", "목록 파일을 만들지 못해 관련 이동·삭제를 보류했습니다.")
        for target in self._list_owner_targets(owner, sync):
            self.summary.held_bundles += 1
            self._hold_target(target, sync, pins)

    def _pregenerate(self, sync: Sync, planner: PathPlanner) -> set[str]:
        """반영하기 전에 목록 파일을 작업 폴더 대신 메모리에서 만들어 본다. 실패한 목록의 주인을 돌려준다."""
        builder = sync.list_builder(planner, lambda i: i in sync.present)
        failed = set()
        for node_id, node in self.nodes.items():
            if node.kind == "database" and not node.frozen and node_id in sync.paths:
                try:
                    builder.files_for_database(node_id)
                except Exception as exc:
                    self.summary.warnings.append(f"목록 파일 생성 실패({node.title}): {type(exc).__name__}: {exc}")
                    failed.add(node_id)
        try:
            builder.root_readme(self.plan_roots)
        except Exception as exc:
            self.summary.warnings.append(f"최상위 목차 생성 실패: {type(exc).__name__}: {exc}")
            failed.add("__root__")
        return failed

    # ================================================================ 반영
    def _apply(self, sync: Sync, bundles: list[Bundle]) -> tuple[set[str], set[str]]:
        applied: set[str] = set()
        rolled_back: set[str] = set()
        for bundle in bundles:
            try:
                sync.apply(bundle)
            except Exception as exc:
                self.summary.raise_to("partial", "경로 변경 묶음을 반영하다 실패해 되돌렸습니다.")
                self.summary.failures.append(f"묶음 반영 실패, 되돌림: {type(exc).__name__}: {exc}")
                rolled_back |= bundle.pages | bundle.targets
                continue
            applied |= bundle.pages
        return applied, rolled_back

    def _write_lists(self, sync: Sync, planner: PathPlanner, keep: set[str], rolled_back: set[str]) -> dict[str, list[str]]:
        """반영 뒤 실제 저장된 경로로 목록 파일을 만든다. 데이터베이스 ID -> 만든 파일."""
        for node_id in rolled_back:
            entry = self.state.entries.get(node_id)
            if entry:
                sync.paths[node_id] = entry.path
            elif node_id in sync.present:
                sync.present.discard(node_id)
        sync.deletions -= rolled_back

        def has_file(page_id: str) -> bool:
            path = sync.paths.get(page_id)
            return bool(path) and (self.notes / path).is_file()

        builder = ListBuilder(self.nodes, sync.paths, planner.csv_names, planner.single_source, has_file)
        generated: dict[str, list[str]] = {}
        # 지우지 않을 목록 파일: 이번에 쓴 파일과, 이번에 다시 만들지 않아 이전 것을 그대로 두는 파일
        keep_files: set[str] = {README}
        for node_id, node in self.nodes.items():
            if node.kind != "database" or node.frozen or node_id in keep or node_id not in sync.paths:
                continue
            files = builder.files_for_database(node_id)
            entry = self.state.entries.get(node_id)
            old_files = list(entry.files) if entry else []
            if not files and not old_files:
                files = builder.placeholder_for(node_id)
            for rel, text in files.items():
                write_text(self.notes / rel, text, bom=rel.endswith(".csv"))
            # 목록을 끝까지 받지 못한 데이터 소스가 있으면 그 소스의 이전 README·CSV를 그대로 둔다.
            # (그 데이터베이스는 _initial_pins에서 이전 경로에 고정되어 있다.)
            preserved = [f for f in old_files if f not in files] if self._sources_incomplete(node) else []
            keep_files |= set(files) | set(preserved)
            generated[node_id] = sorted(set(files) | set(preserved))
        if "__root__" not in keep:
            write_text(self.notes / README, builder.root_readme(self.plan_roots))
        # 이전에 만든 목록 파일 중 이번에 만들지 않은 것 지우기
        for node_id in keep:
            entry = self.state.entries.get(node_id)
            if entry:
                keep_files |= set(entry.files)
        for node_id, node in self.nodes.items():
            if node.kind == "database" and node_id not in generated:
                entry = self.state.entries.get(node_id)
                if entry:
                    keep_files |= set(entry.files)
                    generated[node_id] = list(entry.files)
        page_files = {sync.paths[i] for i in sync.present if i in sync.paths}
        for node_id, entry in self.state.entries.items():
            if entry.kind != "database":
                continue
            for rel in entry.files:
                if rel not in keep_files and rel not in page_files:
                    path = self.notes / rel
                    if path.is_file():
                        path.unlink()
        return generated

    # ================================================================ 상태 파일
    def _new_state(self, sync: Sync, planner: PathPlanner, applied: set[str], rolled_back: set[str],
                   generated: dict[str, list[str]], keep_lists: set[str]) -> None:
        old = self.state
        self._prev_entries = dict(old.entries)
        results = self.worker.results
        now = _now_iso()
        entries: dict[str, Entry] = {}
        for node_id, node in self.nodes.items():
            prev = old.entries.get(node_id)
            if node.frozen:
                if prev:
                    entries[node_id] = prev
                continue
            if node.kind == "page":
                if node_id not in sync.present or node_id not in sync.paths:
                    if prev and node_id not in sync.deletions:
                        entries[node_id] = prev
                    continue
                if node_id in rolled_back or (node_id in results and node_id not in applied):
                    if prev:
                        entries[node_id] = prev
                    continue
                entry = Entry(kind="page", path=sync.paths[node_id]) if prev is None else copy.deepcopy(prev)
                entry.path = sync.paths[node_id]
                entry.title, entry.parent, entry.root = node.title, node.parent, node.root
                entry.created, entry.url = node.created, node.url
                seen = self.seen_children.get(node_id, node.children)
                entry.children = [c for c in seen if c not in sync.deletions]
                entry.children_at = self.disc.children_at.get(node_id, entry.children_at)
                entry.unreachable_since = None
                if node_id in results:
                    res = results[node_id]
                    entry.last_edited = node.last_edited
                    entry.links = sorted(res.rendered.links)
                    entry.synced = sorted(res.rendered.synced_pages)
                    entry.images = entry_images(res)
                    entry.failed_images = sorted(res.failed_images)
                    entry.rendered_at = now
                    entry.refetch = res.refetch
                    entry.fail_count = 0
                    entry.props_hash = node.props_hash
                elif node_id in self.worker.failures:
                    entry.fail_count += 1
                else:
                    if node_id in sync.props_changed and node_id in applied:
                        entry.props_hash = node.props_hash
                if node_id in self.disc.rescan_next:
                    entry.children_at = None
                entries[node_id] = entry
            else:
                entry = Entry(kind=node.kind, path=sync.paths.get(node_id, prev.path if prev else ""))
                entry.title, entry.parent, entry.root = node.title, node.parent, node.root
                entry.created, entry.last_edited, entry.url = node.created, node.last_edited, node.url
                entry.children = list(node.children)
                if node.kind == "database":
                    entry.single_source = planner.single_source.get(node_id, True)
                    entry.files = generated.get(node_id, prev.files if prev else [])
                if node.kind == "data_source":
                    entry.list_fail_count = 0 if node.list_status == "ok" else ((prev.list_fail_count if prev else 0) + 1)
                entries[node_id] = entry
        # 손대지 않은 상태 항목(삭제 확정이 아닌 것)은 그대로 둔다
        for node_id, prev in old.entries.items():
            if node_id not in entries and node_id not in sync.deletions and node_id not in self.nodes:
                if node_id in self.disc.excluded:
                    continue
                entries[node_id] = prev
        new = State(entries=entries)
        new.roots = sorted({r for r in self.plan_roots if r in entries})
        new.queue = list(self.disc.queue_items)
        new.incomplete_roots = sorted(self.disc.incomplete)
        since = old.full_rebuild_since
        if since:
            left = sum(1 for e in entries.values() if e.kind == "page" and (not e.rendered_at or e.rendered_at < since))
            self.summary.full_rebuild_left = left
            new.full_rebuild_since = since if left else None
        self.state.entries, self.state.roots, self.state.queue = new.entries, new.roots, new.queue
        self.state.incomplete_roots, self.state.full_rebuild_since = new.incomplete_roots, new.full_rebuild_since
        self._summarize(sync, applied)

    # ================================================================ Summary
    def _report_pages(self) -> None:
        s = self.summary
        for page_id, reason in self.worker.failures.items():
            entry = self.state.entries.get(page_id)
            count = (entry.fail_count if entry else 0) + 1
            title = self.nodes[page_id].title if page_id in self.nodes else page_id
            line = f"{title}: {reason}"
            if count >= 3:
                s.repeated_failures.append(f"{line} ({count}회 연속)")
                s.warnings.append(line)
            else:
                s.failures.append(line)
                s.raise_to("partial", "일부 페이지를 받거나 변환하지 못했습니다.")
        for page_id, res in self.worker.results.items():
            title = self.nodes[page_id].title if page_id in self.nodes else page_id
            s.warnings += [f"{title}: {w}" for w in res.warnings]
        for root, cause in self.disc.incomplete.items():
            title = self.nodes[root].title if root in self.nodes else root
            if cause == "error":
                s.incomplete.append(f"{title}: 오류로 미완료(파일을 바꾸지 않음)")
                s.raise_to("partial", "일부 범위를 오류로 탐색하지 못했습니다.")
            else:
                s.incomplete.append(f"{title}: 시간 예산으로 미완료(삭제만 보류)")
        for node in self.nodes.values():
            if node.kind == "data_source" and node.list_status != "ok":
                cause = "API가 불완전한 결과를 반환" if node.list_status == "error" else "목록 조회만으로 시간 예산 초과"
                s.lists_not_updated.append(f"{node.title or node.id}: {cause}")
                prev = self.state.entries.get(node.id)
                count = (prev.list_fail_count if prev else 0) + 1
                if count >= 3:
                    s.list_warnings.append(f"{node.title or node.id}: {count}회 연속")
        for e in self.disc.errors:
            s.warnings.append(f"탐색 오류: {e}")
        s.queue_left = len(self.disc.queue_left)
        # 시간 예산 때문에 이번에 받지 못한 페이지(실패한 페이지는 빼고)
        left = 0
        for node_id, node in self.nodes.items():
            if node.kind != "page" or node.frozen or node_id in self.worker.results or node_id in self.worker.failures:
                continue
            if self.worker.needs_render(node, self.state.entries.get(node_id)):
                left += 1
        s.pages_left = left + len(self.disc.queue_left)

    def _summarize(self, sync: Sync, applied: set[str]) -> None:
        s = self.summary
        prev = self._prev_entries
        for page_id in sorted(applied):
            if page_id not in self.nodes or self.nodes[page_id].kind != "page":
                if page_id in sync.deletions and page_id in prev and prev[page_id].kind == "page":
                    s.deleted.append(prev[page_id].path)
                continue
            before = prev.get(page_id)
            path = sync.paths.get(page_id, "")
            if before is None:
                s.added.append(path)
            elif before.path != path:
                s.moved.append(f"{before.path} → {path}")
            elif page_id in self.worker.results or page_id in sync.props_changed:
                s.modified.append(path)
        dates = [e.children_at for e in self.state.entries.values() if e.kind == "page"]
        if dates:
            s.oldest_children_at = "아직 확인하지 않은 페이지 있음" if not all(dates) else min(dates)[:10]

    def _report_plan(self, sync: Sync) -> None:
        s = self.summary
        prev = self.state.entries
        for page_id in sync.present:
            before = prev.get(page_id)
            path = sync.paths.get(page_id, "")
            if before is None:
                s.added.append(path)
            elif before.path != path:
                s.moved.append(f"{before.path} → {path}")
            elif page_id in self.worker.results:
                s.modified.append(path)
        s.deleted = sorted(prev[i].path for i in sync.deletions if i in prev and prev[i].kind == "page")

    # ================================================================ 실행 검사(명세서 9.2절)
    _LINK = re.compile(r"\]\(([^)\s]+)\)")
    # 코드 블록(```, ~~~)과 인라인 코드(`...`) 안의 글자는 링크가 아니다. 예: `[링크 이름](주소)`를 설명하는 표
    _CODE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$|(`+)(?:(?!\2).)+?\2", re.MULTILINE | re.DOTALL)

    def _check_links(self) -> None:
        broken = []
        for md in self.notes.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for target in self._LINK.findall(self._CODE.sub("", text)):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                resolved = (md.parent / unquote(target)).resolve()
                if not resolved.exists():
                    broken.append(f"{md.relative_to(self.notes).as_posix()} → {target}")
        self.summary.broken_links = broken


def _dir_size(path: Path) -> int | None:
    if not path.exists():
        return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
