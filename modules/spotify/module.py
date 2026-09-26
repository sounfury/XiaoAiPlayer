"""Spotify 播放模块。歌单优先由 Jev 从实时列表中选择。"""

import asyncio
import logging
import random
from functools import partial

from core.jev import choose_catalog
from core.module_base import PreparedAction
from modules.spotify.parser import Intent, parse

logger = logging.getLogger(__name__)

PLAYBACK_INTENTS = {
    Intent.PLAY, Intent.RESUME, Intent.PAUSE, Intent.NEXT, Intent.PREVIOUS,
    Intent.SEARCH_PLAY, Intent.SHUFFLE_MY_PLAYLIST,
    Intent.SHUFFLE_PLAYLIST, Intent.PLAY_PLAYLIST,
}
SPOTIFY_MODE_INTENTS = PLAYBACK_INTENTS - {Intent.PAUSE}


def run_spotify(intent, command, spotify) -> str:
    """原有规则的执行器，也是 Jev 不可用时的回退。"""
    match intent:
        case Intent.PAUSE:
            spotify.pause()
            return "已暂停"
        case Intent.RESUME | Intent.PLAY:
            spotify.play()
            return "继续播放"
        case Intent.NEXT:
            spotify.next_track()
            return "下一首"
        case Intent.PREVIOUS:
            spotify.previous_track()
            return "上一首"
        case Intent.SEARCH_PLAY:
            return spotify.search_and_play(command.keyword)
        case Intent.SHUFFLE_MY_PLAYLIST:
            return spotify.shuffle_random_playlist()
        case Intent.SHUFFLE_PLAYLIST | Intent.PLAY_PLAYLIST:
            playlist = spotify.find_playlist(command.keyword)
            if not playlist:
                return f"没有找到名为「{command.keyword}」的歌单"
            return spotify.play_playlist(playlist["id"], shuffle=True)
        case Intent.QUERY_STATUS:
            return spotify.get_current_status()
    return ""


class SpotifyModule:
    name = "spotify"
    routes = {
        "spotify_playlist": "播放用户 Spotify 歌单，包括根据风格或名称选择歌单，以及从全部歌单随机选一个",
        "spotify_other": "Spotify 歌曲搜索、播放暂停、继续、切歌或播放状态查询",
    }
    fallback_route = "spotify_other"

    def __init__(self, service):
        self.service = service

    def fallback_match(self, query: str) -> bool:
        return parse(query).intent != Intent.UNKNOWN

    def preempt_speaker(self, query: str, route: str) -> bool:
        return route == "spotify_playlist" or parse(query).intent in PLAYBACK_INTENTS

    async def prepare(self, query: str, *, route: str, router=None) -> PreparedAction | None:
        prefer_playlist = route == "spotify_playlist" or (
            router is not None and parse(query).intent in {
                Intent.PLAY_PLAYLIST, Intent.SHUFFLE_PLAYLIST, Intent.SHUFFLE_MY_PLAYLIST,
            }
        )
        if prefer_playlist and router:
            chosen, fallback = await self._choose_playlist(query, router)
            if not fallback:
                return chosen
        command = parse(query)
        if command.intent == Intent.UNKNOWN:
            return None
        return self.prepare_parsed(command)

    def prepare_parsed(self, command) -> PreparedAction:
        intent = command.intent
        return PreparedAction(
            run=partial(run_spotify, intent, command, self.service),
            media_action="pause" if intent == Intent.PAUSE else (
                "play" if intent in SPOTIFY_MODE_INTENTS else "none"
            ),
            suppress_speaker=intent in PLAYBACK_INTENTS,
            speak=intent not in PLAYBACK_INTENTS,
        )

    async def _choose_playlist(self, query, router):
        try:
            playlists = await asyncio.to_thread(self.service.get_user_playlists)
        except Exception as exc:
            logger.warning("读取 Spotify 歌单失败，使用规则回退: %s", exc)
            return None, True
        valid = [p for p in playlists if p.get("id") and p.get("name")]
        if not valid:
            return PreparedAction(run=lambda: "你还没有任何歌单", media_action="none"), False
        by_key = {f"playlist_{i}": p for i, p in enumerate(valid)}
        common = {
            "none": "用户没有请求播放 Spotify 歌单",
            "random": "用户想从自己的所有歌单中随机选一个，没有指定歌单名称",
        }
        entries = {k: f"播放 Spotify 歌单「{p['name']}」" for k, p in by_key.items()}
        decision = await choose_catalog(
            router, query, entries, common,
            "根据用户的中文语音选择要播放的真实歌单；普通播放歌单也默认随机播放。"
            "只有未指定名称而要求随机选歌单时才选择 random。",
        )
        if decision.fallback:
            return None, True
        if decision.choice == "none":
            return None, False
        if decision.choice == "random":
            chosen = random.choice(valid)
        else:
            chosen = by_key.get(decision.choice)
        if not chosen:
            return None, True
        return PreparedAction(
            run=partial(self.service.play_playlist, chosen["id"], shuffle=True),
            media_action="play", suppress_speaker=True, speak=False,
        ), False
