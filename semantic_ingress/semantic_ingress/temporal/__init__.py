"""
`semantic-ingress sync` as a Temporal workflow.

    models.py      payload dataclasses (no heavy imports; safe in the workflow sandbox)
    constants.py   activity names, task queue, retry policies and timeouts
    workflow.py    SyncWorkflow: the phase machine, sliding window and Continue-As-New
    activities.py  IndexActivities: the work, delegating to core/sync_steps.py
    worker.py      the worker `sync` embeds for the duration of a run
    client.py      start or attach to the run, print progress, return the outcome
"""
