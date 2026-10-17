"""
The model-context budget, shared by the API and the workflow so they always agree
on which messages a session keeps. Standard library only (workflow sandbox).
"""


def trim_to_budget(messages, budget: int):
    """
    The newest messages whose total content length fits `budget`, oldest first.
    Returns (kept, dropped_count).

    Always keeps the last message, even when it alone is over the budget. Drops a
    leading assistant message so the tail starts on a user turn.
    """
    kept, used = [], 0
    for m in reversed(messages):
        n = len(m.content)
        if kept and used + n > budget:
            break
        kept.append(m)
        used += n
    kept.reverse()
    while len(kept) > 1 and kept[0].role != "user":
        kept.pop(0)
    return kept, len(messages) - len(kept)
