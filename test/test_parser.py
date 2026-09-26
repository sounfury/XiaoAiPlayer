"""Spotify 指令解析单元测试。"""

import unittest

from modules.spotify.parser import Intent, parse


def test_pause():
    assert parse("暂停").intent == Intent.PAUSE
    assert parse("停止播放").intent == Intent.PAUSE
    assert parse("别放了").intent == Intent.PAUSE

def test_resume():
    assert parse("继续播放").intent == Intent.RESUME
    assert parse("继续").intent == Intent.RESUME
    assert parse("恢复播放").intent == Intent.RESUME

def test_next():
    assert parse("下一首").intent == Intent.NEXT
    assert parse("切歌").intent == Intent.NEXT
    assert parse("换一首").intent == Intent.NEXT

def test_previous():
    assert parse("上一首").intent == Intent.PREVIOUS
    assert parse("前一首").intent == Intent.PREVIOUS

def test_query_status():
    assert parse("现在播放什么歌").intent == Intent.QUERY_STATUS
    assert parse("现在放的什么").intent == Intent.QUERY_STATUS
    assert parse("什么歌").intent == Intent.QUERY_STATUS

def test_shuffle_my_playlist():
    cmd = parse("随机播放我的歌单")
    assert cmd.intent == Intent.SHUFFLE_MY_PLAYLIST

    cmd = parse("随机播放歌单")
    assert cmd.intent == Intent.SHUFFLE_MY_PLAYLIST

def test_shuffle_specific_playlist():
    cmd = parse("随机播放我的华语经典歌单")
    assert cmd.intent == Intent.SHUFFLE_PLAYLIST
    assert cmd.keyword == "华语经典"

    cmd = parse("随机播放工作歌单")
    assert cmd.intent == Intent.SHUFFLE_PLAYLIST
    assert cmd.keyword == "工作"

def test_play_playlist():
    cmd = parse("播放我的工作歌单")
    assert cmd.intent == Intent.PLAY_PLAYLIST
    assert cmd.keyword == "工作"

    cmd = parse("播放华语经典歌单")
    assert cmd.intent == Intent.PLAY_PLAYLIST
    assert cmd.keyword == "华语经典"

def test_search_play():
    cmd = parse("播放周杰伦的歌")
    assert cmd.intent == Intent.SEARCH_PLAY
    assert "周杰伦" in cmd.keyword

    cmd = parse("来一首稻香")
    assert cmd.intent == Intent.SEARCH_PLAY
    assert "稻香" in cmd.keyword

    cmd = parse("听一首夜曲")
    assert cmd.intent == Intent.SEARCH_PLAY
    assert "夜曲" in cmd.keyword

def test_unknown():
    assert parse("").intent == Intent.UNKNOWN
    assert parse("今天天气怎么样").intent == Intent.UNKNOWN


def load_tests(loader, tests, pattern):
    """让原有函数式断言也参与 unittest discover。"""
    suite = unittest.TestSuite()
    for name, func in globals().items():
        if name.startswith("test_") and callable(func):
            suite.addTest(unittest.FunctionTestCase(func))
    return suite


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  ✅ {name}")
            except AssertionError as e:
                print(f"  ❌ {name}: {e}")
    print("测试完成")
