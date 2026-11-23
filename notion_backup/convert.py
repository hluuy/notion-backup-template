"""노션 블록을 Markdown으로(명세서 5절).

렌더링 결과의 다른 페이지 링크는 경로 대신 페이지 ID 자리 표시로 남긴다.
경로는 반영 직전에 확정되므로, 자리 표시는 `resolve_links`가 그때 상대 링크로 바꾼다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .ids import is_notion_link, normalize_id, notion_url
from .naming import escape_markdown
from .properties import plain_text

PLACEHOLDER = re.compile("\x00L:([0-9a-f-]{36})\x00")


def placeholder(page_id: str) -> str:
    return f"\x00L:{page_id}\x00"


def resolve_links(text: str, target_of: Callable[[str], str]) -> str:
    return PLACEHOLDER.sub(lambda m: target_of(m.group(1)), text)


class Context(Protocol):
    def title_of(self, page_id: str) -> str: ...
    def image(self, block: dict, caption: str) -> str: ...
    def synced_children(self, block_id: str) -> tuple[list[dict], str | None]: ...
    def warn(self, message: str) -> None: ...


@dataclass
class Rendered:
    body: str
    links: set[str] = field(default_factory=set)
    children: list[tuple[str, str]] = field(default_factory=list)  # (id, "page" | "database")
    synced_pages: set[str] = field(default_factory=set)


CODE_LANG = {
    "plain text": "",
    "c++": "cpp",
    "c#": "csharp",
    "f#": "fsharp",
    "objective-c": "objectivec",
    "shell": "shell",
    "bash": "bash",
    "markdown": "markdown",
    "javascript": "javascript",
    "typescript": "typescript",
    "python": "python",
    "java": "java",
    "go": "go",
    "rust": "rust",
    "sql": "sql",
    "json": "json",
    "yaml": "yaml",
    "html": "html",
    "css": "css",
    "latex": "latex",
    "visual basic": "vb",
    "powershell": "powershell",
    "docker": "dockerfile",
    "makefile": "makefile",
    "mermaid": "mermaid",
}

LIST_TYPES = {"bulleted_list_item", "numbered_list_item", "to_do"}


class Renderer:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.links: set[str] = set()
        self.children: list[tuple[str, str]] = []
        self.synced_pages: set[str] = set()

    # ------------------------------------------------------------------ 진입점
    def render_page(self, title: str, blocks: list[dict]) -> Rendered:
        heading = "# " + escape_markdown(title or "제목 없음")
        body = self.blocks(blocks)
        text = heading + ("\n\n" + body if body else "") + "\n"
        return Rendered(text, self.links, self.children, self.synced_pages)

    # ------------------------------------------------------------------ 글자
    def _link(self, page_id: str) -> str:
        self.links.add(page_id)
        return placeholder(page_id)

    def rich(self, parts: list[dict] | None) -> str:
        out = []
        for part in parts or []:
            out.append(self._rich_one(part))
        return "".join(out)

    def _rich_one(self, part: dict) -> str:
        kind = part.get("type")
        ann = part.get("annotations") or {}
        if kind == "equation":
            return "$" + part.get("equation", {}).get("expression", "") + "$"
        if kind == "mention":
            return self._mention(part)
        text = part.get("text", {}).get("content", part.get("plain_text", ""))
        href = (part.get("text", {}).get("link") or {}).get("url") or part.get("href")
        if ann.get("code"):
            body = "`" + text.replace("`", "ˋ") + "`"
        else:
            body = escape_markdown(text)
        body = _wrap(body, ann)
        if href:
            target = self._href(href)
            body = f"[{body}]({target})"
        return body

    def _href(self, href: str) -> str:
        if is_notion_link(href):
            page_id = normalize_id(href)
            if page_id:
                return self._link(page_id)
        return href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")

    def _mention(self, part: dict) -> str:
        mention = part.get("mention") or {}
        mtype = mention.get("type")
        if mtype in ("page", "database"):
            target = (mention.get(mtype) or {}).get("id")
            if target:
                title = escape_markdown(part.get("plain_text") or self.ctx.title_of(target))
                return f"[{title}]({self._link(target)})"
        if mtype == "date":
            date = mention.get("date") or {}
            start, end = date.get("start", ""), date.get("end")
            start = start[:10] if start else ""
            return f"{start} ~ {end[:10]}" if end else start
        if mtype == "user":
            name = (mention.get("user") or {}).get("name")
            return "@" + escape_markdown(name or "알 수 없음")
        if mtype == "link_preview":
            url = (mention.get("link_preview") or {}).get("url", "")
            return f"[{escape_markdown(url)}]({url})"
        return escape_markdown(part.get("plain_text", ""))

    # ------------------------------------------------------------------ 블록
    def blocks(self, blocks: list[dict]) -> str:
        out: list[str] = []
        prev_list = False
        for block in blocks:
            text = self.block(block)
            if text is None:
                continue
            is_list = block.get("type") in LIST_TYPES
            if out:
                out.append("\n" if (is_list and prev_list) else "\n\n")
            out.append(text)
            prev_list = is_list
        return "".join(out)

    def _children(self, block: dict) -> str:
        return self.blocks(block.get("children") or [])

    def block(self, block: dict) -> str | None:
        kind = block.get("type")
        data = block.get(kind) or {}
        handler = getattr(self, f"_b_{kind}", None)
        if handler is not None:
            return handler(block, data)
        self.ctx.warn(f"지원하지 않는 블록: {kind}")
        return f"<!-- 지원하지 않는 블록: {kind} -->"

    def _b_paragraph(self, block, data):
        text = self.rich(data.get("rich_text"))
        children = self._children(block)
        if children:
            return (text + "\n\n" if text else "") + children
        return text if text else None

    def _heading(self, level: int, block, data):
        text = "#" * (level + 1) + " " + self.rich(data.get("rich_text"))
        children = self._children(block)
        return text + ("\n\n" + children if children else "")

    def _b_heading_1(self, block, data):
        return self._heading(1, block, data)

    def _b_heading_2(self, block, data):
        return self._heading(2, block, data)

    def _b_heading_3(self, block, data):
        return self._heading(3, block, data)

    def _b_heading_4(self, block, data):
        return self._heading(4, block, data)

    def _list_item(self, marker: str, block, data):
        text = marker + self.rich(data.get("rich_text"))
        children = self._children(block)
        if children:
            text += "\n\n" + _indent(children, "    ")
        return text

    def _b_bulleted_list_item(self, block, data):
        return self._list_item("- ", block, data)

    def _b_numbered_list_item(self, block, data):
        return self._list_item("1. ", block, data)

    def _b_to_do(self, block, data):
        mark = "- [x] " if data.get("checked") else "- [ ] "
        return self._list_item(mark, block, data)

    def _b_toggle(self, block, data):
        title = self.rich(data.get("rich_text"))
        head = f"**{title}**" if title else ""
        children = self._children(block)
        if head and children:
            return head + "\n\n" + children
        return head or children or None

    def _b_quote(self, block, data):
        text = self.rich(data.get("rich_text"))
        children = self._children(block)
        whole = "\n\n".join(part for part in (text, children) if part)  # 텍스트 없이 하위만 있으면 빈 줄로 시작하지 않게
        return _quote(whole) if whole else None

    def _b_callout(self, block, data):
        icon = data.get("icon") or {}
        emoji = icon.get("emoji") if icon.get("type") == "emoji" else None
        text = self.rich(data.get("rich_text"))
        if emoji:
            text = f"{emoji} {text}" if text else emoji
        children = self._children(block)
        whole = "\n\n".join(part for part in (text, children) if part)
        return _quote(whole) if whole else None

    def _b_code(self, block, data):
        language = (data.get("language") or "").lower()
        lang = CODE_LANG.get(language, language.replace(" ", ""))
        code = plain_text(data.get("rich_text"))
        fence = "```"
        while fence in code:
            fence += "`"
        text = f"{fence}{lang}\n{code}\n{fence}"
        caption = self.rich(data.get("caption"))
        if caption:
            text += f"\n\n*{caption}*"
        return text

    def _b_equation(self, block, data):
        return "$$\n" + data.get("expression", "") + "\n$$"

    def _b_divider(self, block, data):
        return "---"

    def _b_table(self, block, data):
        rows = [r for r in block.get("children") or [] if r.get("type") == "table_row"]
        if not rows:
            return None
        width = max(len(r["table_row"].get("cells") or []) for r in rows) or 1
        lines = []
        for i, row in enumerate(rows):
            cells = list(row["table_row"].get("cells") or [])
            cells += [[]] * (width - len(cells))
            texts = [re.sub(r"(?<!\\)\|", r"\\|", self.rich(c).replace("\n", "<br>")) for c in cells]
            lines.append("| " + " | ".join(texts) + " |")
            if i == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        return "\n".join(lines)

    def _b_image(self, block, data):
        caption = plain_text(data.get("caption"))
        return self.ctx.image(block, caption)

    def _web(self, data, label: str | None = None):
        url = data.get("url", "")
        caption = self.rich(data.get("caption"))
        text = caption or escape_markdown(url)
        if label:
            text = f"{label}: {text}"
        return f"[{text}]({url})"

    def _b_bookmark(self, block, data):
        return self._web(data)

    def _b_embed(self, block, data):
        return self._web(data)

    def _b_link_preview(self, block, data):
        return self._web(data)

    def _attachment(self, data):
        name = data.get("name")
        if not name:
            source = (data.get("file") or data.get("external") or {}).get("url", "")
            name = source.split("?")[0].rsplit("/", 1)[-1] or "첨부파일"
        return f"📎 {escape_markdown(name)} (첨부파일, 백업 안 됨)"

    def _b_video(self, block, data):
        if data.get("type") == "external":
            url = data["external"].get("url", "")
            return f"[동영상: {escape_markdown(url)}]({url})"
        return self._attachment(data)

    def _b_file(self, block, data):
        return self._attachment(data)

    def _b_pdf(self, block, data):
        return self._attachment(data)

    def _b_audio(self, block, data):
        return self._attachment(data)

    def _b_child_page(self, block, data):
        page_id = normalize_id(block["id"])
        self.children.append((page_id, "page"))
        return f"[{escape_markdown(data.get('title') or '제목 없음')}]({self._link(page_id)})"

    def _b_child_database(self, block, data):
        db_id = normalize_id(block["id"])
        self.children.append((db_id, "database"))
        return f"[{escape_markdown(data.get('title') or '제목 없음')}]({self._link(db_id)})"

    def _b_link_to_page(self, block, data):
        target = data.get("page_id") or data.get("database_id")
        if not target:
            return None
        target = normalize_id(target)
        return f"[{escape_markdown(self.ctx.title_of(target))}]({self._link(target)})"

    def _b_synced_block(self, block, data):
        source = data.get("synced_from")
        if not source:
            return self._children(block) or None
        children, page_id = self.ctx.synced_children(source.get("block_id"))
        if page_id:
            self.synced_pages.add(page_id)
        return self.blocks(children) or None

    def _b_column_list(self, block, data):
        parts = [self._children(col) for col in block.get("children") or []]
        return "\n\n".join(p for p in parts if p) or None

    def _b_column(self, block, data):
        return self._children(block) or None

    def _b_table_of_contents(self, block, data):
        return None

    def _b_breadcrumb(self, block, data):
        return None


def _wrap(text: str, ann: dict) -> str:
    if not text.strip():
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if ann.get("bold"):
        core = f"**{core}**"
    if ann.get("italic"):
        core = f"*{core}*"
    if ann.get("strikethrough"):
        core = f"~~{core}~~"
    if ann.get("underline"):
        core = f"<u>{core}</u>"
    return lead + core + trail


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def _quote(text: str) -> str:
    return "\n".join(("> " + line) if line else ">" for line in text.split("\n"))


def link_target(page_id: str, from_file: str, path_of: Callable[[str], str | None]) -> str:
    """자리 표시를 실제 링크 대상으로: 백업된 파일이면 상대 링크, 아니면 노션 원본 URL."""
    from .naming import relative_link

    target = path_of(page_id)
    if target:
        return relative_link(target, from_file)
    return notion_url(page_id)
