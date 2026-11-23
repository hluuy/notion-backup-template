"""자동으로 만드는 목록 파일: 최상위 목차, 데이터베이스 README, CSV(명세서 6.2절, 6.3절)."""

from __future__ import annotations

import csv
import io
import posixpath
from typing import Callable

from .ids import notion_url
from .naming import escape_markdown, relative_link
from .paths import README, item_date, link_path
from .properties import csv_cell
from .tree import Node

NOT_YET = "목록을 아직 받지 못했습니다. 다음 실행에서 다시 시도합니다."


class ListBuilder:
    """경로와 '실제로 저장된 파일'을 보고 목록 파일 내용을 만든다.

    has_file(page_id) -> 그 페이지의 파일이 이번 실행 뒤 실제로 있는지.
    """

    def __init__(self, nodes: dict[str, Node], paths: dict[str, str], csv_names: dict[str, str],
                 single_source: dict[str, bool], has_file: Callable[[str], bool]):
        self.nodes = nodes
        self.paths = paths
        self.csv_names = csv_names
        self.single_source = single_source
        self.has_file = has_file

    def _link(self, target_id: str, from_file: str) -> str:
        node = self.nodes.get(target_id)
        path = self.paths.get(target_id)
        if node and path and (node.kind != "page" or self.has_file(target_id)):
            return relative_link(link_path(node, path), from_file)
        return notion_url(target_id)

    # ------------------------------------------------------------------ 최상위 목차
    def root_readme(self, roots: list[str]) -> str:
        lines = ["# 노션 백업", "", "최상위 페이지 목록입니다. 이 파일은 백업할 때마다 자동으로 다시 만들어집니다.", ""]
        items = [r for r in roots if r in self.nodes and r in self.paths]
        for root in sorted(items, key=lambda r: (self.nodes[r].title or "", r)):
            title = escape_markdown(self.nodes[root].title or "제목 없음")
            lines.append(f"- [{title}]({self._link(root, README)})")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ 데이터베이스
    def files_for_database(self, db_id: str) -> dict[str, str]:
        """데이터베이스 하나에 대해 만들 파일들(경로 -> 내용). 목록을 끝까지 받지 못한 데이터 소스는 뺀다."""
        db = self.nodes[db_id]
        folder = self.paths[db_id]
        sources = [s for s in db.children if s in self.nodes]
        out: dict[str, str] = {}
        readme = posixpath.join(folder, README)
        header = [f"# {escape_markdown(db.title or '제목 없음')}", ""]
        if db.description:
            header += [escape_markdown(db.description), ""]
        if self.single_source.get(db_id, True):
            ds = self.nodes[sources[0]] if sources else None
            if ds is None:
                out[readme] = "\n".join(header) + "\n"
                return out
            if ds.list_status != "ok":
                return out  # 기존 목록 유지(없으면 아래 placeholder_for에서 처리)
            out[readme] = "\n".join(header + self._table(ds, readme)) + "\n"
            out[self.csv_names[ds.id]] = self._csv(ds)
            return out
        toc = list(header) + ["## 데이터 소스", ""]
        for ds_id in sources:
            ds = self.nodes[ds_id]
            ds_readme = posixpath.join(self.paths[ds_id], README)
            toc.append(f"- [{escape_markdown(ds.title or '제목 없음')}]({relative_link(ds_readme, readme)})")
            if ds.list_status == "ok":
                body = [f"# {escape_markdown(ds.title or '제목 없음')}", ""] + self._table(ds, ds_readme)
                out[ds_readme] = "\n".join(body) + "\n"
                out[self.csv_names[ds_id]] = self._csv(ds)
        out[readme] = "\n".join(toc) + "\n"
        return out

    def _rows(self, ds: Node) -> list[Node]:
        """날짜 속성이 있으면 날짜순(날짜 없는 항목은 맨 뒤), 같으면 만든 순서."""
        items = [self.nodes[i] for i in ds.children if i in self.nodes]
        return sorted(items, key=lambda n: (item_date(n, ds) or "9999-99-99", n.created or "", n.id))

    def _table(self, ds: Node, readme: str) -> list[str]:
        columns = ds.columns or ["이름"]
        title_col = columns[0]
        rest = columns[1:]
        lines = ["| " + " | ".join(escape_markdown(c) for c in [title_col] + rest) + " |",
                 "| " + " | ".join(["---"] * (len(rest) + 1)) + " |"]
        for item in self._rows(ds):
            title = escape_markdown(item.title or "제목 없음")  # `|`도 이미 이스케이프된다
            cells = [f"[{title}]({self._link(item.id, readme)})"]
            for col in rest:
                value = csv_cell((item.props or {}).get(col))
                cells.append(escape_markdown(value).replace("\n", "<br>"))
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    def _csv(self, ds: Node) -> str:
        columns = ds.columns or ["이름"]
        csv_path = self.csv_names[ds.id]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns + ["파일 경로"])
        for item in self._rows(ds):
            row = [item.title]
            for col in columns[1:]:
                row.append(csv_cell((item.props or {}).get(col)))
            path = self.paths.get(item.id)
            row.append(relative_link(path, csv_path) if path and self.has_file(item.id) else "")
            writer.writerow(row)
        return buf.getvalue()

    def placeholder_for(self, db_id: str) -> dict[str, str]:
        """첫 백업이라 기존 목록이 없는데 목록을 끝까지 받지 못한 경우."""
        db = self.nodes[db_id]
        readme = posixpath.join(self.paths[db_id], README)
        return {readme: f"# {escape_markdown(db.title or '제목 없음')}\n\n{NOT_YET}\n"}
