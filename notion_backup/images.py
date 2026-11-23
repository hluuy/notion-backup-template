"""이미지 다운로드(명세서 6.5절)."""

from __future__ import annotations

import posixpath
from pathlib import Path
from urllib.parse import urlparse

import requests

from .deadline import Deadline, DeadlineReached
from .ids import compact

KNOWN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff", ".heic", ".avif"}


class TooLarge(Exception):
    pass


def image_url(block: dict) -> tuple[str, str]:
    """(URL, 종류). 종류는 'file'(노션에 올린 이미지) 또는 'external'."""
    data = block.get("image") or {}
    kind = data.get("type", "external")
    return (data.get(kind) or {}).get("url", ""), kind


def source_id(url: str, kind: str) -> str:
    """노션에 올린 이미지는 서명이 매번 바뀌므로 쿼리스트링을 뺀 경로, 외부 이미지는 URL 전체."""
    if kind == "file":
        return url.split("?")[0]
    return url


def file_name(block_id: str, url: str) -> str:
    ext = posixpath.splitext(urlparse(url).path)[1].lower()
    if ext not in KNOWN_EXT:
        ext = ".png"
    return compact(block_id) + ext


class Downloader:
    def __init__(self, deadline: Deadline, max_bytes: int, session: requests.Session | None = None):
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.session = session or requests.Session()

    def download(self, url: str, dest: Path) -> None:
        """dest에 저장한다. 크기 초과면 TooLarge, 그 밖의 실패는 예외 그대로."""
        self.deadline.check()
        timeout = max(5.0, min(60.0, self.deadline.remaining()))
        with self.session.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > self.max_bytes:
                raise TooLarge()
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise TooLarge()
                        if self.deadline.passed():
                            raise DeadlineReached()
                        fh.write(chunk)
                tmp.replace(dest)
            finally:
                if tmp.exists():
                    tmp.unlink()
