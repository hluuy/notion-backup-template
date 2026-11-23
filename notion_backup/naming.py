"""파일 이름과 상대 링크 규칙(명세서 5.3절, 6.1절)."""

from __future__ import annotations

import posixpath
import re

from .ids import compact

INVALID_CHARS = set('\\/:*?"<>|')
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
UNTITLED = "제목 없음"
MAX_NAME = 100
SHORT_NAME = 40
MAX_PATH = 180  # `notes/`로 시작하는 저장소 안 경로의 상한(글자 수)
IMAGE_MARGIN = len("/images/") + 32 + len(".jpeg")  # 이 폴더에 이미지가 들어갈 자리


def sanitize(title: str) -> str:
    out = []
    for ch in title or "":
        if ch in INVALID_CHARS or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    name = "".join(out).strip()
    name = name[:MAX_NAME].rstrip(" .")
    name = name.strip()
    if not name:
        name = UNTITLED
    if name.lower() in WINDOWS_RESERVED:
        name += "_"
    return name


def with_suffix(name: str, page_id: str, length: int) -> str:
    suffix = compact(page_id) if length >= 32 else compact(page_id)[:length]
    return f"{name} ({suffix})"


def shorten(name: str) -> str:
    return name[:SHORT_NAME].rstrip(" .") or UNTITLED


_SAFE = set("-_.~/!$&'()*+,;=:@")


def encode_path(path: str) -> str:
    """상대 경로의 공백과 특수문자는 URL 인코딩하고 한글 등 ASCII 밖 문자는 그대로 둔다."""
    out = []
    for ch in path:
        if ord(ch) > 127 or ch.isalnum() or ch in _SAFE:
            out.append(ch)
        else:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
    text = "".join(out)
    # Markdown 링크 대상에서 괄호는 짝이 맞지 않으면 깨지므로 인코딩한다.
    return text.replace("(", "%28").replace(")", "%29")


def relative_link(target: str, from_file: str) -> str:
    """`notes/` 기준 경로 target을 from_file(같은 기준)에서 가리키는 인코딩된 상대 링크."""
    start = posixpath.dirname(from_file) or "."
    return encode_path(posixpath.relpath(target, start))


_ESCAPE = re.compile(r"([\\`*_\[\]<>~$|])")


def escape_markdown(text: str) -> str:
    """본문 특수문자가 서식으로 오인되지 않게 이스케이프한다(명세서 5.2절)."""
    text = _ESCAPE.sub(r"\\\1", text)
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        lead = line[: len(line) - len(stripped)]
        numbered = re.match(r"^(\d+)([.)])(\s)", stripped)
        if numbered:
            stripped = numbered.group(1) + "\\" + stripped[len(numbered.group(1)):]
        elif stripped.startswith(("#", "+ ", "- ")):
            stripped = "\\" + stripped
        lines.append(lead + stripped)
    return "\n".join(lines)
