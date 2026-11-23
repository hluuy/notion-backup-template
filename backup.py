"""노션 강의 필기 백업 실행 시작점.

GitHub Actions에서:   python backup.py
로컬에서:             NOTION_TOKEN=... python backup.py [--dry-run] [--only <페이지 링크 또는 ID>]

실행 결과는 `ok`, `partial`, `abort` 중 하나로 `GITHUB_OUTPUT`의 `result`에 적고,
정상적으로 끝나면 항상 종료 코드 0으로 끝낸다(명세서 8.2절). 커밋 여부와 실패 표시는
워크플로가 이 값으로 정한다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from notion_backup.client import NotionClient, NotionError
from notion_backup.config import ConfigError, load_config
from notion_backup.deadline import Deadline
from notion_backup.ids import normalize_id
from notion_backup.runner import Backup, Options
from notion_backup.summary import Summary

HERE = Path(__file__).resolve().parent


def _write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<__EOF__\n{value}\n__EOF__\n")
        else:
            fh.write(f"{name}={value}\n")


def _write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)


def _finish(summary: Summary, local: bool) -> int:
    _write_output("result", summary.result)
    _write_output("message", summary.commit_message())
    _write_summary(summary.to_markdown())
    if local and summary.result == "abort":
        print("결과가 abort입니다. notes/가 일부만 반영되었을 수 있습니다. "
              "`git checkout -- notes/`와 `git clean -fd notes/`로 되돌리세요.", file=sys.stderr)
    return 0


def _only(args, token: str) -> int:
    """한 페이지만 렌더링해 결과를 화면에 보여준다. 파일과 상태는 바꾸지 않는다."""
    import tempfile

    from notion_backup.images import Downloader
    from notion_backup.pages import PageWorker
    from notion_backup.state import State
    from notion_backup.sync import Sync
    from notion_backup.tree import Node, page_title

    from notion_backup import log as progress

    progress.enable(False)  # 변환 결과만 화면에 보이게
    page_id = normalize_id(args.only)
    if not page_id:
        print("페이지 링크나 ID를 찾지 못했습니다.", file=sys.stderr)
        return 2
    deadline = Deadline.unlimited()
    client = NotionClient(token, deadline)
    page = client.retrieve_page(page_id)
    node = Node(id=page_id, kind="page", title=page_title(page), parent=None, root=page_id,
                created=page.get("created_time"), last_edited=page.get("last_edited_time"), url=page.get("url"))
    with tempfile.TemporaryDirectory() as work:
        state = State()
        worker = PageWorker(client, state, Path(args.notes), Path(work), Downloader(deadline, 20 * 1024 * 1024), None)
        worker.render(node)
        if page_id not in worker.results:
            print(worker.failures.get(page_id, "렌더링 실패"), file=sys.stderr)
            return 1
        sync = Sync(Path(args.notes), {page_id: node}, state, worker.results)
        sync.paths = {page_id: "page.md"}
        sync.present = {page_id}
        print(sync.page_text(page_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="노션 강의 필기를 Markdown으로 백업한다.")
    parser.add_argument("--dry-run", action="store_true", help="파일을 바꾸지 않고 계획만 출력")
    parser.add_argument("--only", help="한 페이지만 렌더링해 결과를 출력(상태 파일은 갱신하지 않음)")
    parser.add_argument("--full-rebuild", action="store_true", default=os.environ.get("FULL_REBUILD") == "true")
    parser.add_argument("--allow-mass-delete", action="store_true", default=os.environ.get("ALLOW_MASS_DELETE") == "true")
    parser.add_argument("--notes", default=str(HERE / "notes"))
    parser.add_argument("--config", default=str(HERE / "config.yml"))
    args = parser.parse_args(argv)

    local = not os.environ.get("GITHUB_ACTIONS")
    summary = Summary()
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        summary.raise_to("abort", "NOTION_TOKEN이 없습니다. 저장소 Secrets에 노션 통합 시크릿을 등록하세요.")
        return _finish(summary, local)

    if args.only:
        try:
            return _only(args, token)
        except NotionError as exc:
            print(f"노션 API 오류: {exc}", file=sys.stderr)
            return 1

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        summary.raise_to("abort", str(exc))
        return _finish(summary, local)

    started = os.environ.get("JOB_STARTED_AT")
    deadline = Deadline.from_job_start(float(started) if started else None)
    client = NotionClient(token, deadline)
    options = Options(dry_run=args.dry_run, full_rebuild=args.full_rebuild, allow_mass_delete=args.allow_mass_delete)
    backup = Backup(client, cfg, Path(args.notes), deadline, options)
    summary = backup.run()
    if args.dry_run:
        print(summary.to_markdown())
        return 0
    return _finish(summary, local)


if __name__ == "__main__":
    sys.exit(main())
