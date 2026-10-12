"""配置管理模块"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # 小米账号
    mi_user: str = field(default_factory=lambda: os.getenv("MI_USER", ""))
    mi_pass: str = field(default_factory=lambda: os.getenv("MI_PASS", ""))
    mi_did: str = field(default_factory=lambda: os.getenv("MI_DID", ""))

    # Spotify
    spotify_client_id: str = field(
        default_factory=lambda: os.getenv("SPOTIFY_CLIENT_ID", "")
    )
    spotify_client_secret: str = field(
        default_factory=lambda: os.getenv("SPOTIFY_CLIENT_SECRET", "")
    )
    spotify_redirect_uri: str = field(
        default_factory=lambda: os.getenv(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
        )
    )
    spotify_device_name: str = field(
        default_factory=lambda: os.getenv("SPOTIFY_DEVICE_NAME", "")
    )

    # 可选的 Jev 语义选择；未配置时保留规则解析和软件精确名称匹配。
    jev_api_key: str = field(default_factory=lambda: os.getenv("JEV_API_KEY", ""))
    jev_model: str = field(default_factory=lambda: os.getenv("JEV_MODEL", "jev-latest"))
    jev_min_confidence: float = field(
        default_factory=lambda: float(os.getenv("JEV_MIN_CONFIDENCE", "0.5"))
    )
    app_scan_interval: float = field(
        default_factory=lambda: float(os.getenv("APP_SCAN_INTERVAL", "300"))
    )

    # 轮询间隔
    poll_interval: float = field(
        default_factory=lambda: float(os.getenv("POLL_INTERVAL", "2"))
    )

    # 可选的任务完成通知 webhook（由本机或外部调用方发送）。
    hook_enabled: bool = field(
        default_factory=lambda: os.getenv("HOOK_ENABLED", "false").lower() == "true"
    )
    hook_host: str = field(default_factory=lambda: os.getenv("HOOK_HOST", "127.0.0.1"))
    hook_port: int = field(default_factory=lambda: int(os.getenv("HOOK_PORT", "8765")))
    hook_token: str = field(default_factory=lambda: os.getenv("HOOK_TOKEN", ""))
    hook_text: str = field(
        default_factory=lambda: os.getenv("HOOK_TEXT", "{source}提醒你，{task}已经完成啦")
    )
    hook_ssh_host: str = field(default_factory=lambda: os.getenv("HOOK_SSH_HOST", ""))
    hook_ssh_remote_port: int = field(
        default_factory=lambda: int(os.getenv("HOOK_SSH_REMOTE_PORT", "8765"))
    )

    # 唤醒关键词
    wakeup_keywords: list[str] = field(default_factory=list)

    # 日志级别
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    def __post_init__(self):
        if not self.wakeup_keywords:
            raw = os.getenv(
                "WAKEUP_KEYWORD",
                "播放,暂停,继续,下一首,上一首,歌单,现在播放什么,什么歌,打开,启动,运行",
            )
            self.wakeup_keywords = [k.strip() for k in raw.split(",") if k.strip()]

    def validate(self):
        """校验必填配置"""
        errors = []
        if not self.mi_user:
            errors.append("MI_USER 未设置")
        if not self.mi_pass:
            errors.append("MI_PASS 未设置")
        if bool(self.spotify_client_id) != bool(self.spotify_client_secret):
            errors.append("SPOTIFY_CLIENT_ID 和 SPOTIFY_CLIENT_SECRET 必须同时设置")
        if not 0 <= self.jev_min_confidence <= 1:
            errors.append("JEV_MIN_CONFIDENCE 必须在 0 到 1 之间")
        if self.poll_interval <= 0 or self.app_scan_interval <= 0:
            errors.append("轮询和软件扫描间隔必须大于 0")
        if self.hook_enabled:
            from core.notifications import validate_hook_config
            try:
                validate_hook_config(self)
            except ValueError as exc:
                errors.append(str(exc))
        if self.hook_enabled and self.hook_ssh_host:
            if (self.hook_ssh_host.startswith("-")
                    or any(c.isspace() for c in self.hook_ssh_host)):
                errors.append("HOOK_SSH_HOST 必须是 SSH Host 别名")
            if not 1 <= self.hook_ssh_remote_port <= 65535:
                errors.append("HOOK_SSH_REMOTE_PORT 必须在 1 到 65535 之间")
        if errors:
            raise ValueError("配置缺失:\n  " + "\n  ".join(errors))
