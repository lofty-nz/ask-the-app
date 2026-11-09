#!/usr/bin/env python3
"""Self-test: checks the search, the secret hiding, the answer tidying and the web server, with a stand-in AI.
No network, no models, no real projects needed. Run: python3 selftest.py   (exit code 0 = all passed)
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="askapp-selftest-")
os.environ["ASKAPP_STATE"] = os.path.join(TMP, "state")
os.environ["ASKAPP_CACHE"] = os.path.join(TMP, "cache")

from askapp import answer, config, engines, index  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  ← " + str(detail)))
    if not ok:
        FAILS.append(name)


def write(rel, text):
    path = os.path.join(TMP, "proj", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---- a small pretend program ----
# Fake keys, built from pieces so secret scanners don't mistake this file for a real leak.
FAKE_SK = "sk-" + "live-" + "abcdefghijklmnopqrstuvwx1234"
FAKE_RE = "re_" + "AbCdEf123456" + "GhIjKl789012MnOp"
write("README.md", "# Widget Shop\n\n## Sending an invoice\nOpen the order and press **✉ Email invoice**.\n\n"
                   "## Backups\nBackups run every night at 2am.\n")
write("app.html", "<!doctype html>\n<script>\n" + "\n".join(
    ["function emailInvoice(order){", "  // builds the email and sends it", "  return post('/api/invoice/email', {order});", "}"]
    + ["// filler line %d" % i for i in range(70)]
    + ["function helpPanel(){", "  return sec('start','Getting started','<p>Press <b>➕ New order</b> to begin.</p>')+",
       "  sec('refunds','Refunds','<p>Refunds are made from the <b>💸 Refund</b> button on a paid order.</p>');", "}",
       "const STRIPE_KEY = \"" + FAKE_SK + "\";", "</script>\n"]))
write("server.py", "def save_order(order):\n    '''Save an order to disk.'''\n    pass\n\n"
                   "API_TOKEN = 'abcd1234efgh5678ijkl'\nRESEND = '" + FAKE_RE + "'\n"
                   "re_compile_something_long_name_here = 1\n")
write("secrets/api_key.txt", "super-secret")
write("data.json", '{"customers": ["Alice"]}')
write("node_modules/lib/index.js", "function ignored(){}")
write("old.html.bak-2026", "function oldStuff(){}")

proj = {"root": os.path.join(TMP, "proj"), "name": "Widget Shop",
        "help": ["README.md", {"file": "app.html", "from": "function helpPanel(", "to": "\n}\n",
                               "heading": r"sec\('\w+',\s*'([^']*)'", "title": "Help tab"}]}
cfg = config.load(os.path.join(TMP, "none.json"), {"shop": proj})
cfg["engines"] = {"fake": {"type": "ollama", "label": "Fake", "url": "http://127.0.0.1:9", "model": "x"}}
cfg["auto_order"] = ["fake"]
P = cfg["projects"]["shop"]

# ---- words ----
check("terms split camelCase/snake_case", {"email", "invoic", "emailinvoic"} <= set(index.terms("emailInvoice email_invoices")))
check("stems match", index.stem("invoices") == index.stem("invoice") == index.stem("invoiced"))

# ---- files ----
files, secret = index.list_files(P)
check("indexes code and docs", {"README.md", "app.html", "server.py"} <= set(files), files)
check("skips node_modules, .bak, data .json", not any(f.startswith("node_modules") or ".bak" in f or f.endswith(".json") for f in files), files)
check("skips secret-looking files", "secrets/api_key.txt" not in files and secret == 1, (files, secret))

# ---- secrets ----
red = index.redact(open(os.path.join(TMP, "proj", "server.py")).read() + open(os.path.join(TMP, "proj", "app.html")).read())
check("hides keys and tokens", "abcd1234efgh5678ijkl" not in red and FAKE_SK not in red and FAKE_RE not in red, red[-400:])
check("leaves ordinary names alone", "re_compile_something_long_name_here" in red)

# ---- index + search ----
idx = index.Index.get(P)
s = idx.summary()
check("help from README and from the Help tab code", s["help"] >= 4 and len(s["help_files"]) == 2, s)
titles = [c["title"] for c in idx.chunks if c["kind"] == "help"]
check("Help tab headings come through", any("Refunds" in t for t in titles), titles)
hits = idx.search("how do I email an invoice")
check("search finds the help first", hits and hits[0]["kind"] == "help" and "Email invoice" in hits[0]["text"], [(h["kind"], h["title"]) for h in hits[:3]])
hits = idx.search("emailInvoice", kinds=("code",))
check("search finds code by name", hits and hits[0]["file"] == "app.html" and hits[0]["start"] <= 2, [(h["file"], h["start"]) for h in hits[:3]])
hits = idx.search("refund paid order", kinds=("help",))
check("search finds Help-tab sections", hits and "Refund" in hits[0]["text"], [(h["title"]) for h in hits[:3]])
check("no secrets in the index", not any("abcd1234efgh5678ijkl" in c["text"] for c in idx.chunks))

# ---- mirror for Claude ----
m = idx.mirror()
check("mirror has the code", os.path.exists(os.path.join(m, "app.html")) and os.path.exists(os.path.join(m, "server.py")))
check("mirror has no data or secret files", not os.path.exists(os.path.join(m, "data.json")) and not os.path.exists(os.path.join(m, "secrets")))
check("mirror hides secrets", "abcd1234efgh5678ijkl" not in open(os.path.join(m, "server.py")).read())

# ---- answer tidying ----
a, show, src = answer._split_tail("Press the button [H1].\nSOURCES: app.html:1-4, server.py:2\nSHOW: **✉ Email invoice**")
check("SHOW and SOURCES lines come off", a == "Press the button [H1]." and show == "✉ Email invoice" and src == ["app.html:1-4", "server.py:2"], (a, show, src))

# ---- the whole question, with a stand-in AI ----
calls = []


def fake_chat(c, messages, say=None, **kw):
    calls.append(messages)
    if len(calls) == 1:
        return "SEARCH: save_order disk"
    return "1. Open the order.\n2. Press ✉ Email invoice [H1].\nSHOW: ✉ Email invoice"


engines.ollama_chat = fake_chat
engines.STATUS["ollama"] = lambda c: {"up": True, "note": "fake"}
res = answer.ask(cfg, "shop", "How do I email an invoice?", {"place": "Orders", "buttons": ["✉ Email invoice"]}, "auto")
check("one extra search round", len(calls) == 2 and "More CODE excerpts" in calls[1][-1]["content"], len(calls))
check("answer, show and cited source", res["show"] == "✉ Email invoice" and res["sources"] and res["sources"][0]["ref"] == "H1", res)
check("screen info reaches the AI", "Orders" in calls[0][1]["content"] and "✉ Email invoice" in calls[0][1]["content"])
check("question logged", answer.recent_questions("shop")[0]["q"] == "How do I email an invoice?")

# ---- manual folders (Markdown pages with front matter) ----
write("manual/index.md", "---\ntitle: Shop manual\nstatus: ready\n---\n# Shop manual\n\nThe front page.\n")
write("manual/screens/orders.md", "---\ntitle: Orders screen\nkind: screen\nstatus: draft\n---\n# Orders screen\n\n"
      "The Orders screen lists every order.\n\nAnswers: Where are my orders?\n\n## Refund an order from the Orders screen\n"
      "<!-- hints: orders -->\n- **💸 Refund** `refundOrder`: Gives the money back. Open [the refund page](show:orders#refundOrder).\n\n"
      "```\n# not a heading inside code\n```\n")
write("manual/screens/stock.md", "---\ntitle: Stock\nstatus: planned\noldhelp: start\n---\n# Stock\n\nNot written yet. UNIQUEPLACEHOLDERWORD\n")
mp = dict(proj, help=["manual/**/*.md"], engines={"fake": {"help_chars": 1234}})
mcfg = config.load(os.path.join(TMP, "none.json"), {"shopm": mp})
M = mcfg["projects"]["shopm"]
items = index._help_items(M)
check("help patterns expand to each page", sorted(i["file"] for i in items) == ["manual/index.md", "manual/screens/orders.md", "manual/screens/stock.md"], items)
midx = index.Index.get(M)
mchunks = [c for c in midx.chunks if c["file"].startswith("manual/")]
check("placeholder pages are skipped", not any("UNIQUEPLACEHOLDERWORD" in c["text"] for c in midx.chunks))
check("front matter is left out of the text", not any("kind: screen" in c["text"] for c in mchunks))
check("chunk titles use the page title", any(c["title"] == "Orders screen › Refund an order from the Orders screen" for c in mchunks), [c["title"] for c in mchunks])
check("line numbers still match the file", any(c["start"] == 12 and "Refund an order" in c["text"] for c in mchunks), [(c["start"], c["title"]) for c in mchunks])
check("comments and app/show links become plain words", not any("hints:" in c["text"] or "show:" in c["text"] for c in mchunks)
      and any("Open the refund page." in c["text"] for c in mchunks))
check("a # line inside a code block isn't a heading", not any(c["title"].endswith("not a heading inside code") for c in mchunks))
hits = midx.search("refund an order", kinds=("help",))
check("manual pages are found as help", hits and hits[0]["file"] == "manual/screens/orders.md", [(h["file"], h["title"]) for h in hits[:3]])
pinned = answer.pin_screen_page(midx, "front page", {"manual": "manual/screens/orders.md"}, midx.search("front page", kinds=("help",)), 9000)
check("the current screen's page is put first", pinned and pinned[0]["file"] == "manual/screens/orders.md", [(h["file"], h["title"]) for h in pinned[:3]])
check("per-project engine settings exist", M["engines"]["fake"]["help_chars"] == 1234)

# ---- the web server ----
import serve  # noqa: E402

calls.clear()
cfg["port"] = 0
serve.setup(cfg)
srv = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]


def http(path, body=None, headers=None):
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                 dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


st, h, body = http("/ask.js")
check("serves ask.js", st == 200 and b"AskTheApp" in body and "javascript" in h.get("Content-Type", ""))
st, h, body = http("/")
check("serves the page", st == 200 and b"Ask the App" in body)
st, h, body = http("/api/projects?summary=1")
check("lists projects", st == 200 and json.loads(body)["projects"][0]["id"] == "shop")
st, h, body = http("/api/ask", {"project": "shop", "question": "How do I email an invoice?"}, {"Origin": "http://192.168.1.20:8766"})
check("home-network page may ask, CORS header set", st == 200 and h.get("Access-Control-Allow-Origin") == "http://192.168.1.20:8766", (st, body))
job = json.loads(body)["job"]
for _ in range(50):
    st, h, body = http("/api/job?id=" + job)
    if json.loads(body)["state"] != "running":
        break
    time.sleep(0.1)
check("job finishes with an answer", json.loads(body).get("result", {}).get("show") == "✉ Email invoice", body[:300])
st, h, body = http("/api/ask", {"project": "shop", "question": "hi"}, {"Origin": "https://evil.example.com"})
check("other websites are refused", st == 403)
st, h, body = http("/api/ask", {"project": "nope", "question": "hi"})
check("unknown project is a clear error", st == 400 and b"no project" in body)
st, h, body = http("/api/transcribe", {})
check("speech to text off says so", st == 400 and b"isn't set up" in body)
srv.shutdown()

print("\n%s" % ("ALL PASSED" if not FAILS else "%d FAILED: %s" % (len(FAILS), ", ".join(FAILS))))
sys.exit(1 if FAILS else 0)
