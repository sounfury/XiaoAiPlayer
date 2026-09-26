import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # acceptance/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 项目根目录

from harness import ROOT, Service, ServiceLog, Speaker, Spotify  # noqa: E402

os.chdir(ROOT)  # .env / .mi.token / .spotipy_cache 都按项目根目录的相对路径读取

from config import Config  # noqa: E402


def before_all(context):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config()
    context.speaker = Speaker(cfg.mi_user, cfg.mi_pass, cfg.mi_did)
    context.spotify = Spotify(
        cfg.spotify_client_id, cfg.spotify_client_secret, cfg.spotify_redirect_uri
    )
    context.log = ServiceLog()
    context.service = Service(context.log)
    context.service.start()


def after_scenario(context, scenario):
    # 等小爱把播报说完，避免影响下一个场景的指令
    time.sleep(4)


def after_all(context):
    try:
        if context.spotify.device_online():
            context.spotify.pause()
    finally:
        context.speaker.close()
        context.service.stop_and_restore_background()
