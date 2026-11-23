"""Notion API 호출, 재시도, 요청 간격(명세서 4.6절).

- 초당 평균 3회를 넘지 않도록 요청 사이에 간격을 둔다.
- 429는 `Retry-After`만큼 기다리고, 5xx와 네트워크 오류는 간격을 늘려 최대 5회 다시 시도한다.
- 기다림이 마감 시각을 넘기면 기다리지 않고 `DeadlineReached`를 낸다.
- `Notion-Version`은 `2026-03-11`로 고정한다.

공식 클라이언트(notion-client) 대신 requests로 직접 호출한다. 데이터 소스 같은
새 엔드포인트와 버전 헤더를 이 파일 한 곳에서 통제하기 위함이다.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

from . import NOTION_VERSION
from .deadline import Deadline, DeadlineReached
from .log import log, minutes

API = "https://api.notion.com/v1"


class NotionError(Exception):
    def __init__(self, status: int, code: str | None, message: str | None):
        super().__init__(f"{status} {code or ''} {message or ''}".strip())
        self.status = status
        self.code = code
        self.message = message


class NotionClient:
    def __init__(
        self,
        token: str,
        deadline: Deadline,
        session: requests.Session | None = None,
        min_interval: float = 0.34,
        max_retries: int = 5,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._token = token
        self.deadline = deadline
        self._session = session or requests.Session()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self.request_count = 0

    # ------------------------------------------------------------------ 기본 호출
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if not self.deadline.can_wait(seconds):
            raise DeadlineReached()
        self._sleep(seconds)

    def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        attempt = 0
        while True:
            self.deadline.check()
            self._wait(self._last + self._min_interval - self._clock())
            try:
                resp = self._session.request(
                    method, API + path, headers=self._headers(), json=body, params=params, timeout=60
                )
            except requests.RequestException as exc:
                self._last = self._clock()
                if attempt >= self._max_retries:
                    raise NotionError(0, "network_error", str(exc)) from exc
                log(f"  네트워크 오류({type(exc).__name__}): {min(2**attempt, 30)}초 뒤 다시 시도 ({attempt + 1}/{self._max_retries})")
                self._wait(min(2**attempt, 30))
                attempt += 1
                continue
            self._last = self._clock()
            self.request_count += 1
            if self.request_count % 100 == 0:
                log(f"  … API 요청 {self.request_count}회, 남은 시간 {minutes(self.deadline.remaining())}")
            if resp.status_code == 429:
                if attempt >= self._max_retries:
                    raise NotionError(429, "rate_limited", "재시도 횟수를 넘김")
                try:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                except ValueError:
                    retry_after = 1.0
                log(f"  요청 한도 초과(429): {retry_after:.0f}초 기다린 뒤 다시 시도 ({attempt + 1}/{self._max_retries})")
                self._wait(retry_after)
                attempt += 1
                continue
            if resp.status_code >= 500:
                if attempt >= self._max_retries:
                    raise NotionError(resp.status_code, "server_error", resp.text[:200])
                log(f"  노션 서버 오류({resp.status_code}): {min(2**attempt, 30)}초 뒤 다시 시도 ({attempt + 1}/{self._max_retries})")
                self._wait(min(2**attempt, 30))
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                raise NotionError(resp.status_code, data.get("code"), data.get("message"))
            return resp.json()

    def _paginate(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Iterator[dict]:
        cursor = None
        while True:
            if method == "GET":
                p = dict(params or {})
                p["page_size"] = 100
                if cursor:
                    p["start_cursor"] = cursor
                data = self.request("GET", path, params=p)
            else:
                b = dict(body or {})
                b["page_size"] = 100
                if cursor:
                    b["start_cursor"] = cursor
                data = self.request(method, path, body=b)
            yield data
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    # ------------------------------------------------------------------ 엔드포인트
    def search(self) -> list[dict]:
        """통합이 읽을 수 있는 페이지와 데이터 소스. 매 실행 처음부터 끝까지 다시 조회한다."""
        results: list[dict] = []
        for data in self._paginate("POST", "/search", body={}):
            results.extend(data.get("results", []))
        return results

    def retrieve_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def retrieve_block(self, block_id: str) -> dict:
        return self.request("GET", f"/blocks/{block_id}")

    def block_children(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        for data in self._paginate("GET", f"/blocks/{block_id}/children"):
            out.extend(data.get("results", []))
        return out

    def retrieve_database(self, database_id: str) -> dict:
        return self.request("GET", f"/databases/{database_id}")

    def retrieve_data_source(self, data_source_id: str) -> dict:
        return self.request("GET", f"/data_sources/{data_source_id}")

    def query_data_source(self, data_source_id: str) -> tuple[list[dict], bool]:
        """데이터 소스의 모든 항목. 두 번째 값은 API가 결과가 불완전하다고 알렸는지 여부.

        불완전 응답의 정확한 형태는 구현 후 확인한다(명세서 13절). 지금은 응답의
        `request_status`나 `incomplete` 표시를 본다.
        """
        results: list[dict] = []
        incomplete = False
        body = {"sorts": [{"timestamp": "created_time", "direction": "ascending"}]}
        for data in self._paginate("POST", f"/data_sources/{data_source_id}/query", body=body):
            results.extend(data.get("results", []))
            if _looks_incomplete(data):
                incomplete = True
        return results, incomplete

    def page_property_items(self, page_id: str, property_id: str) -> list[dict]:
        out: list[dict] = []
        for data in self._paginate("GET", f"/pages/{page_id}/properties/{property_id}"):
            if data.get("object") == "list":
                out.extend(data.get("results", []))
            else:
                out.append(data)
                return out
        return out


def _looks_incomplete(data: dict[str, Any]) -> bool:
    status = data.get("request_status")
    if isinstance(status, dict):
        status = status.get("type") or status.get("status")
    if isinstance(status, str) and status.lower() == "incomplete":
        return True
    return bool(data.get("incomplete"))
