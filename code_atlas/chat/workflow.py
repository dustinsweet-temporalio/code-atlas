"""
ChatSessionWorkflow: one workflow per conversation, one Update per message.

The browser keeps the full transcript (IndexedDB); the workflow keeps the model
context only: the newest messages within a character budget, plus how many older
ones were dropped. Turns run one at a time (a lock). Each runs the agent in an
activity that streams tokens to Redis; the Update returns the final answer.

- The first message starts the workflow (Update-with-Start), seeded with the tail
  the browser sent. A message count check detects drift; the browser repairs it by
  resending its tail (resync).
- A turn already answered (same turn id) returns the recorded answer.
- `cancel_turn` stops the turn in flight (or one not started yet); `close` ends the session.
- The workflow completes after `idle_timeout_seconds` without a message; the next
  message starts a new run from the browser's copy. It continues as new when the
  server suggests it (or after `continue_as_new_after_turns`, a test hook).

Deterministic: imports only the standard library, temporalio and the chat payload
modules. Handlers raise only ApplicationError, which fails the Update, not the workflow.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError

with workflow.unsafe.imports_passed_through():
    from chat import constants as C
    from chat.context import trim_to_budget
    from chat.models import (
        ChatMessage,
        ChatSessionInput,
        SendMessageInput,
        SendMessageResult,
        TurnInput,
        TurnLookup,
        TurnResult,
    )


def _error_text(e: BaseException) -> str:
    cause = e
    while isinstance(cause, ActivityError) and cause.cause is not None:
        cause = cause.cause
    text = cause.message if isinstance(cause, ApplicationError) else (str(cause) or type(cause).__name__)
    return text[:500]


@workflow.defn(name=C.WORKFLOW_NAME)
class ChatSessionWorkflow:
    @workflow.init
    def __init__(self, inp: ChatSessionInput) -> None:
        # An Update-with-Start update runs before run() starts; init gives it the input.
        self._inp = inp
        self._messages: list[ChatMessage] = list(inp.messages)
        self._dropped = inp.dropped
        self._lock = asyncio.Lock()
        self._seq = 0                       # bumped on every message event; restarts the idle timer
        self._inflight = 0                  # Updates running or waiting for the lock
        self._closing = False
        self._turns_this_run = 0
        self._active_turn: str | None = None
        self._activity: workflow.ActivityHandle | None = None
        self._cancel_requests: set[str] = set()

    # ---------------- helpers ----------------

    def _total(self) -> int:
        return self._dropped + len(self._messages)

    def _carry(self) -> ChatSessionInput:
        i = self._inp
        return ChatSessionInput(
            session_id=i.session_id, repo_id=i.repo_id, messages=list(self._messages),
            dropped=self._dropped, idle_timeout_seconds=i.idle_timeout_seconds,
            context_char_budget=i.context_char_budget, turn_timeout_seconds=i.turn_timeout_seconds,
            continue_as_new_after_turns=i.continue_as_new_after_turns,
        )

    def _should_continue_as_new(self) -> bool:
        if self._inflight:  # never while a turn runs or waits (it would busy-loop until it finished)
            return False
        cap = self._inp.continue_as_new_after_turns
        return workflow.info().is_continue_as_new_suggested() or (cap > 0 and self._turns_this_run >= cap)

    def _recorded_answer(self, turn_id: str) -> str | None:
        for m in self._messages:
            if m.turn_id == turn_id and m.role == "assistant":
                return m.content
        return None

    def _append(self, *msgs: ChatMessage) -> None:
        self._messages.extend(msgs)
        kept, dropped = trim_to_budget(self._messages, self._inp.context_char_budget)
        self._dropped += dropped
        self._messages = kept

    def _finish(self, user: ChatMessage, answer: str, *, stopped: bool) -> SendMessageResult:
        self._append(user, ChatMessage("assistant", C.STOPPED_TEXT if stopped else answer, user.turn_id))
        self._turns_this_run += 1
        return SendMessageResult(user.turn_id, "" if stopped else answer, stopped, self._total())

    # ---------------- main loop: idle timer, Continue-As-New ----------------

    @workflow.run
    async def run(self, inp: ChatSessionInput) -> None:
        idle = timedelta(seconds=inp.idle_timeout_seconds)
        while True:
            seen = self._seq
            try:
                await workflow.wait_condition(
                    lambda: self._seq != seen or self._closing or self._should_continue_as_new(),
                    timeout=idle,
                )
            except asyncio.TimeoutError:
                if self._inflight:  # a turn longer than the idle timeout still counts as activity
                    continue
                break  # idle: the browser's IndexedDB is the record
            if self._closing:
                break
            if self._should_continue_as_new():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(self._carry())
        await workflow.wait_condition(workflow.all_handlers_finished)

    # ---------------- Update: one turn ----------------

    @workflow.update(name=C.SEND_MESSAGE_UPDATE)
    async def send_message(self, req: SendMessageInput) -> SendMessageResult:
        self._inflight += 1
        self._seq += 1
        try:
            async with self._lock:
                # A turn already answered (the browser resent it) returns the recorded answer.
                # Checked before the count check, which the resend would otherwise fail.
                done = self._recorded_answer(req.turn_id)
                if done is not None:
                    stopped = done == C.STOPPED_TEXT
                    return SendMessageResult(req.turn_id, "" if stopped else done, stopped, self._total())

                if self._closing:  # queued behind a turn when the session was closed
                    raise ApplicationError("session is closing", type=C.SESSION_CLOSING, non_retryable=True)

                if req.history is not None:  # explicit resync from the browser
                    self._messages = list(req.history)
                    self._dropped = max(0, req.client_count - len(req.history))
                elif req.client_count != self._total():
                    raise ApplicationError(
                        f"history out of sync (client {req.client_count}, session {self._total()})",
                        type=C.RESYNC_REQUIRED, non_retryable=True,
                    )

                user = ChatMessage("user", req.text, req.turn_id)
                if req.turn_id in self._cancel_requests:  # stop arrived before the turn started
                    self._cancel_requests.discard(req.turn_id)
                    return self._finish(user, "", stopped=True)

                self._active_turn = req.turn_id
                try:
                    self._activity = workflow.start_activity(
                        C.ACTIVITY_RUN_TURN,
                        TurnInput(self._inp.session_id, self._inp.repo_id, req.turn_id, self._messages + [user]),
                        result_type=TurnResult,
                        start_to_close_timeout=timedelta(seconds=self._inp.turn_timeout_seconds),
                        heartbeat_timeout=C.HEARTBEAT_TIMEOUT,
                        retry_policy=C.TURN_RETRY,
                        # The canceled activity has stopped writing to Redis before the next turn starts.
                        cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    )
                    result: TurnResult = await self._activity
                except ActivityError as e:
                    if isinstance(e.cause, CancelledError):
                        return self._finish(user, "", stopped=True)
                    raise ApplicationError(_error_text(e), type=C.TURN_FAILED, non_retryable=True) from e
                finally:
                    self._activity = None
                    self._active_turn = None
                    self._cancel_requests.discard(req.turn_id)
                return self._finish(user, result.answer, stopped=False)
        finally:
            self._inflight -= 1
            self._seq += 1

    @send_message.validator
    def _validate_send(self, req: SendMessageInput) -> None:
        # Cheap static checks only: the count check needs the lock (a queued turn would see a stale count).
        if self._closing:
            raise ApplicationError("session is closing", type=C.SESSION_CLOSING, non_retryable=True)
        if not req.text.strip() or len(req.text) > C.MAX_TEXT_CHARS:
            raise ApplicationError("invalid message", type=C.INVALID_INPUT, non_retryable=True)

    # ---------------- Signals / Query ----------------

    @workflow.signal(name=C.CANCEL_TURN_SIGNAL)
    def cancel_turn(self, turn_id: str) -> None:
        self._cancel_requests.add(turn_id)
        if self._activity is not None and self._active_turn == turn_id:
            self._activity.cancel()

    @workflow.signal(name=C.CLOSE_SIGNAL)
    def close(self) -> None:
        self._closing = True
        self._seq += 1
        if self._activity is not None:
            self._activity.cancel()

    @workflow.query(name=C.GET_TURN_QUERY)
    def get_turn(self, turn_id: str) -> TurnLookup:
        answer = self._recorded_answer(turn_id)
        return TurnLookup(found=answer is not None, answer=answer or "", stopped=answer == C.STOPPED_TEXT)
