"""
Names, error types, the retry policy and timeouts of the chat session workflow.

Safe to import in the workflow sandbox (standard library and temporalio only).
The values are starting points; tune them against real runs.
"""
from datetime import timedelta

from temporalio.common import RetryPolicy

WORKFLOW_NAME = "ChatSessionWorkflow"
SEND_MESSAGE_UPDATE = "send_message"
CANCEL_TURN_SIGNAL = "cancel_turn"
CLOSE_SIGNAL = "close"
GET_TURN_QUERY = "get_turn"
ACTIVITY_RUN_TURN = "run_agent_turn"          # explicit name, as in the indexer


def workflow_id(repo_id: str, session_id: str) -> str:
    """One workflow per conversation; session_id is the browser's conversation id (a UUID)."""
    return f"chat-{repo_id}-{session_id}"


# ApplicationError types
RESYNC_REQUIRED = "RESYNC_REQUIRED"
INVALID_INPUT = "INVALID_INPUT"
SESSION_CLOSING = "SESSION_CLOSING"
CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
LLM_PERMANENT = "LLM_PERMANENT"
TURN_FAILED = "TURN_FAILED"
# Raised by the server (not us) when the workflow completed while an Update was in flight.
UPDATE_WORKFLOW_COMPLETED = "AcceptedUpdateCompletedWorkflow"

STOPPED_TEXT = "_(stopped)_"
MAX_TEXT_CHARS = 100_000

TURN_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
    non_retryable_error_types=[INVALID_INPUT, CONTEXT_OVERFLOW, LLM_PERMANENT],
)
HEARTBEAT_TIMEOUT = timedelta(seconds=30)
# Cancellation (Stop) reaches an activity only in a heartbeat response, and the SDK
# sends heartbeats at most every 0.8 x the heartbeat timeout unless the worker caps
# it. The chat worker caps it at HEARTBEAT_THROTTLE, so Stop takes a second or two.
HEARTBEAT_INTERVAL_SECONDS = 1.0
HEARTBEAT_THROTTLE = timedelta(seconds=2)
DEFAULT_TURN_TIMEOUT_SECONDS = 20 * 60
