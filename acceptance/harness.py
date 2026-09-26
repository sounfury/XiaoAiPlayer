"""验收测试驱动：操纵真实的小爱音箱、Spotify 和播放助手服务进程。

只通过外部可观察的行为交互 —— 对小爱“说话”（执行文本指令）、
读取 Spotify 播放状态、读取音箱播放状态、读取服务日志。
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import spotipy
from miservice import MiAccount, MiIOService, MiNAService
from spotipy.oauth2 import SpotifyOAuth

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "xiaoai_player.log"

# 小爱音箱 MIoT 规格：智能音箱服务(siid=5) 的“执行文本指令”动作(aiid=4)，等同于对音箱说话
_EXECUTE_TEXT_DIRECTIVE = [5, 4]


def wait_until(check, timeout: float, interval: float = 1.0, desc: str = ""):
    """轮询直到 check() 返回真值，超时则断言失败"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待超时({timeout}s): {desc}，最后一次结果: {last!r}")


class Speaker:
    """小爱音箱"""

    def __init__(self, mi_user: str, mi_pass: str, mi_did: str = ""):
        self._loop = asyncio.new_event_loop()
        self._run(self._init(mi_user, mi_pass, mi_did))

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _init(self, mi_user, mi_pass, mi_did):
        self._session = aiohttp.ClientSession()
        account = MiAccount(self._session, mi_user, mi_pass)
        self._mina = MiNAService(account)
        self._miio = MiIOService(account)
        devices = await self._mina.device_list()
        device = next((d for d in devices if d.get("deviceID") == mi_did), devices[0])
        self._device_id = device["deviceID"]
        self._miot_did = device["miotDID"]

    def say(self, text: str):
        """模拟用户对小爱说一句话"""
        code = self._run(
            self._miio.miot_action(self._miot_did, _EXECUTE_TEXT_DIRECTIVE, [text, False])
        )
        assert code == 0, f"向小爱发送文本指令失败: {code}"

    def is_playing(self) -> bool:
        r = self._run(
            self._mina._ubus_request(
                self._device_id, "player_get_play_status", "mediaplayer", {"media": "app_ios"}
            )
        )
        return json.loads(r["data"]["info"]).get("status") == 1

    def pause(self):
        self._run(self._mina.player_pause(self._device_id))

    def close(self):
        self._run(self._session.close())
        self._loop.close()


class Spotify:
    """电脑上的 Spotify（通过 Web API 观察状态，通过进程管理客户端）"""

    SCOPES = " ".join(
        [
            "user-modify-playback-state",
            "user-read-playback-state",
            "user-read-currently-playing",
            "playlist-read-private",
            "playlist-read-collaborative",
        ]
    )

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.api = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope=self.SCOPES,
                cache_path=str(ROOT / ".spotipy_cache"),
            )
        )

    # ─── 客户端进程 ───

    @staticmethod
    def process_running() -> bool:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/NH"],
            capture_output=True, text=True, errors="replace",
        ).stdout
        return "Spotify.exe" in out

    def device_online(self) -> bool:
        return bool(self.api.devices().get("devices"))

    def open(self):
        if not self.device_online():
            import os
            os.startfile("spotify:")
            wait_until(self.device_online, 60, desc="Spotify 设备上线")

    def quit(self):
        subprocess.run(["taskkill", "/F", "/IM", "Spotify.exe"], capture_output=True)
        wait_until(lambda: not self.process_running(), 15, desc="Spotify 进程退出")
        wait_until(lambda: not self.device_online(), 60, desc="Spotify 设备从云端下线")

    # ─── 播放状态 ───

    def playback(self) -> dict:
        return self.api.current_playback() or {}

    def is_playing(self) -> bool:
        return bool(self.playback().get("is_playing"))

    def current_track(self) -> dict:
        return self.playback().get("item") or {}

    def device_id(self) -> str:
        devices = self.api.devices().get("devices", [])
        computer = next((d for d in devices if d.get("type") == "Computer"), devices[0])
        return computer["id"]

    def find_playlist(self, name: str) -> dict:
        offset = 0
        while True:
            batch = self.api.current_user_playlists(limit=50, offset=offset)
            for p in batch.get("items", []):
                if p["name"] == name:
                    return p
            if not batch.get("next"):
                raise AssertionError(f"测试前提不满足：Spotify 中没有歌单「{name}」")
            offset += 50

    def my_playlist_uris(self) -> set[str]:
        uris, offset = set(), 0
        while True:
            batch = self.api.current_user_playlists(limit=50, offset=offset)
            uris.update(p["uri"] for p in batch.get("items", []))
            if not batch.get("next"):
                return uris
            offset += 50

    def play_playlist(self, name: str):
        playlist = self.find_playlist(name)
        device = self.device_id()
        self.api.shuffle(False, device_id=device)
        self.api.start_playback(device_id=device, context_uri=playlist["uri"])
        wait_until(
            lambda: self.is_playing() and (self.playback().get("context") or {}).get("uri") == playlist["uri"],
            15, desc=f"开始播放歌单「{name}」",
        )

    def pause(self):
        try:
            self.api.pause_playback(device_id=self.device_id())
        except spotipy.SpotifyException:
            pass  # 已经是暂停状态
        wait_until(lambda: not self.is_playing(), 10, desc="Spotify 暂停")


class ServiceLog:
    """播放助手服务的日志文件"""

    def __init__(self):
        self._offset = 0

    def mark(self):
        """记录当前位置，之后只读取新写入的内容"""
        self._offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

    def since_mark(self) -> str:
        if not LOG_FILE.exists():
            return ""
        with open(LOG_FILE, "rb") as f:
            f.seek(self._offset)
            return f.read().decode("utf-8", errors="replace")

    def results_since_mark(self) -> list[str]:
        """服务执行指令后播报给用户的内容"""
        return [
            line.split("执行结果: ", 1)[1].strip()
            for line in self.since_mark().splitlines()
            if "执行结果: " in line
        ]


class Service:
    """被测的播放助手服务进程（用当前代码启动）"""

    def __init__(self, log: ServiceLog):
        self._log = log
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def _kill_running_instances():
        """停掉已在后台运行的服务，避免两个实例同时响应"""
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' "
                "-and $_.CommandLine -match 'main\\.py' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
            ],
            capture_output=True,
        )

    def start(self):
        self._kill_running_instances()
        self._log.mark()
        self._proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        wait_until(lambda: "服务已启动" in self._log.since_mark(), 60, desc="播放助手服务启动")

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop_and_restore_background(self):
        """测试结束：停掉测试实例，恢复日常使用的后台静默服务"""
        if self.is_running():
            self._proc.terminate()
            self._proc.wait(10)
        subprocess.run(["cscript", "//nologo", str(ROOT / "start_silent.vbs")], cwd=ROOT)
