"""小爱音箱对话轮询服务"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
from miservice import MiAccount, MiIOService, MiNAService

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    """一次对话记录"""

    query: str  # 用户说的话
    timestamp: float  # 时间戳（秒）


class XiaoAiService:
    """轮询小爱音箱最新对话，检测新指令"""

    def __init__(self, mi_user: str, mi_pass: str, mi_did: str = ""):
        self._mi_user = mi_user
        self._mi_pass = mi_pass
        self._mi_did = mi_did
        self._account: MiAccount | None = None
        self._mina: MiNAService | None = None
        self._miio: MiIOService | None = None
        self._session: aiohttp.ClientSession | None = None
        self._last_request_id: str = ""  # 上一次处理的 request_id，用于去重
        self._device_id: str = ""
        self._hardware: str = ""
        self._miot_did: str = ""
        self._media_guard_active = False
        self._consecutive_errors: int = 0

    async def initialize(self):
        """初始化小米账号和 MiNA 服务"""
        logger.info("正在登录小米账号...")
        if self._session and not self._session.closed:
            await self._session.close()

        self._session = aiohttp.ClientSession()
        self._account = MiAccount(self._session, self._mi_user, self._mi_pass)
        self._mina = MiNAService(self._account)
        self._miio = MiIOService(self._account)

        # 获取设备列表
        devices = await self._mina.device_list()
        if not devices:
            raise RuntimeError("未找到任何小爱音箱设备，请检查小米账号")

        # 选择目标设备
        if self._mi_did:
            target = next(
                (d for d in devices if d.get("deviceID") == self._mi_did), None
            )
            if not target:
                available = ", ".join(
                    f"{d.get('name', '?')}({d.get('deviceID', '?')})" for d in devices
                )
                raise RuntimeError(
                    f"未找到设备 {self._mi_did}，可用设备: {available}"
                )
        else:
            target = devices[0]
            logger.info("未指定 MI_DID，自动选择第一个设备")

        self._device_id = target.get("deviceID", "")
        self._hardware = target.get("hardware", "")
        self._miot_did = target.get("miotDID", "")
        device_name = target.get("name", "未知")
        logger.info(
            f"已连接小爱音箱: {device_name} (DID: {self._device_id}, Hardware: {self._hardware})"
        )

        # 获取初始对话状态用于去重，避免启动时误触发历史对话
        try:
            records = await self._get_conversation_records()
            if records:
                self._last_request_id = records[0].get("requestId") or records[0].get("request_id", "")
                logger.debug(f"初始 request_id: {self._last_request_id}")
        except Exception:
            pass

    async def _get_conversation_records(self) -> list[dict]:
        """获取最近对话记录，优先使用现代机型 userprofile 接口，失败降级到 ubus"""
        # 策略 1: userprofile 接口（支持小爱 Play 增强版/L05C、触屏版等绝大多数机型）
        try:
            await self._account.login("micoapi")
            cookies = {
                "userId": self._account.token["userId"],
                "serviceToken": self._account.token["micoapi"][1],
                "deviceId": self._device_id,
            }
            url = (
                f"https://userprofile.mina.mi.com/device_profile/v2/conversation"
                f"?source=dialogu&hardware={self._hardware}&limit=2"
            )
            headers = {"User-Agent": "MiHome/6.0.103 (iPad; iOS 14.4; Scale/2.00)"}

            async with self._account.request(
                url, "GET", cookies=cookies, headers=headers
            ) as r:
                resp = await r.json(content_type=None)
                if resp.get("code") == 0:
                    data_raw = resp.get("data", "{}")
                    data = (
                        json.loads(data_raw)
                        if isinstance(data_raw, str)
                        else data_raw
                    )
                    records = data.get("records", [])
                    if records:
                        return records
        except Exception as e:
            logger.warning(f"userprofile 接口获取失败: {e}", exc_info=True)

        # 策略 2: 降级回 ubus get_latest_ask
        try:
            return await self._mina.get_latest_ask(self._device_id)
        except Exception:
            return []

    async def get_latest_conversation(self) -> Conversation | None:
        """
        获取最新一条对话记录。
        如果是新对话（request_id 不同于上次）则返回，否则返回 None。
        """
        if not self._account:
            raise RuntimeError("服务未初始化，请先调用 initialize()")

        try:
            records = await self._get_conversation_records()
            if not records:
                return None

            latest = records[0]
            # 兼容两种接口的 ID 字段名
            request_id = latest.get("requestId") or latest.get("request_id", "")

            if not request_id or request_id == self._last_request_id:
                return None

            self._last_request_id = request_id

            # 提取用户提问
            query = ""
            if "query" in latest:
                query = latest.get("query", "").strip()
            elif "response" in latest:
                for answer in latest.get("response", {}).get("answer", []):
                    q = answer.get("question", "").strip()
                    if q:
                        query = q
                        break

            if not query:
                return None

            ts_ms = latest.get("time") or latest.get("timestamp_ms", 0)
            timestamp_s = ts_ms / 1000.0 if ts_ms else time.time()

            self._consecutive_errors = 0
            return Conversation(query=query, timestamp=timestamp_s)

        except Exception as e:
            self._consecutive_errors += 1
            logger.error(f"获取小爱对话记录失败 ({self._consecutive_errors} 次): {e}")
            # 连续失败 5 次以上，可能为 Token 过期或连接断开，尝试自动重新登录
            if self._consecutive_errors >= 5:
                logger.warning("检测到小米云端连接可能失效，正在尝试自动重新登录...")
                try:
                    await self.initialize()
                    self._consecutive_errors = 0
                    logger.info("小米账号自动重连成功！")
                except Exception as relogin_err:
                    logger.error(f"自动重连失败，将在下轮继续重试: {relogin_err}")
            return None

    async def stop_speaker_player(self):
        """停止小爱音箱本地的播放，避免与电脑媒体同时发声。"""
        if not self._mina or not self._device_id:
            return
        try:
            # 小爱 Play 增强版(L05C) 等机型不响应 stop（返回成功但仍在播放），只有 pause 有效
            accepted = await self._mina.player_pause(self._device_id)
            if not accepted:
                logger.warning("小爱音箱未接受本地暂停指令")
                return
            logger.debug("已触发暂停小爱音箱本地播放")
        except Exception as e:
            logger.warning(f"停止小爱本地播放失败: {e}")

    async def suppress_speaker_player(self, duration: float = 8.0, interval: float = 0.7):
        """在一段时间内反复停止音箱本地播放。
        小爱会先 TTS 回复再开始播放小米音乐，单次 stop 往往早于音乐开始而失效"""
        loop = asyncio.get_running_loop()
        end = loop.time() + duration
        while loop.time() < end:
            await self.stop_speaker_player()
            await asyncio.sleep(interval)

    async def enable_media_guard(self):
        """电脑媒体播放期间保护音箱，防止后续指令恢复本地队列。"""
        if self._media_guard_active or not self._miio or not self._miot_did:
            return
        try:
            code = await self._miio.miot_set_prop(self._miot_did, (2, 2), True)
            if code != 0:
                raise RuntimeError(f"MIoT 返回码 {code}")
            self._media_guard_active = True
            logger.info("已启用电脑媒体音箱保护")
        except Exception as e:
            logger.warning(f"启用电脑媒体音箱保护失败: {e}")

    async def enable_spotify_guard(self):
        """兼容旧调用。"""
        await self.enable_media_guard()

    async def disable_media_guard(self):
        """退出电脑媒体模式，并确保小爱本地媒体保持暂停。"""
        if not self._media_guard_active:
            return
        try:
            # L05C 的 Speaker.Mute 与媒体暂停联动；写入 False 会恢复旧队列，
            # 因此退出保护时只持续 pause，不能主动“解除静音”。
            await self.suppress_speaker_player(duration=2.0, interval=0.5)
            logger.info("已关闭电脑媒体音箱保护，本地媒体保持暂停")
        except Exception as e:
            logger.warning(f"关闭电脑媒体音箱保护失败: {e}")
        finally:
            self._media_guard_active = False

    async def disable_spotify_guard(self):
        """兼容旧调用。"""
        await self.disable_media_guard()

    async def close(self):
        """恢复音箱状态并关闭网络会话。"""
        await self.disable_media_guard()
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_tts(self, text: str):
        """让小爱音箱播报文字（TTS）"""
        if not self._mina:
            return
        if self._media_guard_active:
            logger.debug(f"电脑媒体保护期间跳过 TTS: {text}")
            return
        try:
            await self._mina.text_to_speech(self._device_id, text)
            logger.debug(f"TTS 播报: {text}")
        except Exception as e:
            logger.warning(f"TTS 播报失败: {e}")
