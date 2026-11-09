"""Settings for Ask the App.

One JSON file holds everything: ~/.config/ask-the-app/config.json by default, or the path in
ASKAPP_CONFIG, or serve.py --config. Anything left out falls back to DEFAULTS below, so a config only
needs the projects. See config.example.json.
"""
import copy
import json
import os

CONFIG_PATH = os.path.expanduser(os.environ.get("ASKAPP_CONFIG") or "~/.config/ask-the-app/config.json")
STATE_DIR = os.path.expanduser(os.environ.get("ASKAPP_STATE") or "~/.local/state/ask-the-app")
CACHE_DIR = os.path.expanduser(os.environ.get("ASKAPP_CACHE") or "~/.cache/ask-the-app")

DEFAULTS = {
    "host": "127.0.0.1",          # 0.0.0.0 to let other PCs/phones on the network in (the allow list still applies)
    "port": 8795,
    "allow": ["127.0.0.0/8", "::1/128", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "100.64.0.0/10"],
    "origins": [],                # extra web pages (origins) allowed to use the button, e.g. "https://myapp.example"
    "transcribe_url": "",         # speech to text: an AI Hub /api/dictate address (Whisper). Blank hides the 🎤
    "engine": "auto",             # what "Auto" means: the first engine in auto_order that is on
    "auto_order": ["strata", "ollama"],
    "engines": {
        "ollama": {
            "label": "Qwen (local)",
            "url": "http://127.0.0.1:11434",
            "model": "qwen3:8b",
            "num_ctx": 16384,
            "help_chars": 10000,   # how much help / code text goes into one question
            "code_chars": 20000,
        },
        "strata": {
            "label": "Strata (big local model)",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key_file": "~/.config/strata/api_key",
            "model": "",           # blank = the first model Strata lists
            "busy_wait_s": 60,     # Strata answers one chat at a time: wait this long for another chat to finish
            "help_chars": 30000,
            "code_chars": 40000,
        },
        "claude": {
            "label": "Claude (online)",
            "bin": "",             # blank = find the `claude` command (PATH, ~/.local/bin, VS Code extension)
            "model": "sonnet",
            "timeout_s": 300,
            "help_chars": 8000,
        },
    },
    "projects": {},
}

PROJECT_DEFAULTS = {
    "name": "",
    "about": "",           # one or two sentences: what the program is for (helps the AI)
    "root": "",
    "help": [],            # help files, or {"file", "from", "to", "heading", "title"} slices of a file
    "include": None,       # None = index.DEFAULT_INCLUDE
    "exclude": [],         # added to index.DEFAULT_EXCLUDE
    "examples": [],        # example questions shown before the first question
    "engines": {},         # per-project engine settings, e.g. {"strata": {"help_chars": 45000}}
    "dev_help": [],        # path prefixes of help written for developers (e.g. "manual/dev/"): preferred for code questions
    "max_file_kb": 2048,
}


def _merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "projects":
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def project(pid, p):
    """A project with every field filled in, its root made absolute."""
    out = dict(copy.deepcopy(PROJECT_DEFAULTS), **(p or {}))
    out["id"] = pid
    out["root"] = os.path.abspath(os.path.expanduser(out["root"] or "."))
    out["name"] = out["name"] or os.path.basename(out["root"]) or pid
    return out


def load(path=None, extra_projects=None):
    """The settings: DEFAULTS, then the config file (if there is one), then extra_projects (from --root)."""
    cfg = copy.deepcopy(DEFAULTS)
    path = os.path.expanduser(path or CONFIG_PATH)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    cfg["engines"] = {k: v for k, v in cfg["engines"].items() if v}   # "claude": null in a config switches it off
    cfg["projects"] = dict(cfg.get("projects") or {}, **(extra_projects or {}))
    cfg["projects"] = {pid: project(pid, p) for pid, p in cfg["projects"].items()}
    cfg["config_path"] = path
    return cfg
