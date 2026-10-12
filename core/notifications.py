"""Authenticated, bounded task completion notifications using the shared speaker."""

import asyncio
import hmac
import ipaddress
import logging
import string
import time
from contextlib import suppress

from aiohttp import web

logger = logging.getLogger(__name__)


def validate_hook_config(config):
    if not config.hook_token or not config.hook_token.strip():
        raise ValueError("HOOK_TOKEN is required when HOOK_ENABLED=true")
    if not config.hook_host or not 1 <= config.hook_port <= 65535:
        raise ValueError("HOOK_HOST and HOOK_PORT must specify a valid listener")
    try:
        if not config.hook_text.strip() or len(config.hook_text) > 300:
            raise ValueError()
        for _, name, spec, conversion in string.Formatter().parse(config.hook_text):
            if name is not None and (
                name not in {"source", "event_id", "task"} or spec or conversion
            ):
                raise ValueError()
    except ValueError:
        raise ValueError("HOOK_TEXT must be at most 300 characters with only {source}, {event_id}, {task} placeholders") from None


class NotificationService:
    """202 means queued; dedup is in-memory and expires after successful speech."""

    def __init__(self, config, xiaoai, *, queue_size=64, dedup_size=4096,
                 dedup_ttl=3600.0, worker_timeout=180.0):
        validate_hook_config(config)
        if min(queue_size, dedup_size, dedup_ttl, worker_timeout) <= 0:
            raise ValueError("Notification limits must be positive")
        self.config = config
        self.xiaoai = xiaoai
        self.queue = asyncio.Queue(maxsize=queue_size)
        self._dedup = {}  # (source, event_id) -> expiry; None while queued/speaking
        self._dedup_size = dedup_size
        self._dedup_ttl = dedup_ttl
        self._worker_timeout = worker_timeout
        self._worker_task = None
        self._runner = None
        self._accepting = False
        self.app = web.Application(client_max_size=16 * 1024)
        self.app.router.add_get("/health", self._health)
        self.app.router.add_post("/hooks/task-completed", self._task_completed)

    async def start(self):
        self._runner = web.AppRunner(self.app, access_log=None)
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.config.hook_host, self.config.hook_port)
            await site.start()
        except Exception as exc:
            await self.close()
            raise RuntimeError(
                f"Cannot bind notification webhook to {self.config.hook_host}:{self.config.hook_port}"
            ) from exc
        self._worker_task = asyncio.create_task(self._worker(), name="notification-tts")
        self._accepting = True
        logger.info("Notification webhook listening on %s:%s", self.config.hook_host, self.config.hook_port)

    async def close(self):
        self._accepting = False
        try:
            if self._runner:
                await self._runner.cleanup()
        finally:
            self._runner = None
            if self._worker_task:
                self._worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._worker_task
                self._worker_task = None
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            self._dedup.clear()

    async def _health(self, request):
        try:
            local = ipaddress.ip_address(request.remote).is_loopback
        except (ValueError, TypeError):
            local = False
        if not local:
            return web.json_response({"error": "local access only"}, status=403)
        return web.json_response({"status": "ok"})

    async def _task_completed(self, request):
        scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
        # Bytes also permit non-ASCII invalid credentials without compare_digest errors.
        valid_token = hmac.compare_digest(token.encode("utf-8"), self.config.hook_token.encode("utf-8"))
        if scheme.lower() != "bearer" or not separator or not valid_token:
            return web.json_response({"error": "unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": "Bearer"})
        if request.content_type != "application/json":
            return web.json_response({"error": "expected JSON object"}, status=400)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        limits = {"source": 80, "event_id": 200, "task": 120, "text": 300}
        if (not isinstance(body, dict) or not {"source", "event_id"} <= body.keys()
                or body.keys() - limits.keys()):
            return web.json_response({"error": "invalid fields"}, status=400)
        for name, value in body.items():
            if (not isinstance(value, str) or len(value) > limits[name]
                    or (name != "task" and not value.strip())):
                return web.json_response({"error": f"invalid {name}"}, status=400)
        text = body.get("text")
        if text is None:
            text = self.config.hook_text.format(
                source=body["source"], event_id=body["event_id"], task=body.get("task", "")
            )
        if not text.strip() or len(text) > 300:
            return web.json_response({"error": "rendered text exceeds limits"}, status=400)

        now = time.monotonic()
        for key, expiry in list(self._dedup.items()):
            if expiry is not None and expiry <= now:
                del self._dedup[key]
        key = (body["source"], body["event_id"])
        if key in self._dedup:
            return web.json_response({"status": "duplicate"}, status=200)
        # No await between checking capacity, enqueueing, and reserving dedup.
        if not self._accepting or len(self._dedup) >= self._dedup_size:
            return web.json_response({"error": "notification capacity exhausted"}, status=503)
        try:
            self.queue.put_nowait((key, text))
        except asyncio.QueueFull:
            return web.json_response({"error": "notification queue full"}, status=503)
        self._dedup[key] = None
        return web.json_response({"status": "queued"}, status=202)

    async def _worker(self):
        while True:
            key, text = await self.queue.get()
            try:
                accepted = await asyncio.wait_for(
                    self.xiaoai.announce_notification(text), timeout=self._worker_timeout
                )
                if not accepted:
                    raise RuntimeError("Speaker rejected notification")
                self._dedup[key] = time.monotonic() + self._dedup_ttl
                logger.info("Notification speech window completed (audibility not measured): %s", key[0])
            except asyncio.CancelledError:
                self._dedup.pop(key, None)
                raise
            except Exception:
                self._dedup.pop(key, None)
                logger.exception("Notification speech failed; event may be retried")
            finally:
                self.queue.task_done()
