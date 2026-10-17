"""
Chat sessions on Temporal.

    constants.py   names, error types, retry policy and timeouts (sandbox-safe)
    models.py      payload dataclasses (primitives only)
    context.py     the model-context budget, shared by the API and the workflow
    workflow.py    ChatSessionWorkflow: one per conversation; one Update per message
    activities.py  run_agent_turn: the agent, streaming tokens to Redis
    streaming.py   the Redis Stream side channel for tokens
    worker.py      the chat-worker service (python -m chat.worker)
"""
