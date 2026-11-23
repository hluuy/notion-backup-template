"""페이지 내용 받기와 렌더링, 이미지 준비(명세서 4.2절 6단계, 6.5절)."""

from __future__ import annotations

import time

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .client import NotionClient, NotionError
from .convert import Renderer, Rendered
from .deadline import DeadlineReached
from .log import log, short
from .ids import normalize_id
from .images import Downloader, TooLarge, file_name, image_url, source_id
from .naming import escape_markdown
from .state import Entry, Image, State
from .tree import Node

REFETCH_WINDOW = timedelta(minutes=2)


@dataclass
class PreparedImage:
    block: str
    source: str
    name: str  # images/ 안의 파일 이름
    local: Path  # 작업 폴더의 사본


@dataclass
class PageResult:
    rendered: Rendered
    images: list[PreparedImage] = field(default_factory=list)
    failed_images: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fetched_at: str = ""
    refetch: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PageWorker:
    def __init__(
        self,
        client: NotionClient,
        state: State,
        notes: Path,
        work: Path,
        downloader: Downloader,
        full_rebuild_since: str | None,
    ):
        self.client = client
        self.state = state
        self.notes = notes
        self.work = work
        self.downloader = downloader
        self.full_rebuild_since = full_rebuild_since
        self.title_of: Callable[[str], str] = lambda _id: "링크된 페이지"
        self.page_of_block: Callable[[str], str | None] = lambda _id: None
        self.results: dict[str, PageResult] = {}
        self.failures: dict[str, str] = {}
        self.force: set[str] = set()

    # ------------------------------------------------------------------ 판단
    def needs_render(self, node: Node, entry: Entry | None) -> bool:
        if node.id in self.results:
            return False
        if entry is None or entry.refetch or node.id in self.force:
            return True
        if entry.last_edited != node.last_edited or entry.failed_images:
            return True
        if self.full_rebuild_since and (not entry.rendered_at or entry.rendered_at < self.full_rebuild_since):
            return True
        return not (self.notes / entry.path).exists()

    # ------------------------------------------------------------------ 블록 받기
    def fetch_blocks(self, block_id: str) -> list[dict]:
        blocks = self.client.block_children(block_id)
        for block in blocks:
            kind = block.get("type")
            if block.get("has_children") and kind not in ("child_page", "child_database"):
                if kind == "synced_block" and (block.get("synced_block") or {}).get("synced_from"):
                    continue  # 원본 내용은 렌더링할 때 따로 받는다
                block["children"] = self.fetch_blocks(block["id"])
        return blocks

    @staticmethod
    def collect_children(blocks: list[dict]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "child_page":
                out.append((normalize_id(block["id"]), "page"))
            elif kind == "child_database":
                out.append((normalize_id(block["id"]), "database"))
            elif block.get("children"):
                out.extend(PageWorker.collect_children(block["children"]))
        return out

    def fetch_children(self, page_id: str) -> list[tuple[str, str]]:
        return self.collect_children(self.fetch_blocks(page_id))

    # ------------------------------------------------------------------ 렌더링
    def render(self, node: Node) -> list[tuple[str, str]] | None:
        """내용을 받아 렌더링한다. 하위 목록을 돌려주고, 렌더링에 실패한 새 페이지는 None.

        시간 예산에 걸리면 DeadlineReached를 그대로 낸다.
        """
        entry = self.state.entries.get(node.id)
        started = time.monotonic()
        try:
            blocks = self.fetch_blocks(node.id)
        except NotionError as exc:
            if exc.status == 401:
                raise
            self.failures[node.id] = f"내용 받기 실패: {exc}"
            log(f"  실패: {short(node.title)} — 내용 받기 실패 ({exc.status})")
            return None
        fetched = _now()
        children = self.collect_children(blocks)
        ctx = _Context(self, node, entry)
        try:
            rendered = Renderer(ctx).render_page(node.title, blocks)
        except DeadlineReached:
            raise
        except NotionError as exc:
            if exc.status == 401:
                raise
            self.failures[node.id] = f"변환 실패: {exc}"
            log(f"  실패: {short(node.title)} — 변환 실패 ({exc.status})")
            return children if entry else None
        except Exception as exc:  # 변환기 버그로 한 페이지가 전체를 멈추지 않게 한다
            self.failures[node.id] = f"변환 실패: {type(exc).__name__}: {exc}"
            log(f"  실패: {short(node.title)} — 변환 실패 ({type(exc).__name__})")
            return children if entry else None
        edited = None
        if node.last_edited:
            try:
                edited = datetime.fromisoformat(node.last_edited.replace("Z", "+00:00"))
            except ValueError:
                edited = None
        refetch = bool(edited and abs(fetched - edited) <= REFETCH_WINDOW)
        self.results[node.id] = PageResult(
            rendered=rendered,
            images=ctx.images,
            failed_images=ctx.failed_images,
            warnings=ctx.warnings,
            fetched_at=fetched.replace(microsecond=0).isoformat(),
            refetch=refetch,
        )
        self.failures.pop(node.id, None)
        extra = []
        if ctx.images:
            extra.append(f"이미지 {len(ctx.images)}")
        if ctx.failed_images:
            extra.append(f"이미지 실패 {len(ctx.failed_images)}")
        if children:
            extra.append(f"하위 {len(children)}")
        detail = f" ({', '.join(extra)})" if extra else ""
        log(f"  [{len(self.results)}] {'새로 받음' if entry is None else '다시 받음'}: "
            f"{short(node.title)}{detail} {time.monotonic() - started:.1f}초")
        return children


class _Context:
    def __init__(self, worker: PageWorker, node: Node, entry: Entry | None):
        self.worker = worker
        self.node = node
        self.entry = entry
        self.images: list[PreparedImage] = []
        self.failed_images: list[str] = []
        self.warnings: list[str] = []

    def title_of(self, page_id: str) -> str:
        return self.worker.title_of(page_id)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def synced_children(self, block_id: str) -> tuple[list[dict], str | None]:
        block_id = normalize_id(block_id)
        try:
            blocks = self.worker.fetch_blocks(block_id)
        except NotionError as exc:
            if exc.status == 401:
                raise
            self.warn(f"동기화 블록 원본을 읽지 못함: {block_id}")
            return [], None
        return blocks, self.worker.page_of_block(block_id)

    def image(self, block: dict, caption: str) -> str:
        url, kind = image_url(block)
        block_id = normalize_id(block["id"])
        alt = escape_markdown(caption)
        if not url:
            return f"<!-- 이미지 주소 없음: {block_id} -->"
        src = source_id(url, kind)
        name = file_name(block_id, url)
        local = self.worker.work / "images" / self.node.id / name
        old = self._old_image(block_id, src)
        if old is not None:
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(old, local)
        else:
            try:
                self.worker.downloader.download(url, local)
            except DeadlineReached:
                raise
            except TooLarge:
                return f"📎 {alt or '이미지'} (20MB 초과 이미지, 백업 안 됨)"
            except Exception as exc:  # 다운로드 실패는 경고만 남기고 원래 링크를 둔다
                self.failed_images.append(block_id)
                self.warn(f"이미지 다운로드 실패: {block_id} ({type(exc).__name__})")
                return f"![{alt}]({url})"
        self.images.append(PreparedImage(block=block_id, source=src, name=name, local=local))
        return f"![{alt}](images/{name})"

    def _old_image(self, block_id: str, src: str) -> Path | None:
        if not self.entry:
            return None
        folder = self.entry.path.rsplit("/", 1)[0] if "/" in self.entry.path else ""
        for img in self.entry.images:
            if img.block == block_id and img.source == src:
                path = self.worker.notes / folder / img.file if folder else self.worker.notes / img.file
                if path.exists():
                    return path
        return None


def entry_images(result: PageResult) -> list[Image]:
    return [Image(block=i.block, source=i.source, file=f"images/{i.name}") for i in result.images]
