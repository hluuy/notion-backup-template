"""시간 예산(명세서 4.6절).

마감 시각은 워크플로 작업이 시작된 시각에서 25분 뒤다. 작업 시작 시각은 워크플로
첫 단계가 환경 변수 `JOB_STARTED_AT`(epoch 초)으로 넘긴다. 없으면(로컬 실행)
프로그램 시작 시각을 쓴다.
"""

from __future__ import annotations

import time

BUDGET_SECONDS = 25 * 60


class DeadlineReached(Exception):
    """마감 시각이 지나 새 요청을 멈춰야 할 때."""


class Deadline:
    def __init__(self, seconds_left: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds_left

    @classmethod
    def from_job_start(cls, job_started_at: float | None, budget: float = BUDGET_SECONDS, clock=time.monotonic):
        now_epoch = time.time()
        start = job_started_at if job_started_at else now_epoch
        return cls(start + budget - now_epoch, clock)

    @classmethod
    def unlimited(cls):
        return cls(10**9)

    def remaining(self) -> float:
        return self._end - self._clock()

    def passed(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.passed():
            raise DeadlineReached()

    def can_wait(self, seconds: float) -> bool:
        return self.remaining() > seconds

    def sub(self, seconds: float) -> "Deadline":
        """이 마감보다 이르거나 같은, `seconds`초 뒤의 마감(순환 재탐색 몫 등)."""
        return Deadline(min(seconds, self.remaining()), self._clock)
