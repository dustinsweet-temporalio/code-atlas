"""
The chat turn activity: run the agent on the session's context and stream its
tokens to Redis. Retries belong to the workflow's retry policy, so errors are
classified here: bad requests and permanent model errors are non-retryable;
throttling, timeouts, connection and server errors are retried.
"""
import asyncio

import anthropic
from strands.types.exceptions import (
    ContextWindowOverflowException,
    EventLoopException,
    MaxTokensReachedException,
    ModelThrottledException,
)
from temporalio import activity
from temporalio.exceptions import ApplicationError

from chat import constants as C
from chat.models import TurnInput, TurnResult
from chat.streaming import TurnPublisher

MESSAGE_CHARS = 1000


def _unwrap(e: BaseException) -> BaseException:
    """The model error behind Strands' EventLoopException wrapper."""
    seen = set()
    while isinstance(e, EventLoopException) and e.original_exception is not None and id(e) not in seen:
        seen.add(id(e))
        e = e.original_exception
    return e


def _to_activity_error(e: BaseException) -> ApplicationError:
    """The ApplicationError the activity raises for `e`, classified for TURN_RETRY."""
    cause = _unwrap(e)
    text = (str(cause) or type(cause).__name__)[:MESSAGE_CHARS]
    if isinstance(cause, ContextWindowOverflowException):
        return ApplicationError(text, type=C.CONTEXT_OVERFLOW, non_retryable=True)
    if isinstance(cause, MaxTokensReachedException):
        return ApplicationError(text, type=C.LLM_PERMANENT, non_retryable=True)
    if isinstance(cause, anthropic.APIStatusError):
        if 400 <= cause.status_code < 500 and cause.status_code not in (408, 409, 429):
            return ApplicationError(text, type=C.LLM_PERMANENT, non_retryable=True)
        return ApplicationError(text, type=type(cause).__name__)
    if isinstance(cause, (ModelThrottledException, anthropic.APIConnectionError)):
        return ApplicationError(text, type=type(cause).__name__)
    return ApplicationError(text, type=type(cause).__name__)  # unknown: retried by the policy


async def _heartbeat_loop() -> None:
    # A child task: create_task copies the activity's context, which heartbeat needs.
    while True:
        activity.heartbeat()
        await asyncio.sleep(C.HEARTBEAT_INTERVAL_SECONDS)


class ChatActivities:
    def __init__(self, redis, ttl: int):
        self._redis = redis
        self._ttl = ttl

    def all(self) -> list:
        return [self.run_agent_turn]

    @activity.defn(name=C.ACTIVITY_RUN_TURN)
    async def run_agent_turn(self, inp: TurnInput) -> TurnResult:
        from agent import AgentInputError, stream_answer  # agent.py has import-time side effects

        pub = TurnPublisher(self._redis, inp.turn_id, self._ttl)
        if activity.info().attempt > 1:
            await pub.reset()  # a retry restarts the answer
        heartbeat = asyncio.create_task(_heartbeat_loop())
        parts: list[str] = []
        try:
            payload = {
                "repo_id": inp.repo_id,
                "messages": [{"role": m.role, "content": m.content} for m in inp.messages],
            }
            async for chunk in stream_answer(payload, raise_errors=True):
                parts.append(chunk)
                await pub.chunk(chunk)
            await pub.done()
            return TurnResult(answer="".join(parts))
        except asyncio.CancelledError:
            await pub.done(stopped=True)
            raise  # the activity shows as cancelled
        except AgentInputError as e:
            raise ApplicationError(str(e)[:MESSAGE_CHARS], type=C.INVALID_INPUT, non_retryable=True) from None
        except Exception as e:
            # The message carries the model error; its exception chain (SDK and HTTP
            # library internals) stays out of the failure, which has a size limit.
            raise _to_activity_error(e) from None
        finally:
            heartbeat.cancel()
