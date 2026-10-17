"""
Token streaming for chat turns: a Redis Stream per turn, written by the activity
and relayed to the browser by the API.

A Stream (not pub/sub) lets a reader that connects late replay from the start.
Events: t=chunk (d = text), t=reset (an activity retry restarted the answer),
t=done (d = "stopped" or empty). Streaming is best-effort: the Update result is the
authoritative answer, so a Redis failure degrades streaming and never fails a turn.
"""
import logging

logger = logging.getLogger("code_atlas.chat.streaming")

MAX_EVENTS = 20000


def turn_key(turn_id: str) -> str:
    return f"chat:turn:{turn_id}"


class TurnPublisher:
    """Activity side. A Redis failure is logged once; later events are skipped."""

    def __init__(self, redis, turn_id: str, ttl: int):
        self._redis, self._key, self._ttl, self._broken = redis, turn_key(turn_id), ttl, False

    async def _add(self, **fields) -> None:
        if self._broken or self._redis is None:
            return
        try:
            async with self._redis.pipeline(transaction=False) as p:
                p.xadd(self._key, fields, maxlen=MAX_EVENTS, approximate=True)
                p.expire(self._key, self._ttl)
                await p.execute()
        except Exception as e:
            self._broken = True
            logger.warning("stream publish failed for %s (streaming degraded): %s", self._key, e)

    async def chunk(self, text: str) -> None:
        await self._add(t="chunk", d=text)

    async def reset(self) -> None:
        await self._add(t="reset")

    async def done(self, stopped: bool = False) -> None:
        await self._add(t="done", d="stopped" if stopped else "")
