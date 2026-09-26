"""各功能模块交给小爱基座执行的动作。"""

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class PreparedAction:
    run: Callable[[], str]
    media_action: str = "none"  # play / pause / none
    suppress_speaker: bool = False
    suppress_duration: float = 8.0
    speak: bool = True


class CommandModule(Protocol):
    name: str
    routes: dict[str, str]
    fallback_route: str

    def fallback_match(self, query: str) -> bool: ...

    def preempt_speaker(self, query: str, route: str) -> bool: ...

    async def prepare(self, query: str, *, route: str, router=None) -> PreparedAction | None: ...
