import time

from behave import given, then, when

from harness import wait_until

# 从对小爱说话到 Spotify 响应的最长等待（含轮询间隔、对话记录同步延迟、Spotify API 调用）
RESPONSE_TIMEOUT = 40
# Spotify 冷启动 + 设备注册较慢
COLD_START_TIMEOUT = 90
# 服务在收到指令后会持续压制音箱本地播放约 8 秒，之后再检查音箱是否安静
SPEAKER_SETTLE_SECONDS = 15


def _wait_command_handled(context, timeout=RESPONSE_TIMEOUT):
    """等服务处理完这条指令（播报了执行结果）"""
    return wait_until(context.log.results_since_mark, timeout, desc="服务处理完指令")


# ─── 前提 ─────────────────────────────────


@given("小爱播放助手服务正在运行")
def step_service_running(context):
    assert context.service.is_running(), "播放助手服务未在运行"


@given("电脑上的 Spotify 已经打开")
def step_spotify_open(context):
    context.spotify.open()


@given("电脑上的 Spotify 没有运行")
def step_spotify_not_running(context):
    context.spotify.quit()


@given('Spotify 正在按顺序播放歌单 "{name}"')
def step_playing_playlist(context, name):
    context.spotify.play_playlist(name)
    context.initial_track = context.spotify.current_track()


@given("Spotify 已暂停")
def step_spotify_paused(context):
    context.spotify.pause()


# ─── 动作 ─────────────────────────────────


@when('我对小爱说 "{text}"')
def step_say(context, text):
    context.log.mark()
    context.track_before = context.spotify.current_track() if context.spotify.device_online() else {}
    context.playing_before = context.spotify.is_playing() if context.spotify.device_online() else False
    context.said_at = time.time()
    context.speaker.say(text)


# ─── 结果：Spotify ─────────────────────────


@then('电脑上的 Spotify 应该在播放 "{title}"，歌手是 "{artist}"')
def step_spotify_plays_track(context, title, artist):
    seen = {}

    def check():
        track = context.spotify.current_track()
        artists = [a["name"] for a in track.get("artists", [])]
        seen["now"] = f"{track.get('name')} - {', '.join(artists)}"
        return context.spotify.is_playing() and title in track.get("name", "") and artist in artists

    timeout = COLD_START_TIMEOUT if getattr(context, "cold_start", False) else RESPONSE_TIMEOUT
    try:
        wait_until(check, timeout, desc=f"Spotify 播放「{title} - {artist}」")
    except AssertionError as e:
        raise AssertionError(f"{e}，实际在放: {seen.get('now')}") from None


@then("电脑上的 Spotify 应该处于暂停状态")
def step_spotify_paused_result(context):
    wait_until(lambda: not context.spotify.is_playing(), RESPONSE_TIMEOUT, desc="Spotify 暂停")


@then("电脑上的 Spotify 应该在播放")
def step_spotify_playing_result(context):
    wait_until(context.spotify.is_playing, RESPONSE_TIMEOUT, desc="Spotify 开始播放")


@then("电脑上的 Spotify 应该切换到另一首歌")
def step_spotify_changed_track(context):
    before = context.track_before.get("id")
    wait_until(
        lambda: context.spotify.is_playing() and context.spotify.current_track().get("id") != before,
        RESPONSE_TIMEOUT,
        desc=f"Spotify 从「{context.track_before.get('name')}」切换到另一首",
    )


@then("电脑上的 Spotify 应该回到最开始那首歌")
def step_spotify_back_to_initial(context):
    initial = context.initial_track
    wait_until(
        lambda: context.spotify.is_playing() and context.spotify.current_track().get("id") == initial.get("id"),
        RESPONSE_TIMEOUT,
        desc=f"Spotify 回到「{initial.get('name')}」",
    )


@then('电脑上的 Spotify 应该在播放歌单 "{name}"')
def step_spotify_plays_playlist(context, name):
    uri = context.spotify.find_playlist(name)["uri"]
    wait_until(
        lambda: context.spotify.is_playing()
        and (context.spotify.playback().get("context") or {}).get("uri") == uri,
        RESPONSE_TIMEOUT,
        desc=f"Spotify 播放歌单「{name}」",
    )


@then("电脑上的 Spotify 应该在播放我的某个歌单")
def step_spotify_plays_any_my_playlist(context):
    mine = context.spotify.my_playlist_uris()
    wait_until(
        lambda: context.spotify.is_playing()
        and (context.spotify.playback().get("context") or {}).get("uri") in mine,
        RESPONSE_TIMEOUT,
        desc="Spotify 播放我的某个歌单",
    )


# Spotify 播放状态是最终一致的：歌单切换可见后，随机状态可能稍晚才同步
@then("随机播放应该是开启的")
def step_shuffle_on(context):
    _wait_command_handled(context)
    wait_until(lambda: context.spotify.playback().get("shuffle_state") is True, 10, desc="随机播放开启")


@then("随机播放应该是关闭的")
def step_shuffle_off(context):
    _wait_command_handled(context)
    wait_until(lambda: context.spotify.playback().get("shuffle_state") is False, 10, desc="随机播放关闭")


@then("电脑上的 Spotify 的播放不应该受影响")
def step_spotify_unchanged(context):
    _wait_command_handled(context)
    track = context.spotify.current_track()
    assert track.get("id") == context.track_before.get("id"), (
        f"曲目被改变: {context.track_before.get('name')} -> {track.get('name')}"
    )
    assert context.spotify.is_playing() == context.playing_before, "播放/暂停状态被改变"


@then("电脑上的 Spotify 应该被自动打开")
def step_spotify_launched(context):
    context.cold_start = True
    wait_until(context.spotify.process_running, 30, desc="Spotify 进程启动")


@then("电脑上的 Spotify 不应该被打开")
def step_spotify_not_launched(context):
    _wait_command_handled(context)
    assert not context.spotify.process_running(), "Spotify 被意外打开了"


# ─── 结果：小爱音箱 ─────────────────────────


@then("小爱音箱不应该播放它自己的音乐")
def step_speaker_quiet(context):
    remaining = context.said_at + SPEAKER_SETTLE_SECONDS - time.time()
    if remaining > 0:
        time.sleep(remaining)
    for _ in range(3):
        assert not context.speaker.is_playing(), "小爱音箱仍在播放自己的音乐（二重奏）"
        time.sleep(1)


@then('小爱应该告诉我 "{text}"')
def step_speaker_says(context, text):
    results = _wait_command_handled(context)
    assert any(text in r for r in results), f"播报内容不符: {results}"


@then("小爱应该告诉我当前播放的歌名")
def step_speaker_says_current_track(context):
    results = _wait_command_handled(context)
    name = context.spotify.current_track().get("name", "")
    assert name and any(name in r for r in results), f"播报内容「{results}」不包含当前歌曲「{name}」"
