"""Finding the bits of help and code that answer a question.

Every project is read into small pieces ("chunks"): help sections, and runs of code that start at a
function or heading where possible. A question is matched against them with BM25, the classic
search-engine scoring, using words split the way programmers write them (sendEstimate, send_estimate →
send, estimate). Nothing here needs a model or an internet connection, so search is instant.

Secrets stay out: files whose names look like keys or passwords are skipped (unless they are code), and
anything that looks like a key or token inside a file is replaced with [hidden] before it is searched,
shown to an AI, or copied to the mirror folder the Claude engine reads.
"""
import collections
import fnmatch
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import threading
import time

from . import config

CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".sh", ".c", ".cc",
             ".cpp", ".h", ".hpp", ".ino", ".gs", ".lua", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
             ".vue", ".svelte"}
DOC_EXTS = {".md", ".txt", ".rst"}
DEFAULT_INCLUDE = ["*" + e for e in sorted(CODE_EXTS | DOC_EXTS)]
# A pattern with no "/" is matched against the file name and every folder name; with a "/", the whole path.
DEFAULT_EXCLUDE = [
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "site-packages", ".pio", ".next", "dist",
    "build", ".cache", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", ".claude", "backups", "*.bak*",
    "*.orig", "*.rej", "*~", "*.min.js", "*.min.css", "*.map", "*.log", "*.lock",
]
SECRET_NAME = re.compile(r"(?i)secret|passw|credential|api[_-]?key|private[_-]?key|(^|[._-])env$|^\.env|"
                         r"\.pem$|\.key$|id_rsa|id_ed25519|token|auth\.json")

_SECRET_RES = [
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),          # Telegram bot token
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),                   # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
_RESEND = re.compile(r"\bre_[A-Za-z0-9_]{24,}\b")              # Resend keys; Python names like re_long_name don't count
# NAME = "value" where the name mentions a key, token, secret or password (paths and web addresses are left alone)
_ASSIGNED = re.compile(r"""(?i)\b(\w*(?:api[_-]?key|apikey|secret|passw(?:or)?d|token|bearer)\w*["']?\s*[:=]\s*["'])"""
                       r"""(?![~/.]|https?:)([^"'\s]{12,})(["'])""")


def redact(text):
    """The text with anything that looks like a key, token or password replaced by [hidden]."""
    for rx in _SECRET_RES:
        text = rx.sub("[hidden]", text)
    text = _RESEND.sub(lambda m: "[hidden]" if (re.search(r"\d", m.group()) and re.search(r"[A-Z]", m.group())) else m.group(), text)
    return _ASSIGNED.sub(lambda m: m.group(1) + "[hidden]" + m.group(3), text)


# ---------------------------------------------------------------- words ----

STOP = set("""a an the and or but if then else of to in on at by for from with without into onto about as is are was were be
been being am do does did doing done have has had having i me my mine we us our you your yours he she it its they them
their this that these those there here what which who whom whose when where why how can could should would will shall
may might must not no yes so too very just also only than any all some each every more most much many other another
such own same get got gets please thanks thank want wants need needs know tell show let lets like make into out up down
over under again once one two way thing things something anything app program
function return const var let def self true false none null undefined new class div span px style px em important
import export elif endif else void int""".split())
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def stem(w):
    """A rough English stem, the same for "invoice", "invoices" and "invoiced"."""
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and (w.endswith("ches") or w.endswith("shes") or w.endswith("sses") or w.endswith("xes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def terms(text):
    """Search words in text. A name like sendEstimate gives sendestimate, send and estimate."""
    out = []
    for ident in _IDENT.findall(text):
        parts = [p.lower() for piece in ident.split("_") for p in _CAMEL.findall(piece) if p]
        low = ident.lower()
        if len(parts) > 1 and len(low) <= 40:
            out.append(low.replace("_", ""))
        out.extend(parts)
    return [stem(t) for t in out if len(t) > 1 and t not in STOP]


# ---------------------------------------------------------------- chunks ----

MAX_LINES, MAX_CHARS, MIN_LINES = 60, 3200, 8
_BOUNDARY = re.compile(
    r"^\s*(?:async\s+)?(?:def|class)\s+\w"
    r"|^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*[\w$]"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)"
    r"|^#{1,6}\s"
    r"|^\s*<(?:script|style|section|h[1-3])\b"
    r"|^\s*(?:static\s+)?(?:void|int|bool|float|double|char|String|auto)\s+[\w:]+\s*\([^;]*$")
_SYMBOL = re.compile(r"(?:def|class|function)\s*\*?\s*([A-Za-z_$][\w$]*)"
                     r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\(|[\w$]+\s*=>)"
                     r"|^#{1,6}\s+(.+)")


def _symbol(line):
    m = _SYMBOL.search(line)
    name = next((g for g in m.groups() if g), "").strip()[:80] if m else ""
    return name if re.search(r"[A-Za-z]", name) else ""


def chunk_code(text):
    """(start line, end line, text, title) pieces of a code file, cut at functions/headings where possible."""
    pieces, buf, start, size, title, last_sym = [], [], 1, 0, "", ""

    def flush(end):
        nonlocal buf, size, title
        if buf and "".join(buf).strip():
            pieces.append((start, end, "".join(buf), title or last_sym))
        buf, size, title = [], 0, ""

    for n, line in enumerate(text.splitlines(keepends=True), 1):
        parts = [line[i:i + MAX_CHARS] for i in range(0, len(line), MAX_CHARS)] or [line]
        for part in parts:
            bound = bool(_BOUNDARY.match(part))
            if buf and ((bound and len(buf) >= MIN_LINES) or len(buf) >= MAX_LINES or size + len(part) > MAX_CHARS):
                flush(n - 1 if part is parts[0] else n)
            if not buf:
                start = n
            sym = _symbol(part) if bound or len(part) < 400 else ""
            if sym and bound:
                title = title or sym
                last_sym = sym
            buf.append(part)
            size += len(part)
    flush(len(text.splitlines()) or 1)
    return pieces


def html_to_text(s, heading=None):
    """Readable text from HTML (or HTML built up in JavaScript strings), with headings as "## …" lines."""
    if heading:
        s = re.sub(heading, lambda m: "\n\n## " + (m.group(1) if m.groups() else m.group()) + "\n", s)
    s = re.sub(r"""(['"`])\s*\+\s*\1""", "", s)           # 'one'+'two' -> onetwo
    s = s.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n")
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<h[1-3][^>]*>", "\n\n## ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/ol|/ul|p|div|tr)\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]{0,2000}>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", s)
    lines = [line.strip() for line in s.splitlines()]
    lines = [line for line in lines if not re.fullmatch(r"[^\w#\-•]{1,4}", line)]   # leftovers like ', or '+
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_help(text, base_title):
    """(start line, end line, text, title) sections of help text, split at "#" headings."""
    pieces, lines, head, start, buf = [], text.splitlines(), base_title, 1, []

    def flush(end):
        body = "\n".join(buf).strip()
        while body:
            cut = body if len(body) <= MAX_CHARS else body[:body.rfind("\n", 0, MAX_CHARS) if body.rfind("\n", 0, MAX_CHARS) > 500 else MAX_CHARS]
            pieces.append((start, end, cut, head))
            body = body[len(cut):].strip()

    in_code = False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,4})\s+(.+)", line)
        if m and buf:
            flush(n - 1)
            buf = []
        if m:
            h = m.group(2).strip().rstrip("#").strip()[:80]
            if len(m.group(1)) == 1 and base_title and h.lower() == base_title.lower():
                head = base_title                     # the page's own title: no "Title › Title"
            else:
                head = base_title + " › " + h if base_title else h
            start = n
        buf.append(line)
    flush(len(lines))
    return pieces


# ---------------------------------------------------------------- files ----

def _excluded(rel, patterns):
    parts = rel.split("/")
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatch(rel, pat):
                return True
        elif any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def list_files(proj):
    """Relative paths of the files to read, and how many were skipped because they looked like secrets."""
    root = proj["root"]
    include = proj["include"] or DEFAULT_INCLUDE
    exclude = DEFAULT_EXCLUDE + list(proj["exclude"] or [])
    limit = proj["max_file_kb"] * 1024
    out, secret = [], 0
    for d, dirs, files in os.walk(root):
        reld = os.path.relpath(d, root).replace(os.sep, "/")
        reld = "" if reld == "." else reld + "/"
        dirs[:] = sorted(x for x in dirs if not _excluded(reld + x, exclude) and not os.path.islink(os.path.join(d, x)))
        for name in sorted(files):
            rel = reld + name
            if _excluded(rel, exclude) or not any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in include):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in CODE_EXTS and SECRET_NAME.search(name):
                secret += 1
                continue
            try:
                if os.path.getsize(os.path.join(d, name)) <= limit:
                    out.append(rel)
            except OSError:
                pass
    return out, secret


def _help_items(proj):
    """The help entries, with patterns like "manual/**/*.md" expanded to one entry per file (sorted)."""
    items = []
    for h in proj["help"] or []:
        h = {"file": h} if isinstance(h, str) else dict(h)
        if any(c in h["file"] for c in "*?["):
            root = proj["root"]
            for path in sorted(glob.glob(os.path.join(root, h["file"]), recursive=True)):
                if os.path.isfile(path):
                    items.append(dict(h, file=os.path.relpath(path, root).replace(os.sep, "/")))
            continue
        items.append(h)
    return items


def markdown_page(text):
    """(title, status, text) for a Markdown help page: its front matter blanked (line numbers stay right), comments
    removed, and app:/show: links (which only work inside the app) reduced to their words."""
    title, status = "", ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            close = text.find("\n", end + 4)
            close = len(text) if close < 0 else close
            for line in text[3:end].splitlines():
                m = re.match(r"^\s*(title|status)\s*:\s*(.*?)\s*$", line)
                if m:
                    title, status = (m.group(2), status) if m.group(1) == "title" else (title, m.group(2))
            text = "\n" * text.count("\n", 0, close) + text[close:]
    # drop comments, but not inside ``` code blocks (a page may show a comment as an example, e.g. a hints list)
    parts = re.split(r"(^```.*?^```[^\n]*$)", text, flags=re.S | re.M)
    text = "".join(p if p.startswith("```") else re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), p, flags=re.S)
                   for p in parts)
    text = re.sub(r"\[([^\]]+)\]\((?:app|show):[^)]*\)", r"\1", text)
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else ""
    return title, status, text


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- index ----

class Index:
    """The search index for one project. Index.get(proj) keeps one per project and rebuilds it when files change."""
    K1, B = 1.4, 0.75
    _cache, _lock = {}, threading.Lock()

    def __init__(self, proj):
        self.proj = proj
        self.chunks = []         # {"kind", "file", "start", "end", "text", "title", "tf", "n"}
        self.df = collections.Counter()
        self.files, self.skipped_secret, self.help_files = [], 0, []
        self.stamp = None
        self.checked = 0.0
        self.built_s = 0.0

    @classmethod
    def get(cls, proj):
        with cls._lock:
            idx = cls._cache.get(proj["id"])
            if idx is None or idx.proj != proj:
                idx = cls._cache[proj["id"]] = Index(proj)
        idx.refresh()
        return idx

    def _stamp(self, files):
        h = hashlib.sha1()
        for rel in files:
            try:
                st = os.stat(os.path.join(self.proj["root"], rel))
                h.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
            except OSError:
                pass
        return h.hexdigest()

    def refresh(self, force=False):
        """Rebuild if any file was added, removed or changed (checked at most every 5 seconds)."""
        with self._lock:
            if not force and time.monotonic() - self.checked < 5 and self.stamp:
                return
            files, secret = list_files(self.proj)
            stamp = self._stamp(files)
            self.checked = time.monotonic()
            if stamp == self.stamp and not force:
                return
            t0 = time.monotonic()
            self._build(files)
            self.files, self.skipped_secret, self.stamp = files, secret, stamp
            self.built_s = time.monotonic() - t0

    def _add(self, kind, rel, start, end, text, title):
        tf = collections.Counter(terms(text + " " + title + " " + rel.rsplit("/", 1)[-1]))
        if not tf:
            return
        self.chunks.append({"kind": kind, "file": rel, "start": start, "end": end, "text": text, "title": title,
                            "tf": tf, "n": sum(tf.values())})
        self.df.update(tf.keys())

    def _build(self, files):
        self.chunks, self.df = [], collections.Counter()
        root = self.proj["root"]
        whole_help = set()
        self.help_files = []
        for h in _help_items(self.proj):
            rel = h["file"].replace(os.sep, "/")
            path = os.path.join(root, rel)
            try:
                text = read_text(path)
            except OSError:
                continue
            if text is None:
                continue
            title = h.get("title") or ""
            if h.get("from") or h.get("to"):
                i = text.find(h["from"]) if h.get("from") else 0
                if i < 0:
                    continue
                j = text.find(h["to"], i + 1) if h.get("to") else -1
                first_line = text.count("\n", 0, i) + 1
                text = text[i:j if j > 0 else len(text)]
            else:
                first_line = 1
                whole_help.add(rel)
            ext = os.path.splitext(rel)[1].lower()
            if ext == ".md":
                page_title, status, text = markdown_page(text)
                if status == "planned":       # a placeholder page: nothing written yet
                    whole_help.add(rel)
                    continue
                title = title or page_title
            converted = ext in (".html", ".htm", ".js", ".py", ".gs") or bool(h.get("heading"))
            if converted:
                text = html_to_text(text, h.get("heading"))
            text = redact(text)
            self.help_files.append({"file": rel, "title": title or rel, "line": first_line})
            for s, e, body, head in chunk_help(text, title or rel):
                if converted:   # lines no longer match the file: point at where the help starts
                    s = e = first_line
                self._add("help", rel, s, e, body, head)
        for rel in files:
            if rel in whole_help:
                continue
            try:
                text = read_text(os.path.join(root, rel))
            except OSError:
                continue
            if not text:
                continue
            text = redact(text)
            ext = os.path.splitext(rel)[1].lower()
            if ext in DOC_EXTS:
                for s, e, body, head in chunk_help(text, rel):
                    self._add("doc", rel, s, e, body, head)
            else:
                for s, e, body, head in chunk_code(text):
                    self._add("code", rel, s, e, body, head)
        self.avg = {k: (sum(c["n"] for c in self.chunks if c["kind"] == k) / max(1, sum(1 for c in self.chunks if c["kind"] == k)))
                    for k in ("help", "doc", "code")}

    def search(self, query, kinds=("help", "doc", "code"), limit=10, chars=None, skip=(), boost=None):
        """Best chunks for query (a string or {term: weight}), highest score first. chars caps the total text.
        boost: {path prefix: factor}, e.g. {"manual/dev/": 1.6} to prefer developer pages for a code question."""
        q = query if isinstance(query, dict) else collections.Counter(terms(query))
        if not q or not self.chunks:
            return []
        N = len(self.chunks)
        scored = []
        for i, c in enumerate(self.chunks):
            if c["kind"] not in kinds or i in skip:
                continue
            s = 0.0
            for t, w in q.items():
                f = c["tf"].get(t)
                if not f:
                    continue
                idf = math.log(1 + (N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += w * idf * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * c["n"] / self.avg[c["kind"]]))
            if s:
                matched = sum(1 for t in q if t in c["tf"])
                s *= 1 + 0.15 * (matched - 1)          # more of the question's words beats one word many times
                if c["kind"] == "doc":
                    s *= 0.85
                elif c["kind"] == "help":
                    s *= 1.25    # the help is written for people, so it wins a close call
                for prefix, factor in (boost or {}).items():
                    if c["file"].startswith(prefix):
                        s *= factor
                scored.append((s, i))
        scored.sort(reverse=True)
        out, total = [], 0
        for s, i in scored:
            c = self.chunks[i]
            if chars is not None and total + len(c["text"]) > chars:
                if total:
                    continue
            out.append(dict(c, score=round(s, 2), i=i))
            total += len(c["text"])
            if len(out) >= limit:
                break
        return out

    def summary(self):
        kinds = collections.Counter(c["kind"] for c in self.chunks)
        return {"files": len(self.files), "chunks": len(self.chunks), "help": kinds["help"], "docs": kinds["doc"],
                "code": kinds["code"], "skipped_secret": self.skipped_secret, "help_files": self.help_files,
                "build_s": round(self.built_s, 2)}

    # ------------------------------------------------------------ mirror ----

    def mirror(self):
        """A folder holding a copy of just the indexed files, secrets hidden, for the Claude engine to search.
        The real project folder (data files, keys, customer records) is never opened by it."""
        dest = os.path.join(config.CACHE_DIR, "mirror", re.sub(r"[^\w.-]", "_", self.proj["id"]))
        manifest_path = os.path.join(dest, ".askapp-manifest.json")
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
        files = sorted(set(self.files) | {h["file"] for h in self.help_files})
        new = {}
        for rel in files:
            src = os.path.join(self.proj["root"], rel)
            try:
                st = os.stat(src)
            except OSError:
                continue
            key = f"{st.st_mtime_ns}:{st.st_size}"
            new[rel] = key
            out = os.path.join(dest, rel)
            if manifest.get(rel) == key and os.path.exists(out):
                continue
            text = read_text(src)
            if text is None:
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(redact(text))
        for rel in set(manifest) - set(new):
            try:
                os.remove(os.path.join(dest, rel))
            except OSError:
                pass
        os.makedirs(dest, exist_ok=True)
        with open(manifest_path, "w") as f:
            json.dump(new, f)
        return dest


def drop_mirror(pid):
    shutil.rmtree(os.path.join(config.CACHE_DIR, "mirror", re.sub(r"[^\w.-]", "_", pid)), ignore_errors=True)
