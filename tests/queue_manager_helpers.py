from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient
from app.api_errors import APIError

from helpers import FakeUpstream


def _queued_user_names(client: TestClient) -> list[str]:
    queue = client.get("/admin/api/queue", auth=("admin", "admin123")).json()
    return [item["user_name"] for item in queue["queued"]]


def _wait_until(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


async def _wait_until_async(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


class _NoopUsageLogs:
    def mark_running(self, *args, **kwargs):
        pass

    def mark_success(self, *args, **kwargs):
        pass

    def mark_failed(self, *args, **kwargs):
        pass

    def insert_retry_attempt(self, *args, **kwargs):
        pass

    def update_image_urls(self, *args, **kwargs):
        pass


class One429ThenSuccessfulUpstream(FakeUpstream):
    def __init__(self, first_release_event: threading.Event, *, fail_label: str):
        super().__init__()
        self.first_release_event = first_release_event
        self.fail_label = fail_label
        self.failed_once = False
        self.started_labels = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        if label == self.fail_label and not self.failed_once:
            self.failed_once = True
            await asyncio.to_thread(self.first_release_event.wait)
            raise APIError(
                "Too many requests",
                request=payload,
                response={"message": "Too many requests"},
                code="429",
            )
        return label.encode("utf-8")


class FirstRequestBlockingLabelUpstream(FakeUpstream):
    def __init__(self, first_release_event: threading.Event):
        super().__init__()
        self.first_release_event = first_release_event
        self.started_labels = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        if len(self.started_labels) == 1:
            await asyncio.to_thread(self.first_release_event.wait)
        return label.encode("utf-8")


class TimeoutByLabelUpstream(FakeUpstream):
    def __init__(self, *, timeout_label: str):
        super().__init__()
        self.timeout_label = timeout_label
        self.started_labels = []
        self.started_at = []

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        self.started_at.append(time.monotonic())
        if label == self.timeout_label:
            await asyncio.Event().wait()
        return label.encode("utf-8")


class CancellationResistantTimeoutUpstream(FakeUpstream):
    def __init__(self, *, cleanup_event: asyncio.Event):
        super().__init__()
        self.cleanup_event = cleanup_event
        self.started_labels = []
        self.finished_labels = []
        self.active = 0
        self.max_active = 0
        self.cancel_seen = False

    async def generate_image_payload_zip(self, payload):
        label = payload["label"]
        self.started_labels.append(label)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if label == "slow":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancel_seen = True
                    await self.cleanup_event.wait()
                    return b"late"
            return label.encode("utf-8")
        finally:
            self.active -= 1
            self.finished_labels.append(label)


class _RecordingQuota:
    def __init__(self):
        self.released = []

    def release(self, user_id: int, cost: int):
        self.released.append((user_id, cost))


class _RecordingUsageLogs(_NoopUsageLogs):
    def __init__(self):
        self.failed = []

    def mark_failed(self, request_id, *, queued_ms, error_code, error_message, upstream_ms=None, attempt_number=0):
        self.failed.append(
            {
                "request_id": request_id,
                "error_code": error_code,
                "error_message": error_message,
                "attempt_number": attempt_number,
            }
        )


def _api_429() -> APIError:
    return APIError(
        "Too many requests",
        request={},
        response={"message": "Too many requests"},
        code="429",
    )
