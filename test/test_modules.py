"""模块路由、歌单语义选择与 Windows 软件启动的离线测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core.assistant import AssistantCore
from core.jev import Decision, choose_catalog
from modules.spotify.module import SpotifyModule
from modules.windows.module import InstalledApp, WindowsAppModule, _safe_is_dir


class FakeRouter:
    def __init__(self, *choices):
        self.choices = list(choices)
        self.calls = []

    async def choose(self, query, choices, question, **kwargs):
        self.calls.append((query, choices))
        return self.choices.pop(0)


class ModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.speaker = Mock()
        self.speaker.enable_media_guard = AsyncMock()
        self.speaker.disable_media_guard = AsyncMock()
        self.speaker.suppress_speaker_player = AsyncMock()
        self.speaker.send_tts = AsyncMock()

    async def test_jev_selects_real_playlist_and_always_shuffles(self):
        service = Mock()
        service.get_user_playlists.return_value = [
            {"id": "work-id", "name": "工作专注"},
            {"id": "rock-id", "name": "rock"},
        ]
        service.play_playlist.return_value = "正在随机播放歌单: rock"
        router = FakeRouter(Decision("spotify_playlist"), Decision("playlist_1"))
        core = AssistantCore(self.speaker, [SpotifyModule(service)], router=router)

        # 原规则没有“歌单”关键词，Jev 仍可从实时歌单中选择。
        await core.handle_query("放点摇滚")

        service.play_playlist.assert_called_once_with("rock-id", shuffle=True)
        service.search_and_play.assert_not_called()
        self.speaker.enable_media_guard.assert_awaited_once()
        self.speaker.send_tts.assert_not_awaited()

    async def test_jev_failure_falls_back_to_playlist_rule(self):
        service = Mock()
        service.find_playlist.return_value = {"id": "work-id"}
        service.play_playlist.return_value = "正在随机播放歌单: 工作"
        router = FakeRouter(Decision(fallback=True))
        core = AssistantCore(self.speaker, [SpotifyModule(service)], router=router)

        await core.handle_query("播放我的工作歌单")

        service.find_playlist.assert_called_once_with("工作")
        service.play_playlist.assert_called_once_with("work-id", shuffle=True)

    async def test_explicit_none_does_not_trigger_regex_fallback(self):
        service = Mock()
        core = AssistantCore(self.speaker, [SpotifyModule(service)],
                             router=FakeRouter(Decision("none")))
        await core.handle_query("播放我的工作歌单")
        service.play_playlist.assert_not_called()

    async def test_windows_app_is_launched_only_from_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "Example.exe"
            executable.touch()
            catalog = Mock()
            catalog.list_apps.return_value = [InstalledApp("示例软件", executable)]
            module = WindowsAppModule(catalog)
            router = FakeRouter(Decision("windows"), Decision("app_0"))
            core = AssistantCore(self.speaker, [module], router=router)
            with patch("modules.windows.module.subprocess.Popen") as launch:
                await core.handle_query("打开示例软件")
            launch.assert_called_once_with([str(executable)], cwd=str(executable.parent))
            self.speaker.send_tts.assert_awaited_once_with("已启动 示例软件")

    async def test_windows_exact_name_works_without_jev(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "Example.exe"
            executable.touch()
            catalog = Mock()
            catalog.list_apps.return_value = [InstalledApp("示例软件", executable)]
            core = AssistantCore(self.speaker, [WindowsAppModule(catalog)])
            with patch("modules.windows.module.subprocess.Popen") as launch:
                await core.handle_query("请帮我打开示例软件")
            launch.assert_called_once()

    async def test_catalog_selection_includes_later_batches(self):
        entries = {f"app_{i}": f"软件 {i}" for i in range(300)}
        router = FakeRouter(Decision("none"), Decision("app_299"))
        result = await choose_catalog(
            router, "打开软件 299", entries,
            {"none": "没有请求该批次的软件"}, "选择软件",
        )
        self.assertEqual(result.choice, "app_299")
        self.assertEqual(len(router.calls), 2)

    async def test_catalog_conflict_falls_back(self):
        entries = {f"app_{i}": f"软件 {i}" for i in range(300)}
        router = FakeRouter(Decision("app_0"), Decision("app_299"))
        result = await choose_catalog(
            router, "打开软件", entries,
            {"none": "没有请求该批次的软件"}, "选择软件",
        )
        self.assertTrue(result.fallback)

    async def test_broken_windows_mount_point_is_skipped(self):
        with patch("modules.windows.module.Path.is_dir", side_effect=OSError(649, "bad mount")):
            self.assertFalse(_safe_is_dir(Path("C:/broken/Cursor")))


if __name__ == "__main__":
    unittest.main()
