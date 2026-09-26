"""小爱基座：轮询、模块注册与音箱播放策略。"""

import asyncio
import inspect
import logging

from core.module_base import CommandModule, PreparedAction

logger = logging.getLogger(__name__)


class AssistantCore:
    def __init__(self, xiaoai, modules: list[CommandModule], *, router=None):
        self.xiaoai = xiaoai
        self.modules = modules
        self.router = router
        self.routes = {}
        for module in modules:
            for route in module.routes:
                if route in self.routes or route == "none":
                    raise ValueError(f"重复的模块路由: {route}")
                self.routes[route] = module

    async def handle_query(self, query: str):
        route = None
        use_router = False
        if self.router:
            choices = {"none": "普通闲聊或不属于本助手的指令"}
            for module in self.modules:
                choices.update(module.routes)
            decision = await self.router.choose(
                query, choices,
                "将中文语音路由到唯一的功能；不确定是否为助手命令时选择 none。",
            )
            if not decision.fallback:
                route = decision.choice
                use_router = True
        if route is None:
            for module in self.modules:
                if module.fallback_match(query):
                    route = module.fallback_route
                    break
        if route is None or route == "none":
            logger.info("未识别的控制意图: %s", query)
            return
        module = self.routes.get(route)
        if module:
            # Jev 歌单选择还要读取 Spotify 列表；先阻止音箱抢先播放小米音乐。
            if module.preempt_speaker(query, route):
                stop = getattr(self.xiaoai, "stop_speaker_player", None)
                if inspect.iscoroutinefunction(stop):
                    await stop()
            action = await module.prepare(
                query, route=route, router=self.router if use_router else None,
            )
            if action:
                await self.execute(action)

    async def execute(self, action: PreparedAction):
        if action.media_action == "play":
            guard = getattr(self.xiaoai, "enable_media_guard", None)
            if not inspect.iscoroutinefunction(guard):
                guard = self.xiaoai.enable_spotify_guard  # 兼容旧接口和测试替身
            await guard()
        suppress_task = None
        if action.suppress_speaker:
            suppress = (self.xiaoai.suppress_speaker_player()
                        if action.suppress_duration == 8.0 else
                        self.xiaoai.suppress_speaker_player(duration=action.suppress_duration))
            suppress_task = asyncio.create_task(suppress)
        try:
            response = await asyncio.to_thread(action.run)
        except Exception as exc:
            response = f"操作失败: {exc}"
            logger.error("执行指令出错: %s", exc, exc_info=True)
        if suppress_task:
            await suppress_task
        if action.media_action == "pause" or (
            action.media_action == "play" and response.startswith(
                ("没有找到", "你还没有", "操作失败")
            )
        ):
            guard = getattr(self.xiaoai, "disable_media_guard", None)
            if not inspect.iscoroutinefunction(guard):
                guard = self.xiaoai.disable_spotify_guard
            await guard()
        if response:
            logger.info("执行结果: %s", response)
            # L05C 在播报后会恢复旧的小爱音乐队列，播放命令不播报。
            if action.speak:
                await self.xiaoai.send_tts(response)

    async def poll_forever(self, interval: float):
        while True:
            try:
                conversation = await self.xiaoai.get_latest_conversation()
                if conversation:
                    logger.info("收到小爱对话: %s", conversation.query)
                    await self.handle_query(conversation.query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("轮询异常: %s", exc, exc_info=True)
                await asyncio.sleep(interval * 5)
                continue
            await asyncio.sleep(interval)
