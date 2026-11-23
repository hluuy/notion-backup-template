"""가짜 노션으로 돌려 보는 동작 검증(명세서 10.3절 중 API 없이 재현할 수 있는 것)."""

from pathlib import Path

from fake_notion import FakeNotion, NoDownload, mention, paragraph, text, uid
from notion_backup.config import Config
from notion_backup.deadline import Deadline
from notion_backup.runner import Backup, Options

ROOT, CH1, CH2 = uid(1), uid(2), uid(3)


def run(fake, notes: Path, cfg: Config | None = None, deadline: Deadline | None = None, **opts):
    deadline = deadline or Deadline.unlimited()
    fake.deadline = deadline
    backup = Backup(fake, cfg or Config(), notes, deadline, Options(**opts),
                    downloader=NoDownload(), work=notes.parent / "work")
    return backup.run()


def basic() -> FakeNotion:
    f = FakeNotion()
    f.add_page(ROOT, "운영체제", shared=True, blocks=[paragraph(uid(100), text("소개"))])
    f.add_page(CH1, "1주차", parent=ROOT, blocks=[paragraph(uid(101), text("내용 "), mention(CH2, "2주차"))])
    f.add_page(CH2, "2주차", parent=ROOT, blocks=[paragraph(uid(102), text("둘째"))])
    return f


def read(notes: Path, rel: str) -> str:
    return (notes / rel).read_text(encoding="utf-8")


def test_first_backup(tmp_path):
    notes = tmp_path / "notes"
    s = run(basic(), notes)
    assert s.result == "ok", s.reasons
    assert "[1주차](1주차.md)" in read(notes, "운영체제/README.md")
    assert "[2주차](2주차.md)" in read(notes, "운영체제/1주차.md")
    assert read(notes, "운영체제/1주차.md").startswith('---\n"notion_id": "' + CH1)
    assert (notes / "README.md").exists() and (notes / ".backup-state.json").exists()
    assert len(s.added) == 3 and not s.broken_links


def test_second_run_changes_nothing(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    before = {p: p.read_bytes() for p in notes.rglob("*") if p.is_file()}
    s = run(f, notes)
    after = {p: p.read_bytes() for p in notes.rglob("*") if p.is_file()}
    assert s.result == "ok"
    assert not (s.added or s.modified or s.moved or s.deleted)
    assert before == after


def test_rename_moves_file_and_fixes_links(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    f.edit(CH1, title="1주차 필기")
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert (notes / "운영체제/1주차 필기.md").exists()
    assert not (notes / "운영체제/1주차.md").exists()
    assert "](1주차%20필기.md)" in read(notes, "운영체제/README.md")
    assert s.moved and not s.broken_links


def test_trashed_page_is_deleted_and_links_point_to_notion(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    f.trash(CH2)
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert not (notes / "운영체제/2주차.md").exists()
    assert "https://www.notion.so/" + CH2.replace("-", "") in read(notes, "운영체제/1주차.md")
    assert s.deleted == ["운영체제/2주차.md"] and not s.broken_links


def test_unshared_pages_are_kept(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    f.shared.clear()
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert (notes / "운영체제/README.md").exists() and (notes / "운영체제/1주차.md").exists()
    assert len(s.unreachable) == 3


def test_mass_delete_is_blocked_then_allowed(tmp_path):
    notes = tmp_path / "notes"
    f = FakeNotion()
    f.add_page(ROOT, "과목", shared=True)
    kids = [f.add_page(uid(10 + i), f"{i}주차", parent=ROOT) for i in range(6)]
    run(f, notes)
    for kid in kids[:5]:
        f.trash(kid)
    s = run(f, notes)
    assert s.result == "abort"
    assert (notes / "과목/0주차.md").exists()
    s = run(f, notes, allow_mass_delete=True)
    assert s.result == "ok", s.reasons
    assert not (notes / "과목/0주차.md").exists()


def test_failed_page_makes_partial_but_keeps_others(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    f.fail_blocks.add(CH1)
    s = run(f, notes)
    assert s.result == "partial"
    assert (notes / "운영체제/README.md").exists() and (notes / "운영체제/2주차.md").exists()
    assert not (notes / "운영체제/1주차.md").exists()


def test_exclude_deletes_and_relinks(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    s = run(f, notes, cfg=Config(exclude={CH2}))
    assert s.result == "ok", s.reasons
    assert not (notes / "운영체제/2주차.md").exists()
    assert "https://www.notion.so/" + CH2.replace("-", "") in read(notes, "운영체제/README.md")


def test_lost_state_file_is_rebuilt_without_deleting(tmp_path):
    notes = tmp_path / "notes"
    f = basic()
    run(f, notes)
    (notes / ".backup-state.json").unlink()
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert not s.deleted and not s.moved
    assert (notes / "운영체제/1주차.md").exists() and (notes / ".backup-state.json").exists()


def test_empty_workspace_aborts(tmp_path):
    s = run(FakeNotion(), tmp_path / "notes")
    assert s.result == "abort"


# ---------------------------------------------------------------- 데이터베이스(구현 검수 반영)
DB, DS = uid(50), uid(51)
A, B, C = uid(60), uid(61), uid(62)
DB_DIR = "운영체제/강의 목록"
CSV = f"{DB_DIR}/강의 목록.csv"


def with_db() -> FakeNotion:
    f = basic()
    f.add_database(DB, DS, "강의 목록", parent=ROOT)
    f.add_page(A, "A", parent=DS, tags=["시험"], relations=[B, C], blocks=[paragraph(uid(110), text("가"))])
    f.add_page(B, "B", parent=DS, blocks=[paragraph(uid(111), text("나"))])
    f.add_page(C, "C", parent=DS, blocks=[paragraph(uid(112), text("다"))])
    return f


def csv_rows(notes: Path) -> dict[str, list[str]]:
    import csv

    with open(notes / CSV, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["이름", "관련", "날짜", "태그", "파일 경로"]
    return {r[0]: r for r in rows[1:]}


def test_database_backup(tmp_path):
    notes = tmp_path / "notes"
    s = run(with_db(), notes)
    assert s.result == "ok", s.reasons
    rows = csv_rows(notes)
    assert rows["A"] == ["A", "B, C", "", "시험", "A.md"]
    assert '"관련": ["B", "C"]' in read(notes, f"{DB_DIR}/A.md")
    assert "[B](B.md)" in read(notes, f"{DB_DIR}/README.md")


def test_unwritten_item_keeps_csv_row(tmp_path):
    """목록을 끝까지 받았으면, 본문을 쓰지 못한 항목도 CSV 행은 남고 파일 경로만 빈다."""
    notes = tmp_path / "notes"
    f = with_db()
    f.fail_blocks.add(B)
    s = run(f, notes)
    assert s.result == "partial"
    rows = csv_rows(notes)
    assert set(rows) == {"A", "B", "C"}
    assert rows["B"][-1] == "" and rows["C"][-1] == "C.md"
    assert not (notes / DB_DIR / "B.md").exists()
    assert "https://www.notion.so/" + B.replace("-", "") in read(notes, f"{DB_DIR}/README.md")
    f.fail_blocks.clear()
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert csv_rows(notes)["B"][-1] == "B.md"


def test_incomplete_list_keeps_readme_and_csv(tmp_path):
    """목록 조회가 incomplete면 기존 README·CSV·항목 파일을 바이트까지 그대로 둔다."""
    notes = tmp_path / "notes"
    f = with_db()
    run(f, notes)
    before = {p: (notes / p).read_bytes() for p in (CSV, f"{DB_DIR}/README.md", f"{DB_DIR}/A.md")}
    f.databases[DB].incomplete = True
    f.edit(A, title="A 고침")
    s = run(f, notes)
    assert s.result != "abort", s.reasons
    for rel, data in before.items():
        assert (notes / rel).read_bytes() == data, rel
    assert not s.deleted and s.lists_not_updated


def test_error_root_changes_no_files(tmp_path):
    """오류로 탐색을 끝내지 못한 범위는 이미 렌더링한 페이지도 반영하지 않는다. 다른 범위는 반영한다."""
    notes = tmp_path / "notes"
    f = with_db()
    other = f.add_page(uid(70), "자료구조", shared=True, blocks=[paragraph(uid(120), text("처음"))])
    run(f, notes)
    before = read(notes, "운영체제/1주차.md")
    f.edit(CH1, blocks=[paragraph(uid(101), text("바뀐 내용"))])
    f.add_page(uid(71), "새 페이지", parent=ROOT)
    f.edit(other, blocks=[paragraph(uid(120), text("고침"))])
    f.fail_objects.add(DS)
    s = run(f, notes)
    assert s.result == "partial"
    assert read(notes, "운영체제/1주차.md") == before
    assert not (notes / "운영체제/새 페이지.md").exists()
    assert (notes / CSV).exists() and (notes / DB_DIR / "A.md").exists()
    assert "고침" in read(notes, "자료구조.md")
    f.fail_objects.clear()
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert "바뀐 내용" in read(notes, "운영체제/1주차.md")
    assert (notes / "운영체제/새 페이지.md").exists()


def test_relation_failure_keeps_previous_value(tmp_path):
    notes = tmp_path / "notes"
    f = with_db()
    run(f, notes)
    f.fail_relations = True
    f.edit(A, blocks=[paragraph(uid(110), text("가나"))])
    s = run(f, notes)
    assert '"관련": ["B", "C"]' in read(notes, f"{DB_DIR}/A.md")
    assert "가나" in read(notes, f"{DB_DIR}/A.md")
    assert csv_rows(notes)["A"][1] == "B, C"
    # 이전 값이 없는 새 항목은 본문을 미루고, 목록에는 받은 값까지만 표시한다
    f.add_page(uid(63), "D", parent=DS, relations=[B, C])
    s = run(f, notes)
    assert s.result == "partial"
    assert not (notes / DB_DIR / "D.md").exists()
    assert csv_rows(notes)["D"][1:] == ["B, …", "", "", ""]
    f.fail_relations = False
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert '"관련": ["B", "C"]' in read(notes, f"{DB_DIR}/D.md")


def test_queue_is_resumed_next_run(tmp_path):
    """시간 예산에 걸려 남긴 탐색 대기열을 다음 실행이 먼저 이어 받는다.

    최상위 앞쪽에 매 실행 요청이 드는 데이터베이스가 있어, 최상위부터 다시 따라가면
    같은 예산으로 뒤쪽 페이지를 끝내지 못한다. 요청 수를 시계로 쓴다.
    """
    import json

    notes = tmp_path / "notes"
    f = FakeNotion()
    f.add_page(ROOT, "과목", shared=True)
    p1 = f.add_page(uid(2), "자료", parent=ROOT)
    f.add_database(uid(50), uid(51), "목록", parent=p1)
    p2 = f.add_page(uid(3), "주차별", parent=ROOT)
    for i in range(6):
        f.add_page(uid(10 + i), f"{i}주차", parent=p2)

    def written():
        return sum((notes / f"과목/주차별/{i}주차.md").exists() for i in range(6))

    s = run(f, notes, deadline=Deadline(8, clock=lambda: f.request_count))
    assert s.result == "ok", s.reasons
    state = json.loads((notes / ".backup-state.json").read_text(encoding="utf-8"))
    pages = [q for q in state["queue"] if q["kind"] == "page"]
    assert len(pages) == 5 and all(q["parent"] == p2 and q["root"] == ROOT for q in pages)
    assert written() == 1
    f.request_count = 0
    s = run(f, notes, deadline=Deadline(8, clock=lambda: f.request_count))
    assert s.result == "ok", s.reasons
    assert written() == 6
    state = json.loads((notes / ".backup-state.json").read_text(encoding="utf-8"))
    # 이번에 처리한 페이지(이어 받은 1~5주차)는 다시 남기지 않는다. 아직 방문하지 않은 것만 남는다.
    assert not {q["id"] for q in state["queue"]} & {uid(10 + i) for i in range(1, 6)}
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    state = json.loads((notes / ".backup-state.json").read_text(encoding="utf-8"))
    assert state["queue"] == []


# ---------------------------------------------------------------- 날짜 접두사(파일 목록 날짜순 정렬)
def dated_db() -> FakeNotion:
    """만든 순서와 날짜 순서가 다른 항목들. A는 B를 링크한다."""
    f = basic()
    f.add_database(DB, DS, "강의 목록", parent=ROOT)
    f.add_page(A, "A", parent=DS, date="2024-01-10", blocks=[paragraph(uid(110), text("참고 "), mention(B, "B"))])
    f.add_page(B, "B", parent=DS, date="2023-07-13", blocks=[paragraph(uid(111), text("나"))])
    f.add_page(C, "C", parent=DS, blocks=[paragraph(uid(112), text("다"))])  # 날짜 없음
    return f


def ordered_rows(notes: Path) -> list[str]:
    import csv

    with open(notes / CSV, encoding="utf-8-sig", newline="") as fh:
        return [r[0] for r in list(csv.reader(fh))[1:]]


def test_date_prefix_names_and_order(tmp_path):
    notes = tmp_path / "notes"
    s = run(dated_db(), notes)
    assert s.result == "ok", s.reasons
    names = sorted(p.name for p in (notes / DB_DIR).glob("*.md") if p.name != "README.md")
    assert names == ["2023-07-13 B.md", "2024-01-10 A.md", "C.md"]
    assert ordered_rows(notes) == ["B", "A", "C"]  # 날짜순, 날짜 없는 항목은 맨 뒤
    assert "[B](2023-07-13%20B.md)" in read(notes, f"{DB_DIR}/2024-01-10 A.md")
    assert not s.broken_links


def test_turning_on_date_prefix_renames_without_refetch(tmp_path):
    """이미 백업한 데이터베이스에 날짜 접두사를 켜면, 노션에서 다시 받지 않고 이름만 바꾼다."""
    notes = tmp_path / "notes"
    f = dated_db()
    run(f, notes, cfg=Config(date_prefix=False))
    assert (notes / DB_DIR / "A.md").exists()
    f.fetched.clear()
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert not {A, B, C} & set(f.fetched)  # 항목 본문을 다시 받지 않음
    assert (notes / DB_DIR / "2024-01-10 A.md").exists() and not (notes / DB_DIR / "A.md").exists()
    assert "[B](2023-07-13%20B.md)" in read(notes, f"{DB_DIR}/2024-01-10 A.md")  # 옮긴 파일 안의 링크도 고침
    assert "](2024-01-10%20A.md)" in read(notes, f"{DB_DIR}/README.md")
    assert len(s.moved) == 2 and not s.added and not s.deleted and not s.broken_links
    s = run(f, notes)  # 다음 실행은 아무것도 바꾸지 않는다
    assert not (s.added or s.modified or s.moved or s.deleted)


def test_date_change_renames_file(tmp_path):
    notes = tmp_path / "notes"
    f = dated_db()
    run(f, notes)
    f.pages[B].date = "2024-02-01"
    f.edit(B)
    s = run(f, notes)
    assert s.result == "ok", s.reasons
    assert (notes / DB_DIR / "2024-02-01 B.md").exists() and not (notes / DB_DIR / "2023-07-13 B.md").exists()
    assert "[B](2024-02-01%20B.md)" in read(notes, f"{DB_DIR}/2024-01-10 A.md")
    assert ordered_rows(notes) == ["A", "B", "C"]


def test_link_check_ignores_code(tmp_path):
    """코드 안의 `[이름](주소)`는 깨진 링크로 세지 않는다. 진짜 깨진 링크는 센다."""
    notes = tmp_path / "notes"
    f = basic()
    code = {"object": "block", "id": uid(130), "type": "code", "has_children": False,
            "code": {"rich_text": [text("[x](없는파일.md)")], "language": "markdown", "caption": []}}
    f.edit(CH2, blocks=[paragraph(uid(102), text("[링크 이름](주소)", code=True)), code])
    s = run(f, notes)
    assert s.result == "ok" and not s.broken_links, s.broken_links
    (notes / "운영체제" / "직접 쓴 파일.md").write_text("[깨짐](없음.md)\n", encoding="utf-8")
    s = run(f, notes)
    assert s.broken_links == ["운영체제/직접 쓴 파일.md → 없음.md"]
