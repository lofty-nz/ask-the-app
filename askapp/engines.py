"""The AIs that write the answers.

- ollama: a local model through Ollama (qwen3:8b by default). Always there, quick, weakest at code.
- strata: the big local model on the main PC (an OpenAI-style API). Used only when it is already on, never
  woken or started from here. It answers ONE chat at a time, so its /status is checked and a busy Strata
  is waited for (busy_wait_s), never interrupted.
- claude: the `claude` command line (your Claude subscription, no API key needed). It searches the code
  itself, read-only: only the Read, Grep and Glob tools exist for it, and it runs inside a mirror folder that
  holds just the indexed files with secrets hidden (index.Index.mirror).

Each chat function takes messages / a prompt and a say(text) callback for progress lines, and returns the
answer text. They raise EngineError (or Busy) when they can't answer, so "Auto" can try the next one.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request


class EngineError(Exception):
    pass


class Busy(EngineError):
    pass


def _http(url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict({"Content-Type": "application/json"} if data else {}, **(headers or {}))
    req = urllib.request.Request(url, data, hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _no_think(text):
    return re.sub(r"(?s)<think>.*?</think>", "", text or "").strip()


# ---------------------------------------------------------------- ollama ----

def ollama_status(c):
    try:
        names = [m.get("name") for m in _http(c["url"].rstrip("/") + "/api/tags", timeout=3).get("models", [])]
    except (OSError, ValueError) as e:
        return {"up": False, "note": "not reachable (%s)" % (getattr(e, "reason", None) or e)}
    if c["model"] not in names and c["model"] + ":latest" not in names:
        return {"up": False, "note": "%s isn't installed there" % c["model"]}
    return {"up": True, "note": c["model"]}


def ollama_chat(c, messages, say=None, timeout=600):
    if say:
        say("%s is writing the answer…" % c.get("label", "Ollama"))
    body = {"model": c["model"], "messages": messages, "stream": False, "think": False, "keep_alive": "10m",
            "options": {"temperature": 0.2, "num_ctx": int(c.get("num_ctx") or 8192)}}
    try:
        r = _http(c["url"].rstrip("/") + "/api/chat", body, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise EngineError("Ollama returned an error (%s): %s" % (e.code, e.read()[:300].decode(errors="replace")))
    except (OSError, ValueError) as e:
        raise EngineError("Ollama didn't answer: %s" % e)
    text = _no_think((r.get("message") or {}).get("content"))
    if not text:
        raise EngineError("Ollama's answer was empty")
    return text


# ---------------------------------------------------------------- strata ----

_STRATA_TURN = threading.Lock()   # this program's own Strata questions go one at a time too


def _strata_key(c):
    try:
        with open(os.path.expanduser(c.get("api_key_file") or "")) as f:
            return f.read().strip()
    except OSError:
        return c.get("api_key") or ""


def _strata_auth(c):
    k = _strata_key(c)
    return {"Authorization": "Bearer " + k} if k else {}


def strata_status(c):
    base = c["base_url"].rstrip("/")
    try:
        models = [m.get("id") for m in _http(base + "/models", headers=_strata_auth(c), timeout=3).get("data", [])]
    except urllib.error.HTTPError as e:
        return {"up": False, "note": "refused the key (%s)" % e.code if e.code in (401, 403) else "error %s" % e.code}
    except (OSError, ValueError):
        return {"up": False, "note": "off"}
    st = {}
    try:
        st = _http(re.sub(r"/v1$", "", base) + "/status", headers=_strata_auth(c), timeout=3)
    except (OSError, ValueError):
        pass
    busy = bool(isinstance(st, dict) and (st.get("busy") or st.get("queued")))
    return {"up": True, "busy": busy, "note": ("busy: %s" % (st.get("phase") or "another chat")) if busy else (c.get("model") or (models[0] if models else "")),
            "model": c.get("model") or (models[0] if models else "")}


def _strata_wait_free(c, say, max_wait):
    t0, said = time.monotonic(), False
    while True:
        st = strata_status(c)
        if not st["up"]:
            raise EngineError("Strata is off (%s)" % st["note"])
        if not st.get("busy"):
            return st
        if time.monotonic() - t0 > max_wait:
            raise Busy("Strata has been busy with another chat for %d seconds" % max_wait)
        if say and not said:
            say("Strata is busy with another chat; waiting for it to finish…")
            said = True
        time.sleep(3)


def strata_chat(c, messages, say=None, max_wait=None):
    limit = c.get("busy_wait_s", 60) if max_wait is None else max_wait
    t0 = time.monotonic()
    if not _STRATA_TURN.acquire(timeout=limit):
        raise Busy("another question to Strata from here is still being answered")
    try:
        st = _strata_wait_free(c, say, max(0, limit - (time.monotonic() - t0)))
        if say:
            say("Strata is writing the answer…")
        body = {"model": st.get("model") or "", "messages": messages, "temperature": 0.2, "max_tokens": 3000}
        try:
            r = _http(c["base_url"].rstrip("/") + "/chat/completions", body, _strata_auth(c), timeout=900)
        except urllib.error.HTTPError as e:
            raise EngineError("Strata returned an error (%s): %s" % (e.code, e.read()[:300].decode(errors="replace")))
        except (OSError, ValueError) as e:
            raise EngineError("Strata stopped answering: %s" % e)
        try:
            text = _no_think(r["choices"][0]["message"].get("content"))
        except (KeyError, IndexError, TypeError):
            text = ""
        if not text:
            raise EngineError("Strata's answer was empty")
        return text
    finally:
        _STRATA_TURN.release()


# ---------------------------------------------------------------- claude ----

def claude_bin(c):
    if c.get("bin"):
        return os.path.expanduser(c["bin"])
    found = shutil.which("claude") or (os.path.expanduser("~/.local/bin/claude") if os.access(os.path.expanduser("~/.local/bin/claude"), os.X_OK) else "")
    if found:
        return found
    ext = sorted(glob.glob(os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
                 key=os.path.getmtime)
    return ext[-1] if ext else ""


def claude_status(c):
    b = claude_bin(c)
    return {"up": bool(b), "note": (c.get("model") or "default model") if b else "the claude command isn't installed here"}


def _clean_env():
    """This program may itself be started from inside a Claude session; the child must not think it's nested."""
    env = dict(os.environ)
    for k in list(env):
        if k == "CLAUDECODE" or re.match(r"CLAUDE_CODE_(ENTRYPOINT|SSE_PORT|MESSAGING|SESSION|IDE|EXECPATH)", k):
            env.pop(k)
    return env


def _tool_note(name, inp):
    inp = inp or {}
    if name == "Grep":
        return "Searching the code for “%s”" % str(inp.get("pattern", ""))[:60]
    if name == "Glob":
        return "Looking for files: %s" % str(inp.get("pattern", ""))[:60]
    if name == "Read":
        f = os.path.basename(str(inp.get("file_path", "")))
        off = inp.get("offset")
        return "Reading %s%s" % (f, " from line %s" % off if off else "")
    return "Using %s" % name


def claude_run(c, prompt, system, cwd, say=None):
    """Run `claude -p` read-only in cwd. Returns (answer, info) where info has cost_usd, turns, tools."""
    exe = claude_bin(c)
    if not exe:
        raise EngineError("The claude command isn't installed on this PC")
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
            "--strict-mcp-config", "--no-session-persistence", "--append-system-prompt", system,
            "--tools", "Read,Grep,Glob"]
    if c.get("model"):
        argv += ["--model", c["model"]]
    if say:
        say("Claude is reading the question…")
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=_clean_env(), text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise EngineError("Couldn't start claude: %s" % e)
    timer = threading.Timer(float(c.get("timeout_s") or 300), p.kill)
    timer.start()
    result, tools, err_lines = None, [], []
    try:
        p.stdin.write(prompt)
        p.stdin.close()
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        note = _tool_note(block.get("name"), block.get("input"))
                        tools.append(note)
                        if say:
                            say(note)
            elif ev.get("type") == "result":
                result = ev
        p.wait()
        err_lines = (p.stderr.read() or "").strip().splitlines()
    finally:
        timer.cancel()
    if result is None:
        if p.returncode and p.returncode < 0:
            raise EngineError("Claude took too long (over %s seconds)" % c.get("timeout_s"))
        raise EngineError("Claude didn't answer: %s" % ((err_lines or ["no output"])[-1][:300]))
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        raise EngineError("Claude stopped: %s" % str(result.get("result") or result.get("subtype"))[:300])
    return (result.get("result") or "").strip(), {"cost_usd": result.get("total_cost_usd"), "turns": result.get("num_turns"),
                                                  "tools": tools}


# ---------------------------------------------------------------- all ----

STATUS = {"ollama": ollama_status, "strata": strata_status, "claude": claude_status}


def status(cfg):
    """{engine id: {"up", "note", "label"}} for every configured engine."""
    out = {}
    for eid, c in cfg["engines"].items():
        fn = STATUS.get(c.get("type") or eid)
        if not fn:
            continue
        try:
            st = fn(c)
        except Exception as e:   # a status check must never break the page
            st = {"up": False, "note": str(e)[:200]}
        out[eid] = dict(st, label=c.get("label") or eid)
    return out
