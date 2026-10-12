"""播放设备隔离与双端切歌回归测试，不操作真实设备。"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

import spotipy

from modules.spotify.parser import parse
from main import _run_spotify, handle_command
from modules.spotify.service import SpotifyService


def device(name, kind="Computer", **extra):
    return dict(id=name, name=name, type=kind, is_active=True, **extra)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.service = SpotifyService.__new__(SpotifyService)
        self.service._device_name = "local-pc"
        self.service._device_id = "old-device"
        self.service._sp = Mock()

    def test_plain_playlist_command_defaults_to_shuffle(self):
        spotify = Mock()
        spotify.find_playlist.return_value = {"id": "acg-id"}
        spotify.play_playlist.return_value = "正在随机播放歌单: acg"
        command = parse("播放我的ACG歌单")

        result = _run_spotify(command.intent, command, spotify)

        spotify.play_playlist.assert_called_once_with("acg-id", shuffle=True)
        self.assertEqual(result, "正在随机播放歌单: acg")

    def test_playlist_aliases_match_actual_playlist_names(self):
        names = [
            "rock", "rap", "Electronic", "j pop", "Blues", "Jazz", "pop",
            "paino", "acg", "华语", "民谣", "My top tracks playlist",
        ]
        self.service.get_user_playlists = Mock(
            return_value=[{"id": name, "name": name} for name in names]
        )
        aliases = {
            "摇滚": "rock",
            "摇滚音乐": "rock",
            "说唱": "rap",
            "嘻哈音乐": "rap",
            "电音": "Electronic",
            "电子音乐": "Electronic",
            "日语": "j pop",
            "J-POP": "j pop",
            "蓝调": "Blues",
            "布鲁斯": "Blues",
            "爵士乐": "Jazz",
            "流行音乐": "pop",
            "钢琴曲": "paino",
            "piano": "paino",
            "动漫": "acg",
            "二次元音乐": "acg",
            "中文": "华语",
            "国语": "华语",
            "folk": "民谣",
            "最常听": "My top tracks playlist",
        }
        for spoken, expected in aliases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(self.service.find_playlist(spoken)["name"], expected)

    def test_shuffle_playlist_starts_at_random_offset_without_skipping(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {
            "name": "acg",
            "tracks": {"total": 160},
        }
        self.service._sp.current_playback.side_effect = [
            {"device": {"id": "local-pc"}, "context": None,
             "actions": {"disallows": {"toggling_shuffle": True}}},
            {"device": {"id": "local-pc"},
             "context": {"uri": "spotify:playlist:acg-id"},
             "shuffle_state": False, "actions": {"disallows": {}}},
        ]

        with patch("modules.spotify.service.random.randrange", return_value=73):
            with patch("modules.spotify.service.time.sleep"):
                result = self.service.play_playlist("acg-id", shuffle=True)

        self.service._sp.shuffle.assert_called_once_with(
            True, device_id="local-pc"
        )
        self.service._sp.start_playback.assert_called_once_with(
            device_id="local-pc",
            context_uri="spotify:playlist:acg-id",
            offset={"position": 73},
        )
        self.service._sp.next_track.assert_not_called()
        calls = [call[0] for call in self.service._sp.method_calls]
        self.assertLess(calls.index("start_playback"), calls.index("shuffle"))
        self.assertEqual(result, "正在随机播放歌单: acg")

    def test_disallowed_shuffle_does_not_prevent_playlist_playback(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {"name": "acg", "tracks": {"total": 1}}
        self.service._sp.current_playback.return_value = {
            "device": {"id": "local-pc"},
            "context": {"uri": "spotify:playlist:acg-id"},
            "shuffle_state": False,
            "actions": {"disallows": {"toggling_shuffle": True}},
        }

        result = self.service.play_playlist("acg-id", shuffle=True)

        self.service._sp.start_playback.assert_called_once()
        self.service._sp.shuffle.assert_not_called()
        self.assertIn("正在播放歌单: acg", result)
        self.assertIn("禁止切换随机模式", result)

    def test_unconfirmed_playlist_context_does_not_toggle_shuffle(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {"name": "acg", "tracks": {"total": 1}}
        self.service._sp.current_playback.return_value = {
            "device": {"id": "local-pc"}, "context": None,
            "actions": {"disallows": {"toggling_shuffle": True}},
        }

        with patch("modules.spotify.service.time.sleep") as sleep:
            result = self.service.play_playlist("acg-id", shuffle=True)

        self.service._sp.start_playback.assert_called_once()
        self.service._sp.shuffle.assert_not_called()
        self.assertEqual(self.service._sp.current_playback.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertIn("暂未确认播放状态", result)

    def test_already_shuffled_playlist_needs_no_toggle(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {"name": "acg", "tracks": {"total": 1}}
        self.service._sp.current_playback.return_value = {
            "device": {"id": "local-pc"},
            "context": {"uri": "spotify:playlist:acg-id"},
            "shuffle_state": True,
            "actions": {"disallows": {"toggling_shuffle": True}},
        }

        result = self.service.play_playlist("acg-id", shuffle=True)

        self.service._sp.shuffle.assert_not_called()
        self.assertEqual(result, "正在随机播放歌单: acg")

    def test_shuffle_restriction_after_context_is_reported(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {"name": "acg", "tracks": {"total": 1}}
        self.service._sp.current_playback.return_value = {
            "device": {"id": "local-pc"},
            "context": {"uri": "spotify:playlist:acg-id"},
            "shuffle_state": False,
            "actions": {"disallows": {}},
        }
        self.service._sp.shuffle.side_effect = spotipy.SpotifyException(
            403, -1, "Player command failed: Restriction violated"
        )

        result = self.service.play_playlist("acg-id", shuffle=True)

        self.assertIn("禁止切换随机模式", result)

    def test_other_shuffle_403_is_not_hidden(self):
        self.service._device_id = "local-pc"
        self.service._ensure_device = Mock()
        self.service._sp.playlist.return_value = {"name": "acg", "tracks": {"total": 1}}
        self.service._sp.current_playback.return_value = {
            "device": {"id": "local-pc"},
            "context": {"uri": "spotify:playlist:acg-id"},
            "shuffle_state": False,
            "actions": {"disallows": {}},
        }
        self.service._sp.shuffle.side_effect = spotipy.SpotifyException(
            403, -1, "Insufficient client scope"
        )

        with self.assertRaises(spotipy.SpotifyException):
            self.service.play_playlist("acg-id", shuffle=True)

    def test_only_selects_named_computer(self):
        self.service._sp.devices.return_value = {"devices": [
            device("speaker", "Speaker"), device("other-pc"), device("local-pc")
        ]}
        self.service.play()
        self.service._sp.start_playback.assert_called_once_with(device_id="local-pc")

    def test_missing_or_ambiguous_computer_never_falls_back(self):
        for devices in [[], [device("speaker", "Speaker")], [device("other-pc")],
                        [device("local-pc", is_restricted=True)],
                        [device("local-pc"), device("local-pc")]]:
            with self.subTest(devices=devices):
                self.service._sp.devices.return_value = {"devices": devices}
                with self.assertRaises(RuntimeError):
                    self.service._ensure_device(auto_launch=False)
                self.assertIsNone(self.service._device_id)
                self.service._sp.transfer_playback.assert_not_called()

    def test_failed_refresh_does_not_pause_cached_device(self):
        self.service._sp.devices.side_effect = RuntimeError("offline")
        self.service.pause()
        self.assertIsNone(self.service._device_id)
        self.service._sp.pause_playback.assert_not_called()

    def test_speaker_online_does_not_prevent_launching_computer(self):
        self.service._sp.devices.side_effect = [
            {"devices": [device("speaker", "Speaker")]},
            {"devices": [device("local-pc")]},
        ]
        with patch.object(self.service, "_launch_spotify_client", return_value=True) as launch:
            with patch("modules.spotify.service.time.sleep"):
                self.service.play()
        launch.assert_called_once()
        self.service._sp.start_playback.assert_called_once_with(device_id="local-pc")


class SkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_suppresses_speaker_before_confirmation(self):
        for phrase, method in [("播放下一首", "next_track"), ("播放上一首", "previous_track")]:
            with self.subTest(phrase=phrase):
                spotify = Mock()
                speaker = Mock()
                events = []

                async def suppress():
                    events.append("suppressed")

                async def tts(text):
                    self.assertEqual(events, ["suppressed"])

                speaker.suppress_speaker_player = AsyncMock(side_effect=suppress)
                speaker.enable_spotify_guard = AsyncMock()
                speaker.disable_spotify_guard = AsyncMock()
                speaker.send_tts = AsyncMock(side_effect=tts)
                command = parse(phrase)
                await handle_command(command.intent, command, spotify, speaker)
                getattr(spotify, method).assert_called_once()
                spotify.search_and_play.assert_not_called()
                speaker.suppress_speaker_player.assert_awaited_once()
                speaker.enable_spotify_guard.assert_awaited_once()
                speaker.send_tts.assert_not_awaited()

    async def test_pause_restores_speaker_after_suppression(self):
        spotify = Mock()
        speaker = Mock()
        speaker.enable_spotify_guard = AsyncMock()
        speaker.disable_spotify_guard = AsyncMock()
        speaker.suppress_speaker_player = AsyncMock()
        speaker.send_tts = AsyncMock()
        command = parse("暂停")

        await handle_command(command.intent, command, spotify, speaker)

        spotify.pause.assert_called_once()
        speaker.enable_spotify_guard.assert_not_awaited()
        speaker.suppress_speaker_player.assert_awaited_once()
        speaker.disable_spotify_guard.assert_awaited_once()
        speaker.send_tts.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
