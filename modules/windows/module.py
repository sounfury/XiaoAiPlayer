"""发现并启动 Windows 已安装软件，不接受语音或模型提供的任意执行路径。"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from core.jev import choose_catalog
from core.module_base import PreparedAction

logger = logging.getLogger(__name__)

_EXCLUDED = ("uninstall", "unins", "setup", "installer", "update", "updater", "readme", "help", "卸载", "安装程序")
_LAUNCH_VERBS = ("打开", "启动", "运行", "开启")


@dataclass(frozen=True)
class InstalledApp:
    name: str
    path: Path


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _usable(name: str, path: Path) -> bool:
    lower = name.casefold()
    stem = path.stem.casefold()
    return (bool(name.strip()) and _safe_is_file(path)
            and not any(word in lower or word in stem for word in _EXCLUDED))


def _icon_exe(value: str) -> Path | None:
    """注册表 DisplayIcon 可为带引号路径或带 ,0 图标索引的路径。"""
    value = os.path.expandvars(value.strip())
    if value.startswith('"'):
        end = value.find('"', 1)
        candidate = value[1:end] if end > 1 else ""
    else:
        match = re.match(r"(.+?\.exe)(?:,\d+)?$", value, re.I)
        candidate = match.group(1) if match else ""
    path = Path(candidate) if candidate else None
    return path if path and path.suffix.lower() == ".exe" and _usable(path.stem, path) else None


class WindowsAppCatalog:
    def __init__(self, refresh_seconds: float = 300):
        self.refresh_seconds = refresh_seconds
        self._cached: list[InstalledApp] = []
        self._refreshed_at = 0.0

    def list_apps(self) -> list[InstalledApp]:
        if not self._refreshed_at or time.monotonic() - self._refreshed_at >= self.refresh_seconds:
            self._cached = self._discover()
            self._refreshed_at = time.monotonic()
            logger.info("发现 %d 个可启动 Windows 软件", len(self._cached))
        return list(self._cached)

    def _discover(self) -> list[InstalledApp]:
        if sys.platform != "win32":
            return []
        apps: dict[str, InstalledApp] = {}

        def add(name: str, path: Path):
            name = name.strip()
            if _usable(name, path):
                apps.setdefault(name.casefold(), InstalledApp(name, path))

        # 开始菜单名称通常最接近用户实际说的软件名称。
        for env_name in ("APPDATA", "PROGRAMDATA"):
            value = os.getenv(env_name)
            if not value:
                continue
            base = Path(value) / "Microsoft/Windows/Start Menu/Programs"
            if _safe_is_dir(base):
                try:
                    for link in base.rglob("*.lnk"):
                        add(link.stem, link)
                except OSError as exc:
                    logger.warning("扫描开始菜单时跳过异常目录: %s", exc)

        try:
            import winreg
            roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
            for root in roots:
                for key_path in (
                    r"Software\Microsoft\Windows\CurrentVersion\App Paths",
                    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                ):
                    try:
                        parent = winreg.OpenKey(root, key_path)
                    except OSError:
                        continue
                    with parent:
                        for i in range(winreg.QueryInfoKey(parent)[0]):
                            try:
                                child = winreg.OpenKey(parent, winreg.EnumKey(parent, i))
                                with child:
                                    if key_path.endswith("App Paths"):
                                        raw = str(winreg.QueryValueEx(child, "")[0])
                                        path = _icon_exe(raw)
                                        if path:
                                            add(path.stem, path)
                                    else:
                                        def field(label):
                                            try:
                                                return str(winreg.QueryValueEx(child, label)[0])
                                            except OSError:
                                                return ""

                                        name = field("DisplayName")
                                        path = _icon_exe(field("DisplayIcon"))
                                        if not path:
                                            location = field("InstallLocation")
                                            folder = Path(os.path.expandvars(location)) if location else None
                                            if folder and _safe_is_dir(folder):
                                                exes = [p for p in folder.glob("*.exe") if _usable(p.stem, p)]
                                                matching = [p for p in exes if p.stem.casefold() in name.casefold() or name.casefold() in p.stem.casefold()]
                                                path = matching[0] if matching else (exes[0] if len(exes) == 1 else None)
                                        if path:
                                            add(name or path.stem, path)
                            except (OSError, ValueError):
                                continue
        except ImportError:
            pass

        # 覆盖没有注册卸载信息、但确实放在常用安装目录的软件。
        install_roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)")]
        local = os.getenv("LOCALAPPDATA")
        if local:
            install_roots.append(str(Path(local) / "Programs"))
        for value in install_roots:
            root = Path(value) if value else None
            if root and _safe_is_dir(root):
                try:
                    folders = root.iterdir()
                    for folder in folders:
                        try:
                            if not _safe_is_dir(folder):
                                continue
                            exes = [p for p in folder.glob("*.exe") if _usable(p.stem, p)]
                            matching = [p for p in exes if p.stem.casefold() == folder.name.casefold()]
                            if matching or len(exes) == 1:
                                add(folder.name, (matching or exes)[0])
                        except OSError as exc:
                            logger.warning("跳过不可访问的安装目录 %s: %s", folder, exc)
                except OSError as exc:
                    logger.warning("跳过不可访问的安装根目录 %s: %s", root, exc)
        return sorted(apps.values(), key=lambda app: app.name.casefold())


def _spoken_name(query: str) -> str:
    name = query.strip()
    for prefix in ("请帮我", "帮我", "请", "把"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for verb in _LAUNCH_VERBS:
        if name.startswith(verb):
            name = name[len(verb):]
            break
    for suffix in ("一下", "吧"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip(" ，。！! ")


class WindowsAppModule:
    name = "windows"
    routes = {"windows": "明确要求打开、启动或运行 Windows 电脑上安装的软件"}
    fallback_route = "windows"

    def __init__(self, catalog: WindowsAppCatalog | None = None):
        self.catalog = catalog or WindowsAppCatalog()

    def fallback_match(self, query: str) -> bool:
        text = query.strip()
        for prefix in ("请帮我", "帮我", "请", "把"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        return text.startswith(_LAUNCH_VERBS)

    def preempt_speaker(self, query: str, route: str) -> bool:
        return True

    async def prepare(self, query: str, *, route: str, router=None) -> PreparedAction | None:
        apps = await asyncio.to_thread(self.catalog.list_apps)
        if not apps:
            return PreparedAction(run=lambda: "没有发现可启动的软件",
                                  suppress_speaker=True, suppress_duration=3.5)
        selected = None
        if router:
            by_key = {f"app_{i}": app for i, app in enumerate(apps)}
            entries = {key: f"启动 Windows 软件「{app.name}」" for key, app in by_key.items()}
            decision = await choose_catalog(
                router, query, entries,
                {"none": "用户没有明确要求启动这些软件中的任何一个"},
                "用户想打开哪个已安装的 Windows 软件？只在明确要求启动软件时选择软件。",
            )
            if not decision.fallback:
                if decision.choice == "none":
                    if self.fallback_match(query):
                        return PreparedAction(run=lambda: "没有找到匹配的软件",
                                              suppress_speaker=True, suppress_duration=3.5)
                    return None
                selected = by_key.get(decision.choice)
        if selected is None:
            target = _spoken_name(query).casefold()
            targets = {target, target.removesuffix("软件"), target.removesuffix("程序")}
            matches = [app for app in apps if targets.intersection(
                (app.name.casefold(), app.path.stem.casefold()))]
            if len(matches) == 1:
                selected = matches[0]
        if selected is None:
            return PreparedAction(run=lambda: "没有找到匹配的软件",
                                  suppress_speaker=True, suppress_duration=3.5)
        return PreparedAction(run=lambda: self._launch(selected),
                              suppress_speaker=True, suppress_duration=3.5)

    @staticmethod
    def _launch(app: InstalledApp) -> str:
        if not _safe_is_file(app.path):
            return f"软件「{app.name}」的位置已失效"
        if app.path.suffix.lower() == ".lnk":
            os.startfile(str(app.path))
        elif app.path.suffix.lower() == ".exe":
            subprocess.Popen([str(app.path)], cwd=str(app.path.parent))
        else:
            return "该软件没有可用的启动入口"
        return f"已启动 {app.name}"
