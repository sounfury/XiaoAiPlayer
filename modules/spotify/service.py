"""Spotify 播放控制服务"""

import logging
import os
import random
import re
import socket
import subprocess
import time

import spotipy
from spotipy.oauth2 import SpotifyOAuth

logger = logging.getLogger(__name__)


# 语音里常用中文分类名，映射到 Spotify 中实际存在的英文歌单名。
# 使用完整匹配，避免“流行”等短词误伤更长的自定义歌单名称。
_PLAYLIST_ALIAS_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^(?:摇滚(?:乐|音乐)?|rock(?:\s*(?:and|&|n)\s*roll)?)$", re.I), "rock"),
    (re.compile(r"^(?:说唱(?:音乐)?|嘻哈(?:音乐)?|rap|hip[\s-]?hop)$", re.I), "rap"),
    (re.compile(r"^(?:电子(?:乐|音乐)?|电音|edm|electronic(?:\s+music)?)$", re.I), "Electronic"),
    (re.compile(r"^(?:日语|日文|日系|日本|j[\s-]?pop)$", re.I), "j pop"),
    (re.compile(r"^(?:蓝调|布鲁斯|blues?)$", re.I), "Blues"),
    (re.compile(r"^(?:爵士(?:乐|音乐)?|jazz)$", re.I), "Jazz"),
    (re.compile(r"^(?:流行(?:乐|音乐)?|欧美流行|pop)$", re.I), "pop"),
    # 用户现有歌单名拼写为 paino，同时兼容正确拼写 piano。
    (re.compile(r"^(?:钢琴(?:曲|音乐)?|轻音乐|piano|paino)$", re.I), "paino"),
    (re.compile(r"^(?:动漫|动画|二次元|二次元音乐|acg)$", re.I), "acg"),
    (re.compile(r"^(?:中文|国语|华语(?:音乐)?)$", re.I), "华语"),
    (re.compile(r"^(?:民谣(?:音乐)?|folk)$", re.I), "民谣"),
    (re.compile(r"^(?:最常听|常听歌曲|我的热门|top\s+tracks?)$", re.I), "My top tracks playlist"),
)


def _playlist_alias(name: str) -> str:
    """把自然语言歌单分类名转换为 Spotify 中的实际歌单名。"""
    normalized = re.sub(r"\s+", " ", name.strip())
    for pattern, target in _PLAYLIST_ALIAS_RULES:
        if pattern.fullmatch(normalized):
            return target
    return normalized


class SpotifyService:
    """封装 Spotify Web API 播放控制"""

    # 需要的 OAuth 权限
    SCOPES = " ".join(
        [
            "user-modify-playback-state",  # 播放控制
            "user-read-playback-state",  # 读取播放状态
            "user-read-currently-playing",  # 读取当前播放
            "playlist-read-private",  # 读取私有歌单
            "playlist-read-collaborative",  # 读取协作歌单
        ]
    )

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "http://127.0.0.1:8888/callback",
        device_name: str = "",
    ):
        self._device_name = device_name.strip() or os.getenv("COMPUTERNAME") or socket.gethostname()
        self._sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope=self.SCOPES,
                open_browser=True,
                cache_path=".spotipy_cache",
            )
        )
        self._device_id: str | None = None

    def initialize(self):
        """初始化：验证认证并找到 Windows 设备"""
        user = self._sp.current_user()
        logger.info(f"Spotify 已登录: {user['display_name']} ({user['id']})")
        self._refresh_device()

    def _launch_spotify_client(self) -> bool:
        """尝试在 Windows 电脑上自动拉起 Spotify 桌面客户端"""
        logger.info("未检测到活跃设备，尝试在 Windows 本机自动启动 Spotify 客户端...")
        try:
            # 方式 1: 使用 Windows 原生 protocol 关联
            os.startfile("spotify:")
            return True
        except Exception as e:
            logger.debug(f"通过 spotify: 协议启动失败: {e}")

        # 方式 2: 尝试常见安装路径
        appdata = os.getenv("APPDATA", "")
        spotify_path = os.path.join(appdata, "Spotify", "Spotify.exe")
        if os.path.exists(spotify_path):
            try:
                subprocess.Popen([spotify_path])
                return True
            except Exception as e:
                logger.error(f"通过绝对路径启动 Spotify 失败: {e}")

        return False

    def _refresh_device(self):
        """仅选择指定的电脑，绝不回退到音箱或其他设备。"""
        # 查询失败也不能继续使用上次缓存的设备。
        self._device_id = None
        try:
            devices = self._sp.devices()
        except Exception as e:
            logger.warning(f"获取 Spotify 设备列表失败: {e}")
            return

        device_list = devices.get("devices", [])

        if not device_list:
            logger.warning("未发现活跃的 Spotify 设备")
            self._device_id = None
            return

        candidates = [
            d for d in device_list
            if d.get("type") == "Computer"
            and d.get("name", "").casefold() == self._device_name.casefold()
            and d.get("id")
            and not d.get("is_restricted", False)
        ]
        if len(candidates) != 1:
            logger.warning(
                "无法唯一确定目标电脑 %r；可用设备: %s。不会控制其他设备。",
                self._device_name,
                [(d.get("name"), d.get("type")) for d in device_list],
            )
            return
        target = candidates[0]
        self._device_id = target["id"]
        logger.info(
            f"使用 Spotify 设备: {target['name']} ({target['type']})"
        )

        # 如果设备未处于激活状态，尝试将播放权迁移到该设备
        if not target.get("is_active", False):
            try:
                self._sp.transfer_playback(device_id=self._device_id, force_play=False)
            except Exception as e:
                logger.debug(f"转移设备播放焦点提示: {e}")

    def _ensure_device(self, auto_launch: bool = True):
        """确保有可用设备；若没有则尝试自动启动电脑端客户端并等待上线"""
        # 每次都刷新，避免客户端被关掉后仍使用过期的设备 ID
        self._refresh_device()

        # 电脑 Spotify 没启动时的自动拉起自愈机制
        if not self._device_id and auto_launch:
            if self._launch_spotify_client():
                # 轮询等待客户端上线（冷启动 + 登录注册设备较慢，最多等待 20 秒）
                for i in range(20):
                    time.sleep(1)
                    self._refresh_device()
                    if self._device_id:
                        logger.info(f"Spotify 客户端已成功拉起就绪 (耗时 {i+1}s)")
                        break

        if not self._device_id:
            raise RuntimeError(
                f"未找到目标电脑「{self._device_name}」上的 Spotify。"
                "请打开电脑客户端；若设备已改名，请设置 SPOTIFY_DEVICE_NAME。"
            )

    # ─── 基础播放控制 ────────────────────────────

    def play(self):
        """播放/恢复"""
        self._ensure_device()
        try:
            self._sp.start_playback(device_id=self._device_id)
            logger.info("▶ 播放")
        except spotipy.SpotifyException as e:
            if e.http_status == 403:
                logger.warning("Spotify 拒绝恢复播放 (HTTP 403): %s", e)
            raise

    def pause(self):
        """暂停"""
        # Spotify 没运行就没有东西需要暂停，不必拉起客户端
        self._refresh_device()
        if not self._device_id:
            logger.info("Spotify 未运行，无需暂停")
            return
        try:
            self._sp.pause_playback(device_id=self._device_id)
            logger.info("⏸ 暂停")
        except spotipy.SpotifyException as e:
            # 已经是暂停状态时不报错
            if "Player command failed: Restriction violated" in str(e):
                logger.debug("已经是暂停状态")
            else:
                raise

    def next_track(self):
        """下一首"""
        self._ensure_device()
        self._sp.next_track(device_id=self._device_id)
        logger.info("⏭ 下一首")

    def previous_track(self):
        """上一首"""
        self._ensure_device()
        self._sp.previous_track(device_id=self._device_id)
        logger.info("⏮ 上一首")

    # ─── 搜索并播放 ─────────────────────────────

    def _search_tracks(self, q: str) -> list[dict]:
        # Spotify 在中国大陆无运营，不能用 market=CN；
        # 不传 market 时，用户 token 会自动按账号所在地区过滤
        results = self._sp.search(q=q, type="track", limit=5)
        return results.get("tracks", {}).get("items", [])

    def search_and_play(self, query: str) -> str:
        """搜索歌曲/歌手并播放，返回描述信息"""
        self._ensure_device()

        tracks = []
        # “周杰伦的晴天” 先按 歌手+歌名 精确搜，搜不到再回退为原文搜索
        artist, sep, title = query.partition("的")
        if sep and artist and title:
            tracks = self._search_tracks(f"track:{title} artist:{artist}")
        if not tracks:
            tracks = self._search_tracks(query)

        if not tracks:
            return f"没有找到与「{query}」相关的歌曲"

        track = tracks[0]
        track_name = track["name"]
        artist_name = ", ".join(a["name"] for a in track["artists"])
        uri = track["uri"]

        self._sp.start_playback(device_id=self._device_id, uris=[uri])
        msg = f"正在播放: {track_name} - {artist_name}"
        logger.info(msg)
        return msg

    # ─── 歌单操作 ──────────────────────────────

    def get_user_playlists(self) -> list[dict]:
        """获取用户所有歌单"""
        playlists = []
        offset = 0
        while True:
            batch = self._sp.current_user_playlists(limit=50, offset=offset)
            items = batch.get("items", [])
            if not items:
                break
            playlists.extend(items)
            if not batch.get("next"):
                break
            offset += 50
        return playlists

    def find_playlist(self, name: str) -> dict | None:
        """模糊匹配歌单名称"""
        playlists = self.get_user_playlists()
        name_lower = name.lower().strip()

        # 精确匹配
        for p in playlists:
            if p["name"].lower().strip() == name_lower:
                return p

        # 用户没有同名歌单时，再尝试中英文分类别名。
        alias = _playlist_alias(name)
        alias_lower = alias.lower().strip()
        if alias_lower != name_lower:
            logger.info(f"歌单别名映射: {name} -> {alias}")
            for p in playlists:
                if p["name"].lower().strip() == alias_lower:
                    return p

        # 包含匹配
        for p in playlists:
            if name_lower in p["name"].lower() or p["name"].lower() in name_lower:
                return p

        return None

    def play_playlist(self, playlist_id: str, shuffle: bool = True) -> str:
        """播放指定歌单"""
        self._ensure_device()

        playlist = self._sp.playlist(playlist_id, fields="name,tracks.total")
        name = playlist.get("name", "未知歌单")
        track_total = playlist.get("tracks", {}).get("total", 0)

        # 随机模式直接选择一个随机起点。启动后立刻调用 next_track 容易被
        # Spotify 判定为 Restriction violated，且会造成一次多余的切歌。
        offset = (
            {"position": random.randrange(track_total)}
            if shuffle and track_total > 0
            else None
        )
        context_uri = f"spotify:playlist:{playlist_id}"
        self._sp.start_playback(
            device_id=self._device_id,
            context_uri=context_uri,
            offset=offset,
        )

        # 单曲播放或空闲时可能没有播放上下文，此时 Spotify 禁止切换随机模式。
        # 先确认目标设备已进入新歌单，再读取当前允许的操作。
        playback = None
        for attempt in range(6):
            playback = self._sp.current_playback()
            if (
                playback
                and (playback.get("device") or {}).get("id") == self._device_id
                and (playback.get("context") or {}).get("uri") == context_uri
            ):
                break
            if attempt < 5:
                time.sleep(0.5)
        else:
            logger.warning("歌单播放已发送，但未确认目标设备进入歌单上下文: %s", context_uri)
            return f"已请求播放歌单: {name}；暂未确认播放状态，随机模式未切换"

        if playback.get("shuffle_state") != shuffle:
            disallows = (playback.get("actions") or {}).get("disallows") or {}
            if disallows.get("toggling_shuffle"):
                logger.warning("Spotify 当前禁止切换随机模式: %s", context_uri)
                return f"正在播放歌单: {name}；Spotify 当前禁止切换随机模式"
            try:
                self._sp.shuffle(shuffle, device_id=self._device_id)
            except spotipy.SpotifyException as e:
                if e.http_status != 403 or "Restriction violated" not in str(e):
                    raise
                logger.warning("Spotify 拒绝切换随机模式: %s", e)
                return f"正在播放歌单: {name}；Spotify 当前禁止切换随机模式"

        mode = "随机播放" if shuffle else "播放"
        msg = f"正在{mode}歌单: {name}"
        logger.info(msg)
        return msg

    def shuffle_random_playlist(self) -> str:
        """随机选择一个歌单并随机播放"""
        playlists = self.get_user_playlists()
        if not playlists:
            return "你还没有任何歌单"

        chosen = random.choice(playlists)
        return self.play_playlist(chosen["id"], shuffle=True)

    # ─── 播放状态查询 ─────────────────────────────

    def get_current_status(self) -> str:
        """获取当前播放状态"""
        playback = self._sp.current_playback()

        if not playback or not playback.get("item"):
            return "当前没有在播放任何歌曲"

        item = playback["item"]
        track_name = item.get("name", "未知")
        artist_name = ", ".join(a["name"] for a in item.get("artists", []))
        is_playing = playback.get("is_playing", False)
        status = "正在播放" if is_playing else "已暂停"

        return f"{status}: {track_name} - {artist_name}"
