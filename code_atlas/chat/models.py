"""
Payloads of the chat session workflow and its activity: dataclasses of primitives
only, safe in the workflow sandbox and able to gain fields without breaking history.
"""
from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    role: str                     # "user" | "assistant"
    content: str
    turn_id: str = ""


@dataclass
class ChatSessionInput:
    session_id: str
    repo_id: str
    messages: list[ChatMessage] = field(default_factory=list)   # model-context tail
    dropped: int = 0                                           # messages older than the tail
    idle_timeout_seconds: int = 86400
    context_char_budget: int = 240000
    turn_timeout_seconds: int = 1200
    continue_as_new_after_turns: int = 0                       # test hook; 0 = off


@dataclass
class SendMessageInput:
    turn_id: str
    text: str
    client_count: int                                          # messages the browser holds before `text`
    history: list[ChatMessage] | None = None                   # set only to resync


@dataclass
class SendMessageResult:
    turn_id: str
    answer: str
    stopped: bool = False
    total_count: int = 0


@dataclass
class TurnInput:
    session_id: str
    repo_id: str
    turn_id: str
    messages: list[ChatMessage] = field(default_factory=list)  # context tail + the new user message last


@dataclass
class TurnResult:
    answer: str


@dataclass
class TurnLookup:
    found: bool
    answer: str = ""
    stopped: bool = False
