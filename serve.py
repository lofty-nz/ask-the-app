#!/usr/bin/env python3
"""Ask the App web server: the "ask a question" page, the drop-in button script, and its API.

    python3 serve.py                          # projects from ~/.config/ask-the-app/config.json
    python3 serve.py --root /path/to/program  # quick start: one project, no config needed
    python3 serve.py --config my.json --port 8795

Then open http://127.0.0.1:8795/ or add the button to a web app:
    <script src="http://127.0.0.1:8795/ask.js" data-project="myapp"></script>

API (JSON unless noted):
    GET  /api/projects                 the projects, each with its index summary
    GET  /api/status                   which AIs are on, and whether speech-to-text is set up
    POST /api/ask {project, question, context, engine}  -> {job}
    GET  /api/job?id=…                 {state: running|done|error, progress: [...], result | error}
    POST /api/transcribe   (body = the recording)          -> {text}
    GET  /api/recent?project=…         the latest questions asked
Standard library only.
"""
import argparse
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from askapp import __version__, answer, config, engines  # noqa: E402
from askapp.index import Index  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
MAX_AUDIO = 25 * 1024 * 1024
CFG = {}
JOBS, JOBS_LOCK = {}, threading.Lock()
_STATUS = {"t": 0, "data": None}


def engine_status():
    if time.monotonic() - _STATUS["t"] > 8 or _STATUS["data"] is None:
        _STATUS["data"] = engines.status(CFG)
        _STATUS["t"] = time.monotonic()
    return _STATUS["data"]


def run_job(job, req):
    def say(text):
        with JOBS_LOCK:
            job["progress"].append(text)

    try:
        res = answer.ask(CFG, req.get("project"), req.get("question"), req.get("context"), req.get("engine") or CFG["engine"], say)
        with JOBS_LOCK:
            job.update(state="done", result=res)
    except (ValueError, engines.EngineError) as e:
        with JOBS_LOCK:
            job.update(state="error", error=str(e))
    except Exception as e:   # keep the server up whatever happens, and say what broke
        with JOBS_LOCK:
            job.update(state="error", error="Something went wrong: %s: %s" % (type(e).__name__, e))
    finally:
        _STATUS["t"] = 0   # an answer can change what's busy


def prune_jobs():
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if time.monotonic() - v["t0"] > 1800]:
            JOBS.pop(jid, None)


def transcribe(audio, ctype):
    url = CFG.get("transcribe_url")
    if not url:
        raise ValueError("Speech to text isn't set up (transcribe_url in the config)")
    if not audio:
        raise ValueError("No sound was recorded")
    req = urllib.request.Request(url, audio, {"Content-Type": ctype or "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            out = json.load(e)
        except ValueError:
            raise ValueError("Speech to text failed (%s)" % e.code)
    except (OSError, ValueError) as e:
        raise ValueError("Couldn't reach speech to text: %s" % e)
    if not out.get("ok", True) or "text" not in out:
        raise ValueError(out.get("error") or "Speech to text gave no text")
    return out["text"]


class Handler(BaseHTTPRequestHandler):
    server_version = "AskTheApp/" + __version__

    def log_message(self, fmt, *args):
        pass

    # ---- who may use it ----
    def allowed(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if any(ip in net for net in CFG["allow_nets"]):
            return True
        self.send_json({"error": "Not allowed from this address"}, 403)
        return False

    def origin_ok(self):
        """Pages on this PC, the home network, *.local or the tailnet may use the button; other websites may not."""
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return origin is None
        if origin.rstrip("/") in CFG["origins"]:
            return True
        host = (urllib.parse.urlsplit(origin).hostname or "").lower()
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".ts.net"):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in net for net in CFG["allow_nets"])

    def cors(self):
        origin = self.headers.get("Origin")
        if origin and self.origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            if self.headers.get("Access-Control-Request-Private-Network"):
                self.send_header("Access-Control-Allow-Private-Network", "true")

    # ---- replies ----
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, name, ctype):
        try:
            with open(os.path.join(WEB, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_json({"error": "Not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")   # the button script itself has nothing private in it
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self, limit=200_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes ----
    def do_OPTIONS(self):
        if not self.allowed():
            return
        if not self.origin_ok():
            self.send_json({"error": "Not allowed from that page"}, 403)
            return
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if not self.allowed():
            return
        u = urllib.parse.urlsplit(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                self.send_file("index.html", "text/html; charset=utf-8")
            elif u.path == "/ask.js":
                self.send_file("ask.js", "text/javascript; charset=utf-8")
            elif u.path == "/icon.svg":
                self.send_file("icon.svg", "image/svg+xml")
            elif u.path == "/api/projects":
                out = []
                for pid, p in CFG["projects"].items():
                    item = {"id": pid, "name": p["name"], "about": p["about"], "examples": p["examples"]}
                    if qs.get("summary"):
                        item["index"] = Index.get(p).summary()
                    out.append(item)
                self.send_json({"projects": out, "default": next(iter(CFG["projects"]), "")})
            elif u.path == "/api/status":
                self.send_json({"engines": engine_status(), "auto_order": CFG["auto_order"], "engine": CFG["engine"],
                                "speech": bool(CFG.get("transcribe_url")), "version": __version__})
            elif u.path == "/api/job":
                with JOBS_LOCK:
                    job = JOBS.get(qs.get("id", ""))
                    snap = json.loads(json.dumps(job)) if job else None
                if not snap:
                    self.send_json({"error": "That question has expired; ask it again"}, 404)
                else:
                    snap.pop("t0", None)
                    self.send_json(snap)
            elif u.path == "/api/recent":
                self.send_json({"questions": answer.recent_questions(qs.get("project"), int(qs.get("limit") or 30))})
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        if not self.allowed():
            return
        if not self.origin_ok():
            self.send_json({"error": "Not allowed from that page"}, 403)
            return
        try:
            if self.path == "/api/transcribe":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_AUDIO:
                    raise ValueError("That recording is too long")
                self.send_json({"ok": True, "text": transcribe(self.rfile.read(n), self.headers.get("Content-Type"))})
            elif self.path == "/api/ask":
                req = self.body_json()
                if req.get("project") not in CFG["projects"]:
                    raise ValueError("There's no project called %r here" % req.get("project"))
                prune_jobs()
                job = {"id": uuid.uuid4().hex[:12], "state": "running", "progress": [], "t0": time.monotonic()}
                with JOBS_LOCK:
                    JOBS[job["id"]] = job
                threading.Thread(target=run_job, args=(job, req), daemon=True).start()
                self.send_json({"ok": True, "job": job["id"]})
            else:
                self.send_json({"error": "Not found"}, 404)
        except ValueError as e:
            self.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)


def setup(cfg):
    CFG.clear()
    CFG.update(cfg)
    CFG["allow_nets"] = [ipaddress.ip_network(n) for n in cfg["allow"]]
    CFG["origins"] = [o.rstrip("/") for o in cfg.get("origins") or []]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ask the App: answers questions about a program from its help and code.")
    ap.add_argument("--config", help="settings file (default %s)" % config.CONFIG_PATH)
    ap.add_argument("--root", help="quick start: answer questions about this one folder")
    ap.add_argument("--name", help="with --root: the program's name")
    ap.add_argument("--help-file", action="append", default=[], help="with --root: a help file (repeat for more)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)
    extra = {}
    if a.root:
        root = os.path.abspath(os.path.expanduser(a.root))
        pid = os.path.basename(root.rstrip("/")).lower().replace(" ", "-") or "project"
        helps = a.help_file or [f for f in ("README.md", "HELP.md", "help.html", "docs/README.md") if os.path.exists(os.path.join(root, f))]
        extra[pid] = {"root": root, "name": a.name or os.path.basename(root), "help": helps}
    cfg = config.load(a.config, extra)
    if a.host:
        cfg["host"] = a.host
    if a.port:
        cfg["port"] = a.port
    if extra:   # --root puts its project first
        cfg["projects"] = dict(sorted(cfg["projects"].items(), key=lambda kv: kv[0] not in extra))
    if not cfg["projects"]:
        sys.exit("No projects: pass --root /path/to/program or add projects to %s (see config.example.json)" % cfg["config_path"])
    setup(cfg)
    srv = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    srv.daemon_threads = True
    print("Ask the App %s on http://%s:%d/  projects: %s" % (__version__, cfg["host"], cfg["port"], ", ".join(cfg["projects"])), flush=True)
    for pid, p in cfg["projects"].items():   # build the indexes now so the first question is quick
        threading.Thread(target=Index.get, args=(p,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
