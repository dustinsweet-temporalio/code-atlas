# Chat: manual QA checklist

The browser side has no automated tests (no build step, no JS test setup). Run these
after changing `web/app.js`, `web/store.js` or the chat API. Use the compose stack
(`docker compose up --build -d`) and an indexed repository. Browser DevTools →
Application shows IndexedDB (`code-atlas` → `conversations`) and localStorage.

| # | Check | Expected |
| --- | --- | --- |
| 1 | Start a conversation, ask a question, reload the page. | The conversation and its answer are still there, read from IndexedDB. |
| 2 | Close the app, put conversations from an earlier version into localStorage key `code-atlas:conversations:v1` (or keep the ones you had), reload. | They appear in the sidebar with new UUID ids; the localStorage key is gone. |
| 3 | Set `code-atlas:conversations:v1` to invalid JSON (`{oops`), clear IndexedDB, reload. | The UI loads; the key is still there (nothing failed to import is deleted). |
| 4 | Build a conversation larger than 5 MB (paste long text in a few messages, or ask for long answers). | It saves (localStorage could not hold it); after a reload it is complete. Only the newest `CHAT_CONTEXT_CHARS` go to the model. |
| 5 | Ask for a long answer and click **Stop** while it streams. | Within a second or two the answer ends with `_(stopped)_`. The next message is sent with `resync: true` (DevTools → Network → the `/api/chat` request body). |
| 6 | Ask a question, then `docker compose restart chat-worker` while it answers. | The answer restarts (a reset clears the text) and completes after about 30 seconds. Reloading the page mid-answer instead: the answer is picked up again (reattach). |
| 7 | Set `CHAT_IDLE_HOURS=0.01` in `.env`, `docker compose up -d app`, ask a question, wait a minute. | The session workflow is completed in the Temporal UI; the next message starts a new one (a new run), and the model still knows the earlier conversation. |
| 8 | Delete a conversation. | It is gone from IndexedDB; its `chat-<index>-<id>` workflow completes (the close signal). |
| 9 | `docker compose stop redis`, ask a question. | No streaming: the whole answer appears at the end. The banner shows `Redis unavailable`. `docker compose start redis` afterwards. |
| 10 | `docker compose stop temporal`, ask a question. | The banner shows `Temporal unavailable`; the chat shows a clear error instead of hanging. `docker compose start temporal` afterwards. |
