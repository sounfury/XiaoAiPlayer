"""Jev 有界语义选择；返回值只允许映射到本地提供的候选 ID。"""

import logging
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    choice: str | None = None
    # 仅 uncertain/error 才回退规则；模型明确选择 none 时不回退。
    fallback: bool = False


class JevRouter:
    URL = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, api_key: str, model: str = "jev-latest", min_confidence: float = 0.5):
        self._api_key = api_key
        self._model = model
        self._min_confidence = min_confidence
        self._session: aiohttp.ClientSession | None = None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def choose(self, query: str, choices: dict[str, str], question: str,
                     min_confidence: float = 0) -> Decision:
        if not choices:
            return Decision(fallback=True)
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        payload = {
            "model": self._model,
            "state": {"user_speech": query},
            "questions": {"target": {
                "type": "choice", "instructions": question, "criteria": choices,
            }},
        }
        try:
            async with self._session.post(
                self.URL, json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as response:
                response.raise_for_status()
                answer = (await response.json())["answers"]["target"]
            selected = answer["choice"]
            confidence = float(answer["confidence"])
            if selected not in choices or confidence < max(self._min_confidence, min_confidence):
                logger.info("Jev 选择不确定: %s (%.2f)", selected, confidence)
                return Decision(fallback=True)
            return Decision(choice=selected)
        except (aiohttp.ClientError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Jev 选择失败: %s", exc)
            return Decision(fallback=True)


async def choose_catalog(router, query: str, entries: dict[str, str],
                         common: dict[str, str], question: str,
                         min_confidence: float = 0.75) -> Decision:
    """在动态目录中选目标；超过单次 Choice 上限时分批且只接受无歧义结果。"""
    items = list(entries.items())
    batch_size = 250 - len(common)
    if batch_size < 1:
        raise ValueError("公共候选项过多")
    selected = set()
    for start in range(0, len(items), batch_size):
        choices = dict(common)
        choices.update(items[start:start + batch_size])
        decision = await router.choose(query, choices, question,
                                       min_confidence=min_confidence)
        if decision.fallback:
            return decision
        if decision.choice != "none":
            selected.add(decision.choice)
    if len(selected) > 1:
        logger.info("不同批次的 Jev 选择冲突: %s", selected)
        return Decision(fallback=True)
    return Decision(choice=next(iter(selected)) if selected else "none")
