# Ask the App: notes for Claude sessions

A standalone project: a drop-in "ask a question" button (`web/ask.js`) plus a small server (`serve.py`) that
answers questions about a program from its help and code. It's meant to be reused in other projects, and
later merged into Code Lens and Stream Health (see README "Plans"), so keep it self-contained.

## Layout
- `askapp/config.py`: settings and defaults. Real settings live in `~/.config/ask-the-app/config.json`.
- `askapp/index.py`: file walk, secret filtering and redaction, chunking, BM25 search, and the mirror folder for Claude.
- `askapp/engines.py`: ollama / strata (OpenAI-style) / claude (`claude -p`, read-only tools, run in the mirror).
- `askapp/answer.py`: the prompts, one optional SEARCH round for local models, and SHOW/SOURCES parsing.
- `serve.py`: HTTP API + static files, job polling, CORS, and the IP allow list.
- `web/ask.js`: the button (shadow DOM, mic → /api/transcribe, Show me ring, speech out). `web/index.html` is the home page.

## Rules
- **Standard library only** (Python), with no build step and no npm. `ask.js` is one plain file.
- **Read-only.** Nothing here may change the program it answers about. The Claude engine gets only Read/Grep/Glob,
  inside the mirror folder.
- **No data or secrets reach an AI**: only code/doc extensions are indexed, secret-named files are skipped, and
  `index.redact()` runs on everything that is searched, prompted or mirrored. Keep it that way when adding sources.
- **Strata answers one chat at a time**: check `/status` and wait. Never wake or start it from here.
- Run `python3 selftest.py` after changes (no network needed). For the UI, a Playwright check against a test page
  that loads `ask.js` is worth doing; see the git history for what was tested.
- Write user-facing text in plain words: short sentences, no jargon.
