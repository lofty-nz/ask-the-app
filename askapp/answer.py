"""Turning a question into an answer: search, ask an AI, tidy up the reply.

For the local AIs (Ollama, Strata) this program does the searching: the best help sections and code pieces
go into the prompt, numbered [H1].. and [C1].., and the AI may ask for ONE more search ("SEARCH: words")
before it answers. The Claude engine gets the best help sections as a head start and then searches the
code itself.

Every answer can end with "SHOW: <label>", the on-screen button or tab to press first; the button script
uses it for its "Show me" highlight.
"""
import collections
import json
import os
import re
import time

from . import config, engines
from .index import Index, terms

RULES = """Rules:
- Use only the HELP, CODE and SCREEN information given{own_search}. Never invent buttons, tabs, settings or steps.
- Write for someone who is not a programmer: short plain sentences, numbered steps when there are steps.
  Name buttons, tabs and menus exactly as the person sees them (the text on the button, emoji included).
- Don't talk about code, functions or file names unless the question is about the code itself.
- If the information doesn't answer the question, say so plainly, then give the nearest thing you did find.
- Keep it short: under about 150 words unless the steps need more.
- Last line, only if there is one button, tab or link to press first: SHOW: <its exact on-screen text>"""

LOCAL_SYSTEM = """You are "Ask the App", the built-in helper for the program "{name}".{about}
You answer questions from the person using it: how to do something, where something is, or why something happened.
{rules}
- After a fact taken from an excerpt, cite it like [H2] or [C4]."""

CLAUDE_SYSTEM = """You are "Ask the App", the built-in helper for the program "{name}".{about}
You answer questions from the person using it: how to do something, where something is, or why something happened.
The current folder holds the program's code and help files (a read-only copy; secrets are hidden). Search it with
Grep, Glob and Read to find the answer: look for the words the person sees on screen (button labels, tab names)
first, then follow the code that handles them. Be quick: usually 2 to 8 searches are enough. Never guess.
{rules}
- The line before SHOW (or the last line if there is no SHOW): SOURCES: the files and line numbers you used, e.g.
  SOURCES: app.html:120, server.py:88"""

SEARCH_HINT = ("\nIf these excerpts are clearly not enough, you may instead reply with ONE line only:\n"
               "SEARCH: <up to 8 words or code names to look for>\nand you will get more excerpts.")


def _about(proj):
    return (" " + proj["about"].strip()) if proj.get("about") else ""


def context_text(ctx, limit=2500):
    """The screen information from the button script, as short readable lines."""
    if not ctx:
        return "(not given)"
    lines = []
    for key, label in (("title", "Page title"), ("place", "Where they are"), ("url", "Address"), ("active", "Selected tabs/items"),
                       ("headings", "Headings on screen"), ("buttons", "Buttons and links on screen"),
                       ("focus", "Field they are in"), ("selection", "Text they selected")):
        v = ctx.get(key)
        if not v:
            continue
        if isinstance(v, list):
            v = " · ".join(str(x) for x in v)
        lines.append("%s: %s" % (label, str(v)[:900]))
    if ctx.get("app"):
        try:
            lines.append("From the app: " + json.dumps(ctx["app"], ensure_ascii=False)[:900])
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)[:limit] or "(not given)"


def _query(question, ctx, extra=""):
    q = collections.Counter()
    for t in terms(question):
        q[t] += 1.0
    for t in terms(extra):
        q[t] += 1.0
    for t in terms(" ".join(str(x) for x in ((ctx or {}).get("place"), (ctx or {}).get("active")) if x)):
        q[t] += 0.4
    return q


def _excerpts(hits, prefix, start=1):
    out, refs = [], []
    for n, h in enumerate(hits, start):
        ref = "%s%d" % (prefix, n)
        where = h["title"] if h["kind"] in ("help", "doc") else "%s lines %d-%d%s" % (
            h["file"], h["start"], h["end"], " (%s)" % h["title"] if h["title"] else "")
        out.append("[%s] %s\n%s" % (ref, where, h["text"].strip()))
        refs.append({"ref": ref, "kind": h["kind"], "file": h["file"], "start": h["start"], "end": h["end"], "title": h["title"]})
    return "\n\n".join(out), refs


def _split_tail(text):
    """Pull SHOW: and SOURCES: lines off the answer."""
    show, sources, keep = "", [], []
    for line in text.strip().splitlines():
        m = re.match(r"^\s*\**\s*SHOW\s*:\**\s*(.+?)\s*$", line, re.I)
        if m:
            show = m.group(1).strip().strip("*`\"'“”‘’<>").strip()
            continue
        m = re.match(r"^\s*\**\s*SOURCES?\s*:\**\s*(.+?)\s*$", line, re.I)
        if m:
            sources = [s.strip(" `*") for s in re.split(r",\s*|;\s*", m.group(1)) if s.strip(" `*")]
            continue
        keep.append(line)
    answer = "\n".join(keep).strip()
    if show.lower() in ("none", "n/a", "-", "nothing"):
        show = ""
    return answer, show[:80], sources[:12]


def _local(cfg, proj, idx, eid, c, question, ctx, say):
    call = engines.ollama_chat if (c.get("type") or eid) == "ollama" else engines.strata_chat
    help_chars, code_chars = int(c.get("help_chars", 9000)), int(c.get("code_chars", 14000))
    say("Searching the help and the code…")
    q = _query(question, ctx)
    help_hits = idx.search(q, ("help", "doc"), limit=12, chars=help_chars)
    code_hits = idx.search(q, ("code",), limit=14, chars=code_chars)
    help_txt, help_refs = _excerpts(help_hits, "H")
    code_txt, code_refs = _excerpts(code_hits, "C")
    system = LOCAL_SYSTEM.format(name=proj["name"], about=_about(proj), rules=RULES.format(own_search=""))
    user = ("QUESTION: %s\n\nSCREEN (what the person is looking at now):\n%s\n\nHELP excerpts:\n%s\n\nCODE excerpts:\n%s\n%s"
            % (question, context_text(ctx), help_txt or "(none found)", code_txt or "(none found)", SEARCH_HINT))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    text = call(c, messages, say)
    searched = []
    m = re.match(r"^\s*SEARCH\s*:\s*(.+)$", text.strip().splitlines()[0] if text.strip() else "", re.I)
    if m:
        searched = [m.group(1).strip()[:200]]
        say("Looking for more: %s" % searched[0])
        q2 = _query(question, ctx, extra=m.group(1) + " " + m.group(1))
        given = {h["i"] for h in help_hits + code_hits}
        more_help = idx.search(q2, ("help", "doc"), limit=6, chars=help_chars // 2, skip=given)
        more_code = idx.search(q2, ("code",), limit=8, chars=code_chars // 2, skip=given)
        mh_txt, mh_refs = _excerpts(more_help, "H", len(help_refs) + 1)
        mc_txt, mc_refs = _excerpts(more_code, "C", len(code_refs) + 1)
        help_refs += mh_refs
        code_refs += mc_refs
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "More HELP excerpts:\n%s\n\nMore CODE excerpts:\n%s\n\nNow answer the question "
                      "(no more searches)." % (mh_txt or "(none found)", mc_txt or "(none found)")}]
        text = call(c, messages, say)
        if re.match(r"^\s*SEARCH\s*:", text.strip()):
            text = "I couldn't find enough about that in the help or the code to answer it for sure."
    answer, show, _ = _split_tail(text)
    cited = set(re.findall(r"\[([HC]\d+)\]", answer))
    refs = help_refs + code_refs
    return {"answer": answer, "show": show, "sources": [r for r in refs if r["ref"] in cited],
            "looked_at": len(refs), "searched": searched}


def _claude(cfg, proj, idx, eid, c, question, ctx, say):
    say("Copying the code where Claude can read it (secrets hidden)…")
    cwd = idx.mirror()
    q = _query(question, ctx)
    help_hits = idx.search(q, ("help", "doc"), limit=8, chars=int(c.get("help_chars", 8000)))
    help_txt, help_refs = _excerpts(help_hits, "H")
    help_files = ", ".join("%s%s" % (h["file"], " (%s, from line %d)" % (h["title"], h["line"]) if h["title"] != h["file"] else "")
                           for h in idx.help_files) or "none listed"
    system = CLAUDE_SYSTEM.format(name=proj["name"], about=_about(proj), rules=RULES.format(own_search=", and what you find in the files"))
    prompt = ("QUESTION: %s\n\nSCREEN (what the person is looking at now):\n%s\n\nHelp files: %s\n\n"
              "Help sections that may be relevant (cite as [H1] etc. if you use them):\n%s"
              % (question, context_text(ctx), help_files, help_txt or "(none found)"))
    text, info = engines.claude_run(c, prompt, system, cwd, say)
    answer, show, sources = _split_tail(text)
    cited = set(re.findall(r"\[(H\d+)\]", answer + " " + " ".join(sources)))
    srcs = [r for r in help_refs if r["ref"] in cited]
    for s in sources:
        m = re.match(r"^([\w./ -]+\.\w+)(?::(\d+)(?:-(\d+))?)?$", s)
        if m:
            srcs.append({"ref": "", "kind": "code", "file": m.group(1), "start": int(m.group(2) or 0),
                         "end": int(m.group(3) or m.group(2) or 0), "title": ""})
    return {"answer": answer, "show": show, "sources": srcs, "looked_at": len(info["tools"]),
            "searched": info["tools"], "cost_usd": info.get("cost_usd")}


def engine_order(cfg, engine):
    if engine and engine != "auto":
        if engine not in cfg["engines"]:
            raise engines.EngineError("There's no engine called %r" % engine)
        return [engine]
    order = [e for e in cfg.get("auto_order") or [] if e in cfg["engines"]]
    return order or list(cfg["engines"])


def log_question(rec):
    try:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(os.path.join(config.STATE_DIR, "questions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_questions(project=None, limit=30):
    try:
        with open(os.path.join(config.STATE_DIR, "questions.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    rows = [r for r in rows if not project or r.get("project") == project]
    return rows[-limit:][::-1]


def ask(cfg, pid, question, context=None, engine="auto", say=None):
    """Answer one question about project pid. Returns a dict for the button script (see README "The answer")."""
    say = say or (lambda text: None)
    question = " ".join(str(question or "").split())[:2000]
    if not question:
        raise ValueError("Ask a question first")
    proj = cfg["projects"].get(pid)
    if not proj:
        raise ValueError("There's no project called %r here" % pid)
    t0 = time.monotonic()
    idx = Index.get(proj)
    tried, out, eid = [], None, None
    order = engine_order(cfg, engine)
    for n, eid in enumerate(order):
        c = dict(cfg["engines"][eid])
        kind = c.get("type") or eid
        if len(order) > 1:   # Auto: skip what's off without waiting on it
            st = engines.status({"engines": {eid: c}}).get(eid, {})
            if not st.get("up"):
                tried.append("%s: %s" % (c.get("label", eid), st.get("note", "off")))
                continue
        try:
            if kind == "claude":
                out = _claude(cfg, proj, idx, eid, c, question, context, say)
            elif kind in ("ollama", "strata"):
                out = _local(cfg, proj, idx, eid, c, question, context, say)
            else:
                raise engines.EngineError("Unknown engine type %r" % kind)
            break
        except engines.EngineError as e:
            tried.append("%s: %s" % (c.get("label", eid), e))
            if n + 1 < len(order):
                say("%s couldn't answer (%s); trying the next one…" % (c.get("label", eid), e))
    if out is None:
        raise engines.EngineError("No AI could answer. " + "; ".join(tried))
    out.update(engine=eid, engine_label=cfg["engines"][eid].get("label", eid), seconds=round(time.monotonic() - t0, 1),
               skipped=tried, project=pid, question=question)
    log_question({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "project": pid, "q": question, "engine": eid,
                  "seconds": out["seconds"], "from_help": any(s["kind"] == "help" for s in out["sources"]),
                  "answer": out["answer"][:600], "show": out["show"], "cost_usd": out.get("cost_usd")})
    return out
