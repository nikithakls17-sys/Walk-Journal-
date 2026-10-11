# Build Log — bug diary

One line per thing that broke or surprised us: what happened → why → fix.

| When | What happened | Why | Fix |
|---|---|---|---|
| Oct 11 | Global git identity was a placeholder (`Your Name`) | Fresh machine config | Set repo-local `user.name`/`user.email` |
| Oct 11 | Blur check would reject every clear-sky photo | Variance-of-Laplacian is near zero for a smooth blue sky, same as a blurry shot | Skip the blur test for `sky`/`water` tasks; lowered threshold to 25 |
| Oct 11 | Long bash heredoc for `quests.py` failed with "unexpected EOF" | An apostrophe in `bird's nest` inside a nested quoted block | Wrote the file directly instead of via shell |
| Oct 11 | Python edit script crashed with UnicodeDecodeError on app.js | Windows defaults `open()` to cp1252, and the file has emoji | Run tooling with `PYTHONUTF8=1` |
| Oct 11 | Camera overlay covered every screen | `.camera { display: flex }` beats the `hidden` attribute's UA style | Global `[hidden] { display: none !important; }`; caught by a headless screenshot |
| Oct 11 | Tracing outcomes (photo match / no match) never reached Sentry | Callers set the outcome after `gemma.chat` returned, when the span had already closed | `gemma.chat(..., outcome=fn)` labels the result inside the span |
| Oct 11 | Tracing test found no `gen_ai.chat` span in the transaction | sentry-sdk 2.71 streams gen_ai spans as standalone v2 `span` envelope items | Test reads both transaction spans and standalone span items |
| Oct 11 | A blank `ELEVENLABS_VOICE_ID=` in .env would have sent an empty voice ID to ElevenLabs | `os.getenv(key, default)` returns "" when the key exists but is empty | Use `os.getenv(key) or default` |
| Oct 11 | First real Gemma call: plain-text reply ("Enjoy your walk!") instead of the JSON schema | `gemma4:e4b` is a thinking model; with thinking on, the reply sometimes ignored the `format` schema, and it was slow | Send `"think": false`: valid JSON 3/3, about 2 s per reply |
| Oct 11 | Ollama 500: "unable to allocate CPU buffer of size 4448861504" | Only 1.7 GB of 15.7 GB RAM free, and Gemma needs about 4.4 GB to load (Iris Xe GPU, so no VRAM offload) | Closed OneDrive and other apps, then it loaded |
| Oct 11 | Medium "Walk from here" quest came out with 3 tasks instead of 5 | The "no task types from the last 3 quests" rule emptied the seasonal bank; place quests hid it by padding with place tasks | When the bank runs dry, reuse older task types (never twice in one quest) |
| Oct 11 | Running one test file alone wrote 4 test quests and 2 photos into the real `data/` database | Test isolation lived in `test_core.py`; a file that imported the app first loaded the real `.env` (real data folder, real Gemma, real keys) | Moved isolation to `tests/conftest.py`, which asserts it never sees `data/` or real keys; deleted the test rows by ID after a backup |
| Oct 11 | Regex for "no destinations" matched nothing | A shell heredoc turned `\b` into a literal backspace character | Rewrote the escapes; checked no source file has control characters |
| Oct 11 | First real "Walk from here" quest took 114 s | Gemma's first draft had 2 daytime sky tasks at night, so the rules asked for a second draft | Working as designed; noted that quest creation can take 1-2 min on this laptop |
