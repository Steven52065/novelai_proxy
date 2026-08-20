from __future__ import annotations

import asyncio
import threading

from app.api_errors import APIError

from helpers import FakeUpstream


class UpstreamApiErrorFake(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        raise APIError(
            "secret upstream detail",
            request=payload,
            response={"message": "secret upstream response", "token": "secret-token"},
            code="418",
        )

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        raise APIError(
            "secret suggest detail",
            request={"model": model, "prompt": prompt, "lang": lang},
            response={"message": "secret suggest response", "token": "secret-token"},
            code="429",
        )


class InternalErrorFake(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        raise RuntimeError("secret internal detail")

    async def suggest_tags(self, model: str, prompt: str, lang: str = "en"):
        raise RuntimeError("secret suggest internal detail")


class NeverReturningUpstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(0)
        self.last_generate_payload = payload
        await asyncio.Event().wait()


class Always429Upstream(FakeUpstream):
    async def generate_image_payload_zip(self, payload):
        self.generate_started_at.append(threading.get_native_id())
        raise APIError(
            "Too many requests",
            request=payload,
            response={"message": "Too many requests"},
            code="429",
        )
