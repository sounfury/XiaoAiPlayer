"""安装 Pi 完成事件适配器；令牌始终经文件传输，不放进命令行。

python integrations/pi/install.py --ssh-host oracle
仅接本机时省略 --ssh-host。安装后，新建 PiWeb 会话或执行 /reload。
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.ssh_tunnel import find_ssh


def update_env(path, updates):
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    remaining = dict(updates)
    lines = []
    for line in original.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in updates:
            if key in remaining:
                lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="", help="~/.ssh/config 中的 Host 别名")
    existing = dotenv_values(ROOT / ".env")
    parser.add_argument("--port", type=int, default=int(existing.get("HOOK_PORT") or 8765))
    parser.add_argument("--remote-port", type=int, default=int(existing.get("HOOK_SSH_REMOTE_PORT") or 8765))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.remote_port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    if args.ssh_host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.ssh_host):
        parser.error("请使用不含空格的 SSH Host 别名")
    env = dotenv_values(ROOT / ".env")
    token = env.get("HOOK_TOKEN") or secrets.token_urlsafe(32)
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent")))
    extension_dir = agent_dir / "extensions"
    extension_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(__file__).with_name("xiaoai-hook.js")
    shutil.copy2(extension, extension_dir / extension.name)
    config = {"enabled": True, "piwebOnly": True, "url": f"http://127.0.0.1:{args.port}/hooks/task-completed",
              "source": "本机 PiWeb", "token": token}
    (agent_dir / "xiaoai-hook.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.ssh_host:
        ssh = find_ssh()
        if not ssh:
            raise RuntimeError("找不到 SSH 客户端")
        scp = str(Path(ssh).with_name("scp.exe" if os.name == "nt" else "scp"))
        ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        subprocess.run([ssh, *ssh_opts, args.ssh_host, "mkdir -p ~/.pi/agent/extensions"], check=True, timeout=25)
        subprocess.run([scp, *ssh_opts, str(extension), f"{args.ssh_host}:.pi/agent/extensions/xiaoai-hook.js"], check=True, timeout=30)
        remote = {**config, "url": f"http://127.0.0.1:{args.remote_port}/hooks/task-completed", "source": f"{args.ssh_host} PiWeb"}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as handle:
                temporary = handle.name
                json.dump(remote, handle, ensure_ascii=False, indent=2)
            subprocess.run([scp, *ssh_opts, temporary, f"{args.ssh_host}:.pi/agent/xiaoai-hook.json"], check=True, timeout=30)
            subprocess.run([ssh, *ssh_opts, args.ssh_host, "chmod 600 ~/.pi/agent/xiaoai-hook.json"], check=True, timeout=25)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    update_env(ROOT / ".env", {"HOOK_ENABLED": "true", "HOOK_HOST": "127.0.0.1", "HOOK_PORT": args.port,
                              "HOOK_TOKEN": token, "HOOK_SSH_HOST": args.ssh_host, "HOOK_SSH_REMOTE_PORT": args.remote_port})
    print("Pi 完成通知已配置；重启 XiaoAiPlayer 后，在 PiWeb 新建会话或执行 /reload 生效。")
    print("本机来源：本机 PiWeb" + (f"；远端来源：{args.ssh_host} PiWeb" if args.ssh_host else ""))


if __name__ == "__main__":
    main()
