#!/usr/bin/env python3
"""Measure how well the Ask button answers: ask a set of real questions and check the answers.

    python3 eval.py ttr questions.jsonl --retrieval-only        # seconds: does the search find the right pages?
    python3 eval.py ttr questions.jsonl --engine strata          # minutes: does Strata's answer contain the facts?
    python3 eval.py ttr q1.jsonl q2.jsonl --engine ollama --out report.md

A question file has one JSON object per line:
    {"q": "How do I send an estimate?", "audience": "user", "pages": ["screens/invoice-editor"],
     "facts": ["✉ Email estimate", "P&A sent"]}
"pages" are manual page ids (or any help/code file path) the answer should come from; "facts" are short phrases a
correct answer must contain (matched loosely: case, punctuation and emoji are ignored). Questions run one at a time,
so Strata's one-chat-at-a-time rule is kept. The report lists each question, then the weakest pages.
"""
import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from askapp import answer, config, engines  # noqa: E402
from askapp.index import Index  # noqa: E402


def loose(s):
    return re.sub(r"[^\w]+", " ", str(s).lower()).strip()


def page_file(p):
    p = p.strip()
    if p.endswith((".md", ".py", ".html", ".js", ".gs", ".txt")):
        return p
    return "manual/" + p.strip("/") + ".md"


def load(paths):
    out = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    q = json.loads(line)
                except ValueError as e:
                    sys.exit("%s line %d isn't JSON: %s" % (path, n, e))
                q["_src"] = "%s:%d" % (os.path.basename(path), n)
                out.append(q)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("questions", nargs="+")
    ap.add_argument("--engine", default="strata")
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions")
    ap.add_argument("--out", help="also write the report to this Markdown file")
    a = ap.parse_args()

    cfg = config.load(a.config)
    proj = cfg["projects"].get(a.project)
    if not proj:
        sys.exit("No project %r in %s" % (a.project, cfg["config_path"]))
    qs = load(a.questions)
    if a.limit:
        qs = qs[:a.limit]
    idx = Index.get(proj)
    eng = dict(cfg["engines"].get(a.engine) or {})
    eng.update((proj.get("engines") or {}).get(a.engine) or {})
    help_chars = int(eng.get("help_chars", 10000))

    lines, page_scores = [], collections.defaultdict(list)
    found_n = facts_sum = facts_n = 0
    t_all = time.monotonic()
    for k, q in enumerate(qs, 1):
        want = [page_file(p) for p in q.get("pages") or []]
        boost = {pp: 1.6 for pp in proj.get("dev_help") or []} if answer.is_dev_question(q["q"]) else None
        hits = answer.pin_screen_page(idx, answer._query(q["q"], {}), {}, idx.search(answer._query(q["q"], {}), ("help", "doc"),
                                      limit=max(12, help_chars // 2500), chars=help_chars, boost=boost), help_chars)
        got = [h["file"] for h in hits]
        rank = next((i + 1 for i, f in enumerate(got) if f in want), 0)
        found = bool(rank) or not want
        found_n += found
        row = {"n": k, "q": q["q"], "audience": q.get("audience", ""), "rank": rank, "found": found}
        if not a.retrieval_only:
            t0 = time.monotonic()
            try:
                res = answer.ask(cfg, a.project, q["q"], {}, a.engine, say=lambda s: None)
                text, err = res["answer"], ""
            except (ValueError, engines.EngineError) as e:
                text, err = "", str(e)
            facts = q.get("facts") or []
            ok = [f for f in facts if loose(f) and loose(f) in loose(text)]
            row.update(answer=text, error=err, facts_ok=len(ok), facts_n=len(facts), missing=[f for f in facts if f not in ok],
                       seconds=round(time.monotonic() - t0, 1))
            if facts:
                facts_sum += len(ok) / len(facts)
                facts_n += 1
            score = (len(ok) / len(facts)) if facts else (1.0 if text else 0.0)
        else:
            score = 1.0 if found else 0.0
        for p in want:
            page_scores[p].append(score)
        lines.append(row)
        status = ("found #%d" % rank) if rank else ("NOT FOUND" if want else "-")
        extra = "" if a.retrieval_only else "  facts %d/%d  %ss%s" % (row["facts_ok"], row["facts_n"], row["seconds"], ("  ERROR " + row["error"][:80]) if row["error"] else "")
        print("%2d. %-9s %s%s" % (k, status, q["q"][:80], extra), flush=True)

    out = ["# Ask the App evaluation: %s (%s)" % (a.project, "retrieval only" if a.retrieval_only else a.engine), "",
           "%s · %d questions · %.0f s" % (time.strftime("%Y-%m-%d %H:%M"), len(qs), time.monotonic() - t_all), "",
           "- Right page found by the search: **%d of %d**" % (found_n, len(qs))]
    if not a.retrieval_only and facts_n:
        out.append("- Facts in the answers: **%.0f%%** (average over %d questions)" % (100 * facts_sum / facts_n, facts_n))
    out += ["", "## Questions", ""]
    for r in lines:
        out.append("%d. **%s** (%s) — page %s" % (r["n"], r["q"], r["audience"] or "?", ("#%d" % r["rank"]) if r["rank"] else "not found"))
        if not a.retrieval_only:
            out.append("   - facts %d/%d%s; %ss" % (r["facts_ok"], r["facts_n"], (", missing: " + "; ".join(r["missing"])) if r["missing"] else "", r["seconds"]))
            if r["error"]:
                out.append("   - error: " + r["error"])
            out.append("   - answer: " + " ".join(r["answer"].split())[:700])
    weak = sorted(((sum(v) / len(v), p, len(v)) for p, v in page_scores.items()), key=lambda x: x[0])
    out += ["", "## Pages, weakest first", ""] + ["- %s: %.0f%% over %d question%s" % (p, 100 * s, n, "" if n == 1 else "s") for s, p, n in weak]
    report = "\n".join(out) + "\n"
    print("\n" + "\n".join(out[:8]))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("report written to", a.out)


if __name__ == "__main__":
    main()
