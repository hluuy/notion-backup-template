"""API 없이 검사하는 규칙: 이름, 링크, 설정, 변환(명세서 10.1절)."""

from pathlib import Path

import pytest

from fake_notion import mention, paragraph, text, uid
from notion_backup.config import ConfigError, load_config
from notion_backup.convert import PLACEHOLDER, Renderer
from notion_backup.ids import normalize_id
from notion_backup.naming import encode_path, escape_markdown, relative_link, sanitize
from notion_backup.paths import PathPlanner
from notion_backup.properties import front_matter, front_matter_keys, read_front_matter, to_kst
from notion_backup.tree import Node


# ---------------------------------------------------------------- ID와 이름
def test_normalize_id_forms():
    raw = "1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6"
    expected = "1a2b3c4d-5e6f-47a8-b9c0-d1e2f3a4b5c6"
    assert normalize_id(raw) == expected
    assert normalize_id(expected) == expected
    assert normalize_id(f"https://www.notion.so/운영체제-{raw}?pvs=4") == expected
    assert normalize_id("no id here") is None


@pytest.mark.parametrize("title,expected", [
    ("3주차: 프로세스/스레드?", "3주차_ 프로세스_스레드_"),
    ("  끝에 마침표.  ", "끝에 마침표"),
    ("CON", "CON_"),
    ("", "제목 없음"),
    ("가" * 120, "가" * 100),
])
def test_sanitize(title, expected):
    assert sanitize(title) == expected


def test_encode_keeps_korean_and_encodes_spaces():
    assert encode_path("../운영체제/3주차 필기.md") == "../운영체제/3주차%20필기.md"
    assert encode_path("a(b).md") == "a%28b%29.md"


def test_relative_link():
    assert relative_link("운영체제/3주차 필기.md", "운영체제/README.md") == "3주차%20필기.md"
    assert relative_link("기타.md", "운영체제/README.md") == "../기타.md"


def test_escape_markdown():
    assert escape_markdown("a*b_c") == "a\\*b\\_c"
    assert escape_markdown("# 제목 아님") == "\\# 제목 아님"
    assert escape_markdown("1. 목록 아님") == "1\\. 목록 아님"


# ---------------------------------------------------------------- 설정
def test_config_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "없음.yml")
    assert cfg.max_delete_ratio == 0.3 and cfg.min_delete_count == 5 and cfg.max_image_mb == 20


def test_config_reads_ids(tmp_path: Path):
    path = tmp_path / "config.yml"
    path.write_text("exclude:\n  - https://www.notion.so/x-1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.exclude == {"1a2b3c4d-5e6f-47a8-b9c0-d1e2f3a4b5c6"}


def test_config_error_names_line(tmp_path: Path):
    path = tmp_path / "config.yml"
    path.write_text("safety:\n  max_delete_ratio: 많이\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="2번째 줄"):
        load_config(path)


# ---------------------------------------------------------------- front matter
def test_front_matter_round_trip_and_collision():
    meta = {"notion_id": uid(1), "notion_url": "u", "created": to_kst("2026-03-10T00:12:00.000Z"), "last_edited": None}
    text_ = front_matter(meta, {"created": "속성", "태그": ["시험"]})
    assert '"created_": "속성"' in text_
    assert read_front_matter(text_ + "# 본문\n")["notion_id"] == uid(1)
    assert meta["created"] == "2026-03-10T09:12:00+09:00"
    assert front_matter_keys(["Notion_ID"]) == {"Notion_ID": "Notion_ID_"}


# ---------------------------------------------------------------- 변환
class Ctx:
    def __init__(self):
        self.warnings = []

    def title_of(self, page_id):
        return "다른 페이지"

    def image(self, block, caption):
        return f"![{caption}](images/x.png)"

    def synced_children(self, block_id):
        return [], None

    def warn(self, message):
        self.warnings.append(message)


def render(blocks):
    return Renderer(Ctx()).render_page("제목", blocks)


def block(kind, block_id="b", children=None, **data):
    b = {"object": "block", "id": block_id, "type": kind, "has_children": bool(children), kind: data}
    if children:
        b["children"] = children
    return b


def test_render_basic_formatting():
    out = render([paragraph("p", text("굵게", bold=True), text(" 보통 "), text("코드", code=True))]).body
    assert out == "# 제목\n\n**굵게** 보통 `코드`\n"


def test_render_lists_and_nested():
    blocks = [
        block("bulleted_list_item", "a", rich_text=[text("하나")], children=[
            block("numbered_list_item", "b", rich_text=[text("안쪽")])]),
        block("to_do", "c", rich_text=[text("할 일")], checked=True),
    ]
    out = render(blocks).body
    assert "- 하나\n\n    1. 안쪽\n- [x] 할 일" in out


def test_render_toggle_is_expanded():
    blocks = [block("toggle", "t", rich_text=[text("열기")], children=[paragraph("p", text("내용"))])]
    assert "**열기**\n\n내용" in render(blocks).body


def test_render_table_and_code():
    rows = [block("table_row", "r1", cells=[[text("a|b")], [text("둘째\n줄")]]),
            block("table_row", "r2", cells=[[text("1")], [text("2")]])]
    blocks = [block("table", "t", children=rows, table_width=2),
              block("code", "c", rich_text=[text("print(1)")], language="python", caption=[])]
    out = render(blocks).body
    assert "| a\\|b | 둘째<br>줄 |\n| --- | --- |\n| 1 | 2 |" in out
    assert "```python\nprint(1)\n```" in out


def test_render_links_use_placeholders():
    target = uid(9)
    result = render([paragraph("p", mention(target, "링크"))])
    assert target in result.links
    assert PLACEHOLDER.search(result.body)


def test_unsupported_block_is_commented():
    out = render([block("mystery", "m")]).body
    assert "<!-- 지원하지 않는 블록: mystery -->" in out


def test_callout_with_emoji():
    blocks = [block("callout", "c", rich_text=[text("주의")], icon={"type": "emoji", "emoji": "💡"})]
    assert "> 💡 주의" in render(blocks).body


# ---------------------------------------------------------------- 경로
def _node(i, title, parent=None, created="2026-01-01", kind="page"):
    return Node(id=uid(i), kind=kind, title=title, parent=uid(parent) if parent else None,
                root=uid(1), created=created)


def test_paths_folder_and_duplicates_and_reserved():
    root = _node(1, "운영체제")
    a = _node(2, "메모", 1, "2026-01-01")
    b = _node(3, "메모", 1, "2026-01-02")
    r = _node(4, "README", 1, "2026-01-03")
    root.children = [a.id, b.id, r.id]
    nodes = {n.id: n for n in (root, a, b, r)}
    paths = PathPlanner(nodes, [root.id], {}).plan()
    assert paths[root.id] == "운영체제/README.md"
    assert paths[a.id] == "운영체제/메모.md"
    assert paths[b.id] == "운영체제/메모 (0000).md".replace("0000", uid(3).replace("-", "")[:4])
    assert paths[r.id].startswith("운영체제/README (")


def test_paths_long_names_are_shortened():
    nodes = {}
    parent = None
    for i in range(1, 7):
        n = _node(i, "아주 긴 제목" * 8, parent)
        nodes[n.id] = n
        if parent:
            nodes[uid(parent)].children = [n.id]
        parent = i
    paths = PathPlanner(nodes, [uid(1)], {}).plan()
    assert all(len("notes/" + p) <= 260 for p in paths.values())
    assert len("notes/" + paths[uid(6)]) <= 180 + 60


def test_heading_4_is_supported():
    out = render([block("heading_4", "h", rich_text=[text("작은 제목")])]).body
    assert "##### 작은 제목" in out and "지원하지 않는" not in out


def test_callout_without_text_does_not_start_with_blank_lines():
    blocks = [block("callout", "c", rich_text=[], icon=None, children=[paragraph("p", text("내용"))])]
    assert render(blocks).body == "# 제목\n\n> 내용\n"


def test_config_date_prefix(tmp_path: Path):
    path = tmp_path / "config.yml"
    path.write_text("database_files:\n  date_prefix: false\n  date_property:\n"
                    "    https://www.notion.so/x-1a2b3c4d5e6f47a8b9c0d1e2f3a4b5c6: 수업일\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.date_prefix is False
    assert cfg.date_property == {"1a2b3c4d-5e6f-47a8-b9c0-d1e2f3a4b5c6": "수업일"}
    assert load_config(tmp_path / "없음.yml").date_prefix is True
    path.write_text("database_files:\n  date_prefix: 네\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="2번째 줄"):
        load_config(path)
