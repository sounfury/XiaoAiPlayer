"""Offline webhook, lifecycle, and speaker guard tests; no real Xiaomi calls."""

import asyncio
import hmac
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer, unused_port

from config import Config
from core.notifications import NotificationService
from core.xiaoai import XiaoAiService
from main import main_loop


def hook_config(**overrides):
    values = dict(hook_enabled=True, hook_host="127.0.0.1", hook_port=8765,
                  hook_token="test-secret", hook_text="{source}的任务已完成")
    values.update(overrides)
    return Config(**values)


class ConfigTests(unittest.TestCase):
    def test_hook_defaults_are_disabled_and_local(self):
        with patch.dict("os.environ", {}, clear=True):
            config = Config(mi_user="user", mi_pass="pass")
        self.assertFalse(config.hook_enabled)
        self.assertEqual(config.hook_host, "127.0.0.1")
        self.assertEqual(config.hook_port, 8765)
        self.assertEqual(config.hook_text, "{source}的任务已完成")
        config.validate()

    def test_enabled_hook_requires_token_and_valid_template(self):
        for overrides in [dict(hook_token=""), dict(hook_token="  "),
                          dict(hook_port=0), dict(hook_port=65536),
                          dict(hook_host=""), dict(hook_text="{unknown}"),
                          dict(hook_text="{source.__class__}"),
                          dict(hook_text="{source:100000}"),
                          dict(hook_text="{source!r}"), dict(hook_text="{"),
                          dict(hook_text=""), dict(hook_text="a" * 301)]:
            with self.subTest(overrides=overrides):
                config = hook_config(mi_user="user", mi_pass="pass", **overrides)
                with self.assertRaises(ValueError):
                    config.validate()
                with self.assertRaises(ValueError):
                    NotificationService(config, Mock())

    def test_hook_settings_load_from_environment(self):
        with patch.dict("os.environ", {"HOOK_ENABLED": "true", "HOOK_HOST": "::1",
                                     "HOOK_PORT": "9000", "HOOK_TOKEN": "secret",
                                     "HOOK_TEXT": "{task} done"}):
            config = Config()
        self.assertTrue(config.hook_enabled)
        self.assertEqual(config.hook_host, "::1")
        self.assertEqual(config.hook_port, 9000)
        self.assertEqual(config.hook_token, "secret")
        self.assertEqual(config.hook_text, "{task} done")


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.speaker = SimpleNamespace(announce_notification=AsyncMock(return_value=True))
        await self.create_service()

    async def create_service(self, **limits):
        if hasattr(self, "client"):
            await self.client.close()
            await self.service.close()
        self.service = NotificationService(hook_config(), self.speaker, **limits)
        self.client = TestClient(TestServer(self.service.app))
        await self.client.start_server()
        self.service._accepting = True
        self.service._worker_task = asyncio.create_task(self.service._worker())

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def post(self, body=None, **kwargs):
        return await self.client.post(
            "/hooks/task-completed", json=body or {"source": "local", "event_id": "one"},
            headers={"Authorization": "Bearer test-secret"}, **kwargs
        )

    async def joined(self):
        await asyncio.wait_for(self.service.queue.join(), timeout=2)

    async def test_authentication_uses_constant_time_token_comparison(self):
        for authorization in [None, "", "Basic test-secret", "Bearer wrong",
                              "Bearer test-secret extra", "Bearer 错误"]:
            with self.subTest(authorization=authorization):
                headers = {} if authorization is None else {"Authorization": authorization}
                with patch("core.notifications.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
                    response = await self.client.post("/hooks/task-completed", json={
                        "source": "local", "event_id": "one"}, headers=headers)
                    self.assertEqual(response.status, 401)
                    compare.assert_called_once()
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.speaker.announce_notification.assert_not_awaited()
        self.assertFalse(self.service._dedup)

    async def test_duplicate_pending_and_completed_and_source_namespace(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def speak(text):
            entered.set()
            await release.wait()
            return True

        self.speaker.announce_notification.side_effect = speak
        self.assertEqual((await self.post()).status, 202)
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual((await self.post()).status, 200)
        self.assertEqual(self.service._dedup[("local", "one")], None)
        release.set()
        await self.joined()
        self.assertEqual((await self.post()).status, 200)
        self.assertEqual((await self.post({"source": "oracle", "event_id": "one"})).status, 202)
        await self.joined()
        self.assertEqual(self.speaker.announce_notification.await_count, 2)
        self.speaker.announce_notification.assert_any_await("local的任务已完成")
        self.speaker.announce_notification.assert_any_await("oracle的任务已完成")

    async def test_simultaneous_retries_enqueue_once(self):
        responses = await asyncio.gather(*(self.post() for _ in range(12)))
        self.assertEqual([r.status for r in responses].count(202), 1)
        self.assertEqual([r.status for r in responses].count(200), 11)
        await self.joined()
        self.speaker.announce_notification.assert_awaited_once()

    async def test_queue_full_does_not_reserve_event_and_worker_is_serial(self):
        await self.create_service(queue_size=1)
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        maximum = 0

        async def speak(text):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return True

        self.speaker.announce_notification.side_effect = speak
        self.assertEqual((await self.post()).status, 202)
        await asyncio.wait_for(entered.wait(), 2)
        second = {"source": "local", "event_id": "two"}
        third = {"source": "local", "event_id": "three"}
        self.assertEqual((await self.post(second)).status, 202)
        self.assertEqual((await self.post(third)).status, 503)
        self.assertNotIn(("local", "three"), self.service._dedup)
        self.assertEqual((await self.post(second)).status, 200)
        release.set()
        await self.joined()
        self.assertEqual((await self.post(third)).status, 202)
        await self.joined()
        self.assertEqual(maximum, 1)
        self.assertEqual(self.speaker.announce_notification.await_count, 3)

    async def test_dedup_is_bounded_and_completed_entries_expire(self):
        await self.create_service(dedup_size=1, dedup_ttl=10)
        with patch("core.notifications.time") as clock:
            clock.monotonic.return_value = 100
            self.assertEqual((await self.post()).status, 202)
            await self.joined()
            clock.monotonic.return_value = 109
            self.assertEqual((await self.post()).status, 200)
            second = {"source": "local", "event_id": "two"}
            self.assertEqual((await self.post(second)).status, 503)
            self.assertNotIn(("local", "two"), self.service._dedup)
            clock.monotonic.return_value = 110
            self.assertEqual((await self.post(second)).status, 202)
            await self.joined()
            self.assertEqual(len(self.service._dedup), 1)

    async def test_pending_entries_do_not_expire_during_speech(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def speak(text):
            entered.set()
            await release.wait()
            return True

        self.speaker.announce_notification.side_effect = speak
        with patch("core.notifications.time") as clock:
            clock.monotonic.return_value = 100
            self.assertEqual((await self.post()).status, 202)
            await asyncio.wait_for(entered.wait(), 2)
            clock.monotonic.return_value = 100000
            self.assertEqual((await self.post()).status, 200)
            release.set()
            await self.joined()
        self.speaker.announce_notification.assert_awaited_once()

    async def test_speech_failure_and_boolean_rejection_allow_retry(self):
        for outcome in [RuntimeError("offline"), False, None]:
            with self.subTest(outcome=outcome):
                self.speaker.announce_notification.side_effect = [outcome, True]
                with self.assertLogs("core.notifications", level="ERROR"):
                    self.assertEqual((await self.post()).status, 202)
                    await self.joined()
                self.assertNotIn(("local", "one"), self.service._dedup)
                self.assertEqual((await self.post()).status, 202)
                await self.joined()
                self.assertEqual((await self.post()).status, 200)
                self.service._dedup.clear()

    async def test_timeout_releases_dedup_and_worker_continues(self):
        await self.create_service(worker_timeout=0.02)
        cancelled = asyncio.Event()

        async def blocked(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.speaker.announce_notification.side_effect = blocked
        with self.assertLogs("core.notifications", level="ERROR"):
            self.assertEqual((await self.post()).status, 202)
            await self.joined()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.service._dedup)
        self.speaker.announce_notification.side_effect = None
        self.assertEqual((await self.post()).status, 202)
        await self.joined()
        self.assertEqual((await self.post()).status, 200)

    async def test_custom_text_and_template(self):
        self.service.config.hook_text = "{source}: {task} ({event_id})"
        body = dict(source="piweb", event_id="build", task="tests")
        self.assertEqual((await self.post(body)).status, 202)
        await self.joined()
        self.speaker.announce_notification.assert_awaited_with("piweb: tests (build)")
        self.assertEqual((await self.post(dict(body, event_id="other", text="自定义通知"))).status, 202)
        await self.joined()
        self.speaker.announce_notification.assert_awaited_with("自定义通知")

    async def test_invalid_json_fields_types_and_lengths(self):
        headers = {"Authorization": "Bearer test-secret"}
        invalid = [None, [], "text", 3, True, {}, {"source": "local"},
                   {"event_id": "one"}, {"source": "local", "event_id": "one", "extra": 1}]
        base = {"source": "local", "event_id": "one"}
        for name, limit in [("source", 80), ("event_id", 200), ("task", 120), ("text", 300)]:
            for value in [None, True, 1, [], {}, "x" * (limit + 1)]:
                invalid.append(dict(base, **{name: value}))
            if name != "task":
                invalid.extend([dict(base, **{name: ""}), dict(base, **{name: "  "})])
        for body in invalid:
            with self.subTest(body=body):
                response = await self.client.post("/hooks/task-completed",
                                                  data=json.dumps(body), headers=headers,
                                                  skip_auto_headers={"Content-Type"})
                self.assertEqual(response.status, 400)
                response = await self.client.post("/hooks/task-completed", data=json.dumps(body),
                                                  headers=dict(headers, **{"Content-Type": "application/json"}))
                self.assertEqual(response.status, 400)
        response = await self.client.post("/hooks/task-completed", data="{broken",
                                          headers=dict(headers, **{"Content-Type": "application/json"}))
        self.assertEqual(response.status, 400)
        self.speaker.announce_notification.assert_not_awaited()
        self.assertFalse(self.service._dedup)

    async def test_input_boundaries_body_limit_and_rendered_limit(self):
        body = dict(source="源" * 80, event_id="事" * 200, task="任" * 120, text="文" * 300)
        self.assertEqual((await self.post(body)).status, 202)
        await self.joined()
        self.speaker.announce_notification.assert_awaited_with("文" * 300)
        response = await self.client.post("/hooks/task-completed", data=" " * 20000,
                                          headers={"Authorization": "Bearer test-secret",
                                                   "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.service.config.hook_text = "{source}{event_id}{task}"
        body.pop("text")
        body["event_id"] = "other" * 40
        self.assertEqual((await self.post(body)).status, 400)
        self.assertEqual((await self.post(dict(source="local", event_id="empty-task", task=""))).status, 202)

    async def test_health_is_local_and_contains_no_secrets(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})
        for remote in ["203.0.113.1", None]:
            response = await self.service._health(SimpleNamespace(remote=remote))
            self.assertEqual(response.status, 403)
        response = await self.service._health(SimpleNamespace(remote="::1"))
        self.assertEqual(response.status, 200)

    async def test_close_cancels_worker_discards_queue_and_dedup(self):
        entered = asyncio.Event()

        async def blocked(text):
            entered.set()
            await asyncio.Event().wait()

        self.speaker.announce_notification.side_effect = blocked
        await self.post()
        await asyncio.wait_for(entered.wait(), 2)
        await self.post(dict(source="local", event_id="two"))
        await self.service.close()
        await self.joined()
        self.assertFalse(self.service._dedup)
        self.assertIsNone(self.service._worker_task)
        self.assertEqual((await self.post()).status, 503)

    async def test_listener_start_serves_requests_and_close_releases_resources(self):
        service = NotificationService(hook_config(hook_port=unused_port()), self.speaker)
        try:
            await service.start()
            url = f"http://127.0.0.1:{service.config.hook_port}"
            async with ClientSession() as client:
                async with client.get(url + "/health") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.json(), {"status": "ok"})
                async with client.post(url + "/hooks/task-completed", json={
                    "source": "piweb", "event_id": "actual-listener"},
                    headers={"Authorization": "Bearer test-secret"}) as response:
                    self.assertEqual(response.status, 202)
                await asyncio.wait_for(service.queue.join(), 2)
        finally:
            await service.close()
        self.assertIsNone(service._runner)
        self.assertIsNone(service._worker_task)
        self.assertFalse(service._dedup)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", service.config.hook_port))

    async def test_bind_failure_is_explicit_and_leaves_no_worker(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            service = NotificationService(hook_config(hook_port=occupied.getsockname()[1]), self.speaker)
            with self.assertRaisesRegex(RuntimeError, "Cannot bind notification webhook"):
                await service.start()
            self.assertIsNone(service._runner)
            self.assertIsNone(service._worker_task)
            self.assertFalse(service._accepting)
            await service.close()


class SpeakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.speaker = XiaoAiService("user", "pass")
        self.speaker._device_id = "speaker"
        self.speaker._miot_did = "miot"
        self.speaker._mina = Mock(text_to_speech=AsyncMock(return_value=True),
                                  player_pause=AsyncMock(return_value=True))
        self.speaker._miio = Mock(miot_set_prop=AsyncMock(return_value=0),
                                  miot_get_props=AsyncMock(return_value=[20, False]),
                                  miot_get_prop=AsyncMock(return_value=False),
                                  miot_action=AsyncMock(return_value=0))

    async def test_notification_bypasses_guard_restores_it_and_regular_tts_stays_skipped(self):
        self.speaker._media_guard_active = True
        await self.speaker.send_tts("regular")
        self.speaker._mina.text_to_speech.assert_not_awaited()
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()) as delay:
            self.assertTrue(await self.speaker.announce_notification("notification"))
        self.speaker._mina.text_to_speech.assert_awaited_once_with("speaker", "notification")
        self.assertEqual([call.args[-1] for call in self.speaker._miio.miot_set_prop.await_args_list],
                         [False, True])
        self.assertTrue(self.speaker._media_guard_active)
        self.assertEqual(self.speaker._mina.player_pause.await_count, 5)
        delay.assert_any_await(0.8)
        delay.assert_any_await(14.0)
        await self.speaker.send_tts("still regular")
        self.assertEqual(self.speaker._mina.text_to_speech.await_count, 1)

    async def test_notification_failure_is_not_swallowed_and_guard_is_restored(self):
        self.speaker._media_guard_active = True
        for outcome in [False, RuntimeError("tts offline")]:
            with self.subTest(outcome=outcome):
                self.speaker._mina.text_to_speech.side_effect = [outcome]
                with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
                    with self.assertRaises(RuntimeError):
                        await self.speaker.announce_notification("notification")
                self.assertEqual(self.speaker._miio.miot_set_prop.await_args.args[-1], True)
                self.assertTrue(self.speaker._media_guard_active)

    async def test_unmute_and_pause_rejections_fail_notification(self):
        self.speaker._media_guard_active = True
        self.speaker._miio.miot_set_prop.side_effect = [1, 0]
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "unmute"):
                await self.speaker.announce_notification("notification")
        self.speaker._mina.text_to_speech.assert_not_awaited()
        self.speaker._miio.miot_set_prop.side_effect = None
        self.speaker._mina.player_pause.return_value = False
        with self.assertRaisesRegex(RuntimeError, "pause"):
            await self.speaker.announce_notification("notification")
        self.assertEqual(self.speaker._miio.miot_set_prop.await_args.args[-1], True)

    async def test_uninitialized_notification_fails_and_regular_errors_remain_swallowed(self):
        self.speaker._mina.text_to_speech.side_effect = RuntimeError("offline")
        with self.assertLogs("core.xiaoai", level="WARNING"):
            await self.speaker.send_tts("regular")
        self.speaker._mina = None
        with self.assertRaisesRegex(RuntimeError, "not initialized"):
            await self.speaker.announce_notification("notification")

    async def test_pause_operations_wait_and_cancellation_restores_guard(self):
        self.speaker._media_guard_active = True
        entered = asyncio.Event()

        async def blocked(device, text):
            entered.set()
            await asyncio.Event().wait()

        self.speaker._mina.text_to_speech.side_effect = blocked
        task = asyncio.create_task(self.speaker.announce_notification("notification"))
        await asyncio.wait_for(entered.wait(), 2)
        pause = asyncio.create_task(self.speaker.stop_speaker_player())
        await asyncio.sleep(0)
        self.assertFalse(pause.done())
        self.assertEqual(self.speaker._mina.player_pause.await_count, 1)
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(pause, 2)
        self.assertTrue(self.speaker._media_guard_active)
        self.assertEqual(self.speaker._miio.miot_set_prop.await_args.args[-1], True)
        self.assertFalse(self.speaker._speaker_lock.locked())

    async def test_cancellation_is_preserved_when_pause_cleanup_fails(self):
        self.speaker._media_guard_active = True
        entered = asyncio.Event()

        async def blocked(device, text):
            entered.set()
            await asyncio.Event().wait()

        self.speaker._mina.text_to_speech.side_effect = blocked
        task = asyncio.create_task(self.speaker.announce_notification("notification"))
        await asyncio.wait_for(entered.wait(), 2)
        self.speaker._mina.player_pause.return_value = False
        with self.assertLogs("core.xiaoai", level="ERROR"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.speaker._miio.miot_set_prop.await_args.args[-1], True)
        self.assertFalse(self.speaker._speaker_lock.locked())

    async def test_unguarded_notification_still_pauses_after_speech(self):
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(await self.speaker.announce_notification("notification"))
        self.speaker._miio.miot_set_prop.assert_awaited_once_with("miot", (2, 2), False)
        self.assertEqual(self.speaker._mina.player_pause.await_count, 5)

    async def test_l05c_speaks_with_hardware_muted_after_restart_without_resuming_music(self):
        self.speaker._hardware = "L05C"
        self.speaker._miio.miot_get_props.return_value = [20, True]
        events = []
        self.speaker._mina.player_pause.side_effect = lambda *_: events.append("pause") or True
        self.speaker._miio.miot_action.side_effect = lambda *_: events.append("tts") or 0

        async def sleep(seconds):
            events.append(("wait", seconds))

        with patch("core.xiaoai.asyncio.sleep", side_effect=sleep):
            self.assertTrue(await self.speaker.announce_notification("已收到"))
        self.speaker._miio.miot_action.assert_awaited_once_with("miot", (5, 3), ["已收到"])
        self.speaker._mina.text_to_speech.assert_not_awaited()
        self.speaker._miio.miot_set_prop.assert_awaited_once_with("miot", (2, 2), True)
        self.assertFalse(self.speaker._media_guard_active)
        self.assertEqual(events[:4], ["pause", "tts", ("wait", 12.0), "pause"])

    async def test_l05c_notification_during_spotify_keeps_guard_and_regular_tts_skipped(self):
        self.speaker._hardware = "L05C"
        self.speaker._media_guard_active = True
        self.speaker._miio.miot_get_props.return_value = [20, True]
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            await self.speaker.announce_notification("本机 PiWeb的任务已完成")
        await self.speaker.send_tts("regular")
        self.speaker._miio.miot_action.assert_awaited_once()
        self.speaker._mina.text_to_speech.assert_not_awaited()
        self.speaker._miio.miot_set_prop.assert_awaited_once_with("miot", (2, 2), True)
        self.assertTrue(self.speaker._media_guard_active)

    async def test_l05c_regular_tts_uses_play_text_and_preserves_spaces(self):
        self.speaker._hardware = "L05C"
        await self.speaker.send_tts("本机 PiWeb 已收到")
        self.speaker._miio.miot_action.assert_awaited_once_with("miot", (5, 3), ["本机 PiWeb 已收到"])
        self.speaker._mina.text_to_speech.assert_not_awaited()

    async def test_l05c_action_rejection_does_not_fall_back_to_silent_mina(self):
        self.speaker._hardware = "L05C"
        self.speaker._media_guard_active = True
        self.speaker._miio.miot_action.return_value = -1
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "MIoT Play Text"):
                await self.speaker.announce_notification("已收到")
        self.speaker._mina.text_to_speech.assert_not_awaited()
        self.speaker._miio.miot_set_prop.assert_awaited_once_with("miot", (2, 2), True)

    async def test_l05c_zero_volume_is_raised_and_long_speech_is_not_truncated(self):
        self.speaker._hardware = "L05C"
        self.speaker._miio.miot_get_props.return_value = [0, True]
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()) as delay:
            await self.speaker.announce_notification("文" * 300)
        self.speaker._miio.miot_set_prop.assert_any_await("miot", (2, 1), 20)
        delay.assert_any_await(110.0)
        self.assertGreater(NotificationService(hook_config(), self.speaker)._worker_timeout, 140)

    async def test_generic_mute_after_restart_is_released_and_restored(self):
        self.speaker._miio.miot_get_props.return_value = [20, True]
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            await self.speaker.announce_notification("已收到")
        self.assertEqual([call.args[-1] for call in self.speaker._miio.miot_set_prop.await_args_list],
                         [False, True])
        self.assertFalse(self.speaker._media_guard_active)

    async def test_unmute_must_take_effect_before_generic_tts(self):
        self.speaker._miio.miot_get_prop.return_value = True
        with patch("core.xiaoai.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "did not take effect"):
                await self.speaker.announce_notification("已收到")
        self.speaker._mina.text_to_speech.assert_not_awaited()



class MainLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_initialized_speaker_and_cleanup_order(self):
        config = hook_config(spotify_client_id="", spotify_client_secret="", jev_api_key="")
        events = []
        speaker = Mock(initialize=AsyncMock(side_effect=lambda: events.append("initialize")),
                       close=AsyncMock(side_effect=lambda: events.append("speaker-close")))
        service = Mock(start=AsyncMock(side_effect=lambda: events.append("hook-start")),
                       close=AsyncMock(side_effect=lambda: events.append("hook-close")))
        core = Mock(poll_forever=AsyncMock(side_effect=asyncio.CancelledError))
        with patch("main.XiaoAiService", return_value=speaker), \
                patch("main.NotificationService", return_value=service) as factory, \
                patch("main.AssistantCore", return_value=core):
            with self.assertRaises(asyncio.CancelledError):
                await main_loop(config)
        factory.assert_called_once_with(config, speaker)
        self.assertEqual(events, ["initialize", "hook-start", "hook-close", "speaker-close"])

    async def test_hook_start_failure_closes_speaker_and_propagates(self):
        config = hook_config(spotify_client_id="", spotify_client_secret="", jev_api_key="")
        speaker = Mock(initialize=AsyncMock(), close=AsyncMock())
        service = Mock(start=AsyncMock(side_effect=RuntimeError("Cannot bind")), close=AsyncMock())
        with patch("main.XiaoAiService", return_value=speaker), \
                patch("main.NotificationService", return_value=service):
            with self.assertRaisesRegex(RuntimeError, "Cannot bind"):
                await main_loop(config)
        service.close.assert_awaited_once()
        speaker.close.assert_awaited_once()

    async def test_disabled_hook_does_not_create_listener(self):
        config = hook_config(hook_enabled=False, hook_token="", spotify_client_id="",
                             spotify_client_secret="", jev_api_key="")
        speaker = Mock(initialize=AsyncMock(), close=AsyncMock())
        core = Mock(poll_forever=AsyncMock(side_effect=asyncio.CancelledError))
        with patch("main.XiaoAiService", return_value=speaker), \
                patch("main.NotificationService") as factory, \
                patch("main.AssistantCore", return_value=core):
            with self.assertRaises(asyncio.CancelledError):
                await main_loop(config)
        factory.assert_not_called()
        speaker.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
