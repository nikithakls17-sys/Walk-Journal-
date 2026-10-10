# Build Log — bug diary

One line per thing that broke or surprised us: what happened → why → fix.

| When | What happened | Why | Fix |
|---|---|---|---|
| Oct 11 | Global git identity was a placeholder (`Your Name`) | Fresh machine config | Set repo-local `user.name`/`user.email` |
| Oct 11 | Blur check would reject every clear-sky photo | Variance-of-Laplacian is near zero for a smooth blue sky, same as a blurry shot | Skip the blur test for `sky`/`water` tasks; lowered threshold to 25 |
| Oct 11 | Long bash heredoc for `quests.py` failed with "unexpected EOF" | An apostrophe in `bird's nest` inside a nested quoted block | Wrote the file directly instead of via shell |
| Oct 11 | Python edit script crashed with UnicodeDecodeError on app.js | Windows defaults `open()` to cp1252, and the file has emoji | Run tooling with `PYTHONUTF8=1` |
| Oct 11 | Camera overlay covered every screen | `.camera { display: flex }` beats the `hidden` attribute's UA style | Global `[hidden] { display: none !important; }`; caught by a headless screenshot |
