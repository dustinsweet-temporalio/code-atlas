"""The model-context budget (chat/context.py), shared by the API and the workflow."""
from chat.context import trim_to_budget
from chat.models import ChatMessage as M


def _msgs(*pairs):
    return [M(role, text) for role, text in pairs]


def test_empty_input():
    assert trim_to_budget([], 100) == ([], 0)


def test_keeps_the_newest_messages_that_fit():
    msgs = _msgs(("user", "a" * 40), ("assistant", "b" * 40), ("user", "c" * 40), ("assistant", "d" * 40))
    kept, dropped = trim_to_budget(msgs, 100)
    assert [m.content[0] for m in kept] == ["c", "d"] and dropped == 2


def test_a_single_message_over_the_budget_is_kept():
    kept, dropped = trim_to_budget(_msgs(("user", "x" * 500)), 100)
    assert len(kept) == 1 and dropped == 0


def test_the_tail_starts_on_a_user_message():
    msgs = _msgs(("user", "a" * 30), ("assistant", "b" * 30), ("user", "c" * 30), ("assistant", "d" * 30))
    kept, dropped = trim_to_budget(msgs, 95)  # fits b, c, d; b is an assistant message, so it goes too
    assert [m.role for m in kept] == ["user", "assistant"] and dropped == 2


def test_dropped_plus_kept_is_every_message():
    msgs = _msgs(*[("user" if i % 2 == 0 else "assistant", "z" * (i * 7 % 50 + 1)) for i in range(40)])
    for budget in (0, 1, 30, 200, 10_000):
        kept, dropped = trim_to_budget(msgs, budget)
        assert dropped + len(kept) == len(msgs)
        assert kept[-1] is msgs[-1]
