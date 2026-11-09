# Ask the App

**A button you can add to any program: ask it a question, typed or spoken, and get the answer then and there.**
"How do I send an estimate?", "Where do I change the email wording?", "Why didn't this invoice get marked paid?"

It looks in the program's **help first, then its code**, writes the answer in plain words, and its
**👉 Show me** button scrolls to the button you need and puts a ring around it. It can read the answer out loud.

It never changes anything: it only looks.

```
 you ask ──► 🎤 Whisper (if spoken) ──► search the help + code ──► an AI writes the answer ──► answer + Show me
                                          (instant, on this PC)     Claude / Strata / Qwen
```

## Quick start

```bash
cd ask-the-app
./run.sh /path/to/your/program        # opens http://127.0.0.1:8795/ and answers questions about that folder
```

You need Python 3.10 or newer and nothing else (standard library only). For the answers you need at least one
of the AIs below.

## Put the button in a program

Add one line to the program's web page, before `</body>`:

```html
<script src="http://127.0.0.1:8795/ask.js" data-project="ttr"></script>
```

A round **?** button appears in the bottom corner. Everything it draws is inside its own shadow root, so it
can't change how the program looks, and the program can't break it.

| Option | What it does |
|---|---|
| `data-project` | which program the questions are about (an id from the settings) |
| `data-server` | where Ask the App runs (default: wherever `ask.js` came from) |
| `data-inline="someId"` | fill that element with the panel instead of a floating button |
| `data-position="left"` | button on the left |
| `data-theme="dark"` / `"light"` | otherwise it follows the computer's setting |
| `data-accent="#2a7d4f"` | button colour |

The program can tell it more about what's on screen, and react to answers:

```js
AskTheApp.context = () => ({ place: 'Jobs tab', selected: 'Job 1234' });  // sent with every question
AskTheApp.onSource = (src) => openFile(src.file, src.start);              // makes "where this came from" clickable
window.addEventListener('asktheapp:answer', (e) => console.log(e.detail));  // every answer
AskTheApp.ask('How do I make a backup?');                                 // ask from your own button
```

**What it sends with a question:** the page title and address, the selected tab, the headings, and the labels of
the buttons and links that are visible. It never sends what's typed in forms. Each question has a "screen
info sent" link that shows exactly what went.

## The AIs

| Engine | What it is | Good at |
|---|---|---|
| `claude` | the `claude` command line, using your Claude subscription (no API key) | the best and quickest at reading code (about 8 s, a few cents of usage per question) |
| `strata` | the big local model on the main PC | good answers, private, slower (about 50 s); only used when it's already on |
| `ollama` | a local model through Ollama (qwen3:8b) | always there and private, but it can get details wrong in big code |

**Auto** tries them in the order in `auto_order` and skips any that are off. The person asking can pick one
from the panel's drop-down.

- **Claude runs online.** The question, the on-screen labels and parts of the code go to Anthropic, and the
  panel says so. It works inside a **mirror folder** (`~/.cache/ask-the-app/mirror/<project>`) that holds
  only the indexed files, with secrets hidden. It has only the Read, Grep and Glob tools, so it can't change,
  run or open anything else.
- **Strata answers one chat at a time.** Its `/status` is checked first, and a busy Strata is waited for
  (`busy_wait_s`) rather than interrupted. Auto then moves on to the next engine.

## Speaking the question

Set `transcribe_url` to an AI Hub's `/api/dictate` (Whisper). The 🎤 then appears in the panel: press it,
speak, and press ■. The question is asked as soon as Whisper has written it out, and the answer is read
out loud. Browsers only allow the microphone on **https** pages or on `localhost`.

## Settings

Everything goes in `~/.config/ask-the-app/config.json`. You can also set `ASKAPP_CONFIG` or use
`serve.py --config`. Start from [config.example.json](config.example.json). A project needs:

- `root`: the program's folder.
- `help`: its help files. These can also be **part of a file**: `{"file": "app.html", "from": "function
  helpPanel(", "to": "\n}\n", "heading": "sec\\('\\w+',\\s*'([^']*)'", "title": "Help tab"}` reads a Help
  tab that is built in JavaScript, and turns each `sec('id','Title', …)` into a heading.
- `exclude` (optional): folders or files to leave out, such as tests, blog posts and old copies. Names without a
  `/` match any folder or file name.
- `about` and `examples` (optional): a sentence about what the program is for (this helps the AI), and the
  example questions shown before the first question.

### What's never read

- Folders such as `.git`, `node_modules`, `venv`, `backups`, `__pycache__` and `build`.
- Backup files (`*.bak*`), logs and minified files.
- **Data files**: by default only code and docs are read (`.py .js .html .css .md .txt …`), never `.json`
  data, databases, attachments or uploads.
- Files whose names look like keys or passwords (`api_key`, `secret`, `token`, `.env`, `auth.json`, …).
- Anything inside a file that looks like a key, token or password (an `sk-…` key, a GitHub token, a Telegram
  bot token, a Resend key, a private key, or `password = "…"`) is replaced with `[hidden]` before it's searched,
  shown to an AI, or copied to the mirror folder.

## Running it all the time

```bash
mkdir -p ~/.config/systemd/user && cp ask-the-app.service ~/.config/systemd/user/
# edit the path in it if the folder isn't ~/Sandbox/ask-the-app
systemctl --user daemon-reload && systemctl --user enable --now ask-the-app
```

Out of the box it only listens on `127.0.0.1`. Set `"host": "0.0.0.0"` to let other PCs and phones in.
The `allow` list (home network and Tailscale by default) still applies, and only pages on those addresses,
`*.local` or `*.ts.net` (or the ones listed in `origins`) can use the button.

**For an app on an https address** (such as the Tailscale links), the browser blocks a plain-http Ask the App.
Serve it on https too, for example `tailscale serve --bg --https=8795 http://127.0.0.1:8795`, and point
`data-server` at that address.

## Using it from Python

```python
from askapp import config, answer
cfg = config.load()
res = answer.ask(cfg, "ttr", "How do I make a backup?", context={"place": "Home"}, engine="auto")
print(res["answer"], res["show"], res["sources"])
```

That's how a non-web program (like Stream Health's tkinter window) can have an Ask box.

## The answer

`POST /api/ask` → `{job}`; `GET /api/job?id=` → `{state, progress[], result}`. The `result` holds:

| Field | Meaning |
|---|---|
| `answer` | the answer (simple Markdown; `[H2]`/`[C4]` mark which help/code excerpt a fact came from) |
| `show` | the on-screen label of the first button/tab to press (for **Show me**), or blank |
| `sources` | `{ref, kind: help/doc/code, file, start, end, title}` for what it used |
| `engine`, `engine_label`, `seconds`, `cost_usd` | who answered and how long it took |
| `searched`, `skipped` | the extra searches it made; the engines Auto skipped and why |

Every question is logged in `~/.local/state/ask-the-app/questions.jsonl`. The home page lists the recent ones
and marks those the help didn't answer (**no help**), which shows what's missing from the help.

## Checking it works

```bash
python3 selftest.py     # search, secret hiding, answer tidying and the web server, with a stand-in AI
```

## Plans

- **Code Lens**: put the button in its viewer so you can ask what the code you're looking at does. Use
  `AskTheApp.context` to send the selected function, and `onSource` to jump to the lines an answer used.
- **Stream Health**: an Ask tab in the tkinter window (via `answer.ask`) and the button on its web pages.
- **TTR**: the button on every page, with the app's own context (the open tab, the selected job).
