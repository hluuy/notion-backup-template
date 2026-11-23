"""실행 결과와 Summary(명세서 9절, 9.2절)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .properties import KST

LEVEL = {"ok": 0, "partial": 1, "abort": 2}


@dataclass
class Summary:
    result: str = "ok"
    reasons: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    repeated_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    lists_not_updated: list[str] = field(default_factory=list)
    list_warnings: list[str] = field(default_factory=list)
    no_id_files: list[str] = field(default_factory=list)
    orphan_images: list[str] = field(default_factory=list)
    broken_links: list[str] = field(default_factory=list)
    planned_deletions: list[str] = field(default_factory=list)
    pages_left: int = 0
    queue_left: int = 0
    held_bundles: int = 0
    full_rebuild_left: int | None = None
    oldest_children_at: str | None = None
    repo_size: int | None = None
    requests: int = 0

    def raise_to(self, level: str, reason: str | None = None) -> None:
        if LEVEL[level] > LEVEL[self.result]:
            self.result = level
        if reason:
            self.reasons.append(reason)

    def commit_message(self, now: datetime | None = None) -> str:
        day = (now or datetime.now(KST)).astimezone(KST).strftime("%Y-%m-%d")
        return (f"backup: {day} (추가 {len(self.added)}, 수정 {len(self.modified)}, "
                f"이동 {len(self.moved)}, 삭제 {len(self.deleted)})")

    def to_markdown(self) -> str:
        out = ["## Notion 백업 결과", ""]
        if self.repeated_failures:
            out += ["### 계속 실패 중", ""] + [f"- {x}" for x in self.repeated_failures] + [""]
        if self.list_warnings:
            out += ["### 목록을 끝까지 받지 못하는 데이터 소스", ""] + [f"- {x}" for x in self.list_warnings] + [""]
        out += [
            f"**결과: `{self.result}`**" + (f" — {'; '.join(self.reasons)}" if self.reasons else ""),
            "",
            "| 추가 | 수정 | 이동 | 삭제 | 실패 | 경고 |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| {len(self.added)} | {len(self.modified)} | {len(self.moved)} | {len(self.deleted)} | "
            f"{len(self.failures)} | {len(self.warnings)} |",
            "",
        ]
        details = [
            ("실패", self.failures),
            ("경고", self.warnings),
            ("확인 필요(접근 불가)", self.unreachable),
            ("탐색 미완료 범위", self.incomplete),
            ("목록과 CSV를 이번에 갱신하지 않은 데이터 소스", self.lists_not_updated),
            ("삭제 예정(대량 삭제 차단 등)", self.planned_deletions),
            ("notion_id가 없는 파일", self.no_id_files),
            ("주인을 알 수 없는 이미지", self.orphan_images),
            ("깨진 링크", self.broken_links),
            ("추가", self.added),
            ("수정", self.modified),
            ("이동", self.moved),
            ("삭제", self.deleted),
        ]
        for title, items in details:
            if items:
                out += [f"<details><summary>{title} ({len(items)})</summary>", ""]
                out += [f"- {x}" for x in items[:200]]
                if len(items) > 200:
                    out.append(f"- … 외 {len(items) - 200}개")
                out += ["", "</details>", ""]
        out += [
            "| 항목 | 값 |",
            "| --- | --- |",
            f"| 다음 실행으로 넘긴 페이지 | {self.pages_left} |",
            f"| 남은 탐색 대기열 | {self.queue_left} |",
            f"| 보류한 경로 변경 묶음(이동·삭제) | {self.held_bundles} |",
            f"| full_rebuild 남은 페이지 | {self.full_rebuild_left if self.full_rebuild_left is not None else '-'} |",
            f"| 가장 오래된 하위 목록 확인일 | {self.oldest_children_at or '-'} |",
            f"| 저장소 크기 | {_size(self.repo_size)} |",
            f"| API 요청 수 | {self.requests} |",
        ]
        if self.repo_size and self.repo_size > 1024**3:
            out += ["", "> 저장소가 1GB를 넘었습니다. 오래된 이미지 이력 정리를 검토하세요."]
        return "\n".join(out) + "\n"


def _size(value: int | None) -> str:
    if value is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return str(value)
