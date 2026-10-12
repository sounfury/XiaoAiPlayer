"""可选 SSH 反向隧道，让远端通知源访问本机的 Hook。

使用 OpenSSH 配置中的 Host 别名；端口只绑定远端回环地址。
"""

import asyncio
import logging
import os
from pathlib import Path
import shutil
import subprocess

logger = logging.getLogger(__name__)


def find_ssh():
    """Windows 优先使用 Git SSH，与 Git Bash 中已验证的 Host 配置一致。"""
    if os.name == "nt":
        git_ssh = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "ssh.exe"
        if git_ssh.is_file():
            return str(git_ssh)
    return shutil.which("ssh")


async def maintain_reverse_tunnel(host: str, local_port: int, remote_port: int):
    ssh = find_ssh()
    if not ssh:
        raise RuntimeError("找不到 ssh，请安装 OpenSSH 或 Git for Windows")
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError("HOOK_SSH_HOST 必须是有效的 SSH Host 别名")
    if not 1 <= remote_port <= 65535:
        raise ValueError("HOOK_SSH_REMOTE_PORT 必须在 1 到 65535 之间")
    args = [
        ssh, "-N", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=20",
        "-o", "ServerAliveCountMax=3", "-R",
        f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}", host,
    ]
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            logger.info("SSH 通知隧道进程已启动: %s，远端回环端口 %s", host, remote_port)
            _, stderr = await process.communicate()
            logger.warning("SSH 通知隧道退出 (%s): %s", process.returncode,
                           stderr.decode("utf-8", errors="replace").strip()[-1000:])
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.warning("SSH 通知隧道启动失败: %s", exc)
        finally:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        await asyncio.sleep(5)
