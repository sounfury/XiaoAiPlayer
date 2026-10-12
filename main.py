"""小爱模块化助手入口。"""

import asyncio
from contextlib import suppress
import logging
import sys

from config import Config
from core.assistant import AssistantCore
from core.jev import JevRouter
from core.mi_account import MiLoginRequired
from core.notifications import NotificationService
from core.ssh_tunnel import maintain_reverse_tunnel
from core.xiaoai import XiaoAiService
from modules.spotify.module import SpotifyModule, run_spotify as _run_spotify
from modules.spotify.service import SpotifyService
from modules.windows.module import WindowsAppCatalog, WindowsAppModule

logger = logging.getLogger("xiaoai_player")


def setup_logging(level: str = "INFO"):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("xiaoai_player.log", encoding="utf-8"),
        ],
    )


async def handle_command(intent, command, spotify: SpotifyService, xiaoai: XiaoAiService):
    """兼容旧调用入口；新轮询统一经 AssistantCore 分发。"""
    from modules.spotify.parser import Intent
    if intent == Intent.UNKNOWN:
        return
    module = SpotifyModule(spotify)
    core = AssistantCore(xiaoai, [module])
    await core.execute(module.prepare_parsed(command))


async def main_loop(config: Config):
    xiaoai = XiaoAiService(config.mi_user, config.mi_pass, config.mi_did)
    router = (
        JevRouter(config.jev_api_key, config.jev_model, config.jev_min_confidence)
        if config.jev_api_key else None
    )
    notifications = None
    tunnel_task = None
    try:
        await xiaoai.initialize()
        if config.hook_enabled:
            notifications = NotificationService(config, xiaoai)
            await notifications.start()
            if config.hook_ssh_host:
                tunnel_task = asyncio.create_task(
                    maintain_reverse_tunnel(config.hook_ssh_host, config.hook_port,
                                            config.hook_ssh_remote_port),
                    name="notification-ssh-tunnel",
                )
        spotify = None
        if config.spotify_client_id and config.spotify_client_secret:
            try:
                service = SpotifyService(
                    config.spotify_client_id,
                    config.spotify_client_secret,
                    config.spotify_redirect_uri,
                    device_name=config.spotify_device_name,
                )
                await asyncio.to_thread(service.initialize)
                spotify = SpotifyModule(service)
            except Exception as exc:
                logger.error("Spotify 模块初始化失败，Windows 模块继续运行: %s", exc, exc_info=True)
        windows = WindowsAppModule(WindowsAppCatalog(config.app_scan_interval))
        core = AssistantCore(xiaoai, [windows] + ([spotify] if spotify else []), router=router)
        enabled = [name for name, module in (("Spotify", spotify), ("Windows", windows)) if module]
        logger.info(
            "服务已启动；轮询间隔 %.1fs；Jev %s；模块: %s",
            config.poll_interval, "已启用" if router else "未配置", ", ".join(enabled),
        )
        await core.poll_forever(config.poll_interval)
    finally:
        try:
            if tunnel_task:
                tunnel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tunnel_task
        finally:
            try:
                if notifications:
                    await notifications.close()
            finally:
                try:
                    if router:
                        await router.close()
                finally:
                    await xiaoai.close()


async def login_once(config: Config):
    """前台交互登录：完成短信验证并保存登录态，供后台启动复用。"""
    xiaoai = XiaoAiService(config.mi_user, config.mi_pass, config.mi_did, allow_otp=True)
    try:
        await xiaoai.initialize()
        logger.info("小米账号登录成功，登录态已保存，之后开机后台启动无需再验证")
    finally:
        await xiaoai.close()


def main():
    config = Config()
    setup_logging(config.log_level)
    try:
        config.validate()
    except ValueError as exc:
        logger.error("配置错误: %s", exc)
        sys.exit(1)
    try:
        asyncio.run(login_once(config) if "--login" in sys.argv[1:] else main_loop(config))
    except MiLoginRequired as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("服务已停止")


if __name__ == "__main__":
    main()
