"""指令解析模块 —— 从小爱对话文本中提取用户意图"""

import re
from dataclasses import dataclass
from enum import Enum, auto


class Intent(Enum):
    """用户意图枚举"""

    PLAY = auto()  # 播放（通用）
    PAUSE = auto()  # 暂停
    RESUME = auto()  # 继续播放
    NEXT = auto()  # 下一首
    PREVIOUS = auto()  # 上一首
    SEARCH_PLAY = auto()  # 搜索并播放指定歌曲/歌手
    PLAY_PLAYLIST = auto()  # 播放指定歌单
    SHUFFLE_MY_PLAYLIST = auto()  # 随机播放我的歌单
    SHUFFLE_PLAYLIST = auto()  # 随机播放指定歌单
    QUERY_STATUS = auto()  # 查询当前播放状态
    UNKNOWN = auto()  # 无法识别


@dataclass
class Command:
    """解析后的指令"""

    intent: Intent
    keyword: str = ""  # 搜索关键词 / 歌单名称
    raw_text: str = ""  # 原始文本


# 意图匹配规则，按优先级从高到低排列
_RULES: list[tuple[Intent, re.Pattern, int]] = [
    # 查询状态
    (Intent.QUERY_STATUS, re.compile(r"(现在|正在).*(播放|放).*(什么|啥)|什么歌|哪首歌"), 0),
    # 暂停
    (Intent.PAUSE, re.compile(r"暂停|停止播放|别放了|停"), 0),
    # 继续播放
    (Intent.RESUME, re.compile(r"继续(播放|放)?|恢复播放"), 0),
    # 下一首
    (Intent.NEXT, re.compile(r"下一首|下一曲|切歌|换一首"), 0),
    # 上一首
    (Intent.PREVIOUS, re.compile(r"上一首|上一曲|前一首"), 0),
    # 随机播放我的歌单（不带歌单名）
    (
        Intent.SHUFFLE_MY_PLAYLIST,
        re.compile(r"随机播放(我的)?(歌单|音乐|歌)$"),
        0,
    ),
    # 随机播放指定歌单（带歌单名）
    (
        Intent.SHUFFLE_PLAYLIST,
        re.compile(r"随机播放(我的)?(.+?)(歌单|播放列表)"),
        2,  # 捕获组索引：歌单名在 group(2)
    ),
    # 播放指定歌单
    (
        Intent.PLAY_PLAYLIST,
        re.compile(r"播放(我的)?(.+?)(歌单|播放列表)"),
        2,
    ),
    # 搜索播放（严格限定前缀，避免生活用语“放假”等误触发）
    (
        Intent.SEARCH_PLAY,
        re.compile(r"^(?:请?播放|我想听|来一首|放一首|听一首|听听|听|放首歌|放歌|放)(?:叫)?(.+)"),
        1,
    ),
]


def parse(text: str) -> Command:
    """解析对话文本，返回结构化指令"""
    text = text.strip()
    if not text:
        return Command(intent=Intent.UNKNOWN, raw_text=text)

    for intent, pattern, group_idx in _RULES:
        m = pattern.search(text)
        if m:
            keyword = ""
            if group_idx > 0:
                try:
                    keyword = m.group(group_idx).strip()
                except (IndexError, AttributeError):
                    pass
            return Command(intent=intent, keyword=keyword, raw_text=text)

    return Command(intent=Intent.UNKNOWN, raw_text=text)
