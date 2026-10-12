"""小米账号登录态持久化，避免重复触发短信验证。"""

import logging
from pathlib import Path

from miservice import MiAccount

logger = logging.getLogger(__name__)

TOKEN_PATH = Path(__file__).resolve().parent.parent / ".mi.token"
LOGIN_HINT = "请在终端运行 python main.py --login 完成一次短信验证"


class MiLoginRequired(RuntimeError):
    """登录需要短信/邮箱验证码，后台模式无法完成。"""


class PersistentMiAccount(MiAccount):
    """保留 passToken 与 deviceId 的 MiAccount。

    miservice 原实现遇到 401 或登录失败会清空整个 token 并删除 .mi.token，
    随后以新的随机 deviceId 走密码登录，小米视为新设备而要求短信验证。
    passToken 长期有效，serviceToken 过期时只需用它换新即可。
    """

    def __init__(self, session, username: str, password: str, allow_otp: bool = False):
        super().__init__(session, username, password, token_store=str(TOKEN_PATH))
        self._allow_otp = allow_otp
        self._otp_blocked = False

    async def ensure_login(self, sid: str):
        """确保持有 sid 对应的 serviceToken，必要时用 passToken 续期。"""
        if self.token is None:
            self.token = await self.token_store.load_token()
        if self.token and sid in self.token:
            return
        if await self.login(sid):
            return
        if self._otp_blocked:
            raise MiLoginRequired(f"小米账号需要短信验证，{LOGIN_HINT}")
        raise RuntimeError(f"小米账号登录失败: {self._login_error or '未知错误'}")

    async def invalidate(self, sid: str):
        """丢弃失效的 serviceToken，保留 passToken 以便免验证续期。"""
        if self.token and self.token.pop(sid, None) is not None:
            await self.token_store.save_token(self.token)

    async def login(self, sid: str) -> bool:
        self._otp_blocked = False
        previous = dict(self.token) if self.token else None
        if await super().login(sid):
            return True
        # 原实现失败时清空 token 并删除文件；恢复以保留 deviceId 和 passToken
        if previous:
            previous.pop(sid, None)
            self.token = previous
            await self.token_store.save_token(previous)
        return False

    async def _verify_otp(self, sid: str, ntf: str) -> dict:
        # 后台隐藏窗口里 input() 会永久阻塞，且父类会先发送短信再等待输入
        if not self._allow_otp:
            self._otp_blocked = True
            raise MiLoginRequired("需要短信验证，后台模式不发送验证码")
        return await super()._verify_otp(sid, ntf)

    async def mi_request(self, sid: str, url: str, data, headers: dict, relogin: bool = True) -> dict:
        await self.ensure_login(sid)
        cookies = {"userId": self.token["userId"], "serviceToken": self.token[sid][1]}
        content = data(self.token, cookies) if callable(data) else data
        method = "GET" if data is None else "POST"
        async with self.request(url, method, data=content, cookies=cookies, headers=headers) as r:
            status = r.status
            if status == 200:
                resp = await r.json(content_type=None)
                if resp.get("code", -1) == 0:
                    return resp
                if "auth" in resp.get("message", "").lower():
                    status = 401
            else:
                resp = await r.text()
        if status == 401 and relogin:
            logger.warning("小米 serviceToken 已失效，使用 passToken 续期: %s", sid)
            await self.invalidate(sid)
            return await self.mi_request(sid, url, data, headers, False)
        raise Exception(f"Error {url}: {resp}")
