"""Purpose: the downloadable session report, as a self-contained HTML page and as a PDF (pandoc + XeLaTeX).
A port of labcrew's ui/report_export.R (GWAS pack only), so both apps hand out the same kind of report.

Integrity rule: this module only changes how the report LOOKS. Each round's report content is the reviewed,
number-checked markdown whose SHA-256 is in the audit log; only formatting changes (title line dropped, headings
re-levelled, "->" typeset as an arrow), so the hash-checked source stays the Markdown. Every other number is
copied as-is from the session state, the R engine output, the audit log or the tool log: nothing is recomputed,
rounded or derived.

Safety: LLM-written text never becomes markup. HTML: markdown-it with raw HTML disabled, everything else escaped.
PDF: pandoc reads markdown with raw TeX / raw HTML off, and raw-attribute blocks in the round text are defused.

    build_parts(...)  -> dict      everything both renderers need
    render_html(P)    -> str       one file, charts inlined as base64 PNG
    render_pdf(P)     -> bytes     needs pandoc and xelatex on PATH (pdf_available())
"""
from __future__ import annotations

import base64
import html
import json
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt

from gwascrew import evidence
from gwascrew.r_stats import ROOT, rscript

CHART_SCRIPT = ROOT / "stats" / "report_charts.R"
OUTCOME = {"CONFIRM": ("Confirm", "confirm", "lcgood", "✓"), "INCONCLUSIVE": ("Inconclusive", "inconclusive", "lcwarn", "!"),
           "FAIL": ("Fail", "fail", "lcbad", "✕")}
STATUS = {"running": "Agents working", "awaiting_approval": "Waiting for the reviewer", "finished": "Finished",
          "needs_review": "Stopped · needs human review", "error": "Error"}
_MD = MarkdownIt("commonmark", {"html": False}).enable("table")
_chart_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def na(x) -> str:
    return "—" if x is None or x == "" or x == [] else str(x)


def pretty_md(md: str) -> str:                       # presentation only
    return md.replace(" -> ", " → ")


def drop_title(md: str) -> str:                      # the round heading replaces it
    return re.sub(r"^# [^\n]*\n", "", md, count=1)


def demote(md: str, by: int) -> str:
    return re.sub(r"(^|\n)(#{1,5}) ", lambda m: m.group(1) + m.group(2) + "#" * by + " ", md)


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    return datetime.fromisoformat(ts[:19]).strftime("%d %B %Y, %H:%M UTC")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")


def pdf_available() -> bool:
    return bool(shutil.which("pandoc") and shutil.which("xelatex"))


# ------------------------------------------------------------------ data model
def build_parts(state, session, audit: list[dict], tools: list[dict], status: str | None = None) -> dict:
    """state: the Flow state; session: the live Session (None if the analysis never started)."""
    rounds = list(state.rounds)
    reps = [r for r in rounds if r.get("report_md")]
    last = reps[-1] if reps else None
    lam_rule = next((r for r in session.ruleset.rules if r.id == "lambda_range"), None) if session else None
    res = session.result if session else None
    return {
        "sid": state.session_id, "rounds": rounds, "reps": reps, "last": last, "truth": dict(state.truth or {}),
        "audit": audit, "tools": tools, "result": res, "engine": session.engine if session else {},
        "lambda_range": (lam_rule.min, lam_rule.max) if lam_rule else None,
        "meta": {"question": state.question, "session": state.session_id, "status": STATUS.get(status or state.status, status or state.status),
                 "stop": state.stop_reason, "reviewer": state.reviewer, "seed": state.seed, "rounds_max": state.rounds_max,
                 "started": fmt_ts(tools[0]["timestamp"]) if tools else "—", "generated": utc_now(),
                 "rules": f"v{last['decision']['rules_version']}" if last else "—",
                 "sha": last["report_sha256"] if last else "—",
                 "outcome": last["decision"]["outcome"] if last else None}}


def round_rows(P) -> list[list]:
    rng = P["lambda_range"]
    out = []
    for r in P["reps"]:
        g, d = r["gwas"], r["decision"]
        pos = [a["variant_id"] for a in r.get("assay", []) if a["assay_positive"]]
        out.append([na(r["round"]), na(g["lambda_gc"]) + (f" ({rng[0]} – {rng[1]})" if rng else ""), na(g["n_hits"]),
                    na(g["bonferroni_threshold"]), na(g["n_tested"]), ("badge", d["outcome"]),
                    ", ".join(r["human"]["approved"]) or "none" if r.get("human") else "pending" if r is P["last"] else "—",
                    ", ".join(pos) or "none" if r.get("assay") else "—"])
    return out


ROUND_HEAD = ["Round", "λ (allowed)", "Bonferroni hits", "Threshold", "Variants tested", "Code decision",
              "Approved for assay", "Assay positive"]
TOP_HEAD = ["Variant", "Beta", "SE", "p-value", "PIP", "Bonferroni hit"]
ASSAY_HEAD = ["Round", "Variant", "PIP", "Assay result", "Confirmation rule"]
AUDIT_HEAD = ["Time (UTC)", "Round", "Reviewer", "Approved", "Rejected", "Code decision", "Rules", "Report sha256", "Comment"]
TOOL_HEAD = ["Time (UTC)", "Tool", "Version", "Round", "Status", "Inputs sha256"]


def top_rows(P) -> list[list]:
    res = P["result"]
    return [[v["variant_id"], na(v.get("beta")), na(v.get("se")), na(v.get("p")), na(v.get("pip")),
             "yes" if v.get("bonferroni_hit") else ""] for v in (res.ranked if res else [])]


def assay_rows(P) -> list[list]:
    out = []
    for r in P["rounds"]:
        pip = {x["variant_id"]: x["pip"] for x in r.get("proposal_rows", [])}
        for a in r.get("assay", []):
            out.append([na(r["round"]), a["variant_id"], na(pip.get(a["variant_id"])),
                        "replicated" if a["assay_positive"] else "not replicated", na(r.get("assay_decision", {}).get("outcome"))])
    return out


def audit_rows(P) -> list[list]:
    return [[a["timestamp"][11:19], na(a["round"]), na(a["reviewer"]), ", ".join(a["approved"]) or "none",
             ", ".join(a["rejected"]) or "none", na(a["code_decision"]), f"v{a['rules_version']}",
             a["shown_report_sha256"][:12], na(a["comment"])] for a in P["audit"]]


def tool_rows(P) -> list[list]:
    return [[t["timestamp"][11:23], t["tool"], na(t.get("tool_version")), na(t["inputs"].get("round")), na(t.get("status")),
             na(t.get("inputs_sha256", "")[:16])] for t in P["tools"]]


def truth_rows(P) -> list[list]:
    t = P["truth"]
    if "power" not in t:
        return []
    j = lambda k: ", ".join(t.get(k) or []) or "none"   # noqa: E731
    return [["Power (planted causal variants found by Bonferroni)", na(t["power"])], ["Found", j("true_positives")],
            ["Missed", j("missed")], ["False-positive rate", na(t.get("false_positive_rate"))],
            ["Assay: true replications", j("assay_true_replications")], ["Assay: false replications", j("assay_false_replications")]]


def sources(P):
    """Unique passages, SOP conflicts and quarantined documents over all rounds, in first-seen order."""
    seen, cites, conflicts, quar = set(), [], {}, {}
    for r in P["reps"]:
        for s in r.get("sources", []):
            if s["citation"] not in seen:
                seen.add(s["citation"]); cites.append(s)
        for v in r.get("sop_versions", []):
            if v.get("conflict"):
                conflicts[v["sop_id"]] = v["conflict_detail"]
        for q in r.get("quarantined", []):
            quar.setdefault(q["doc_id"], q["flags"])
    return cites, conflicts, quar


# ------------------------------------------------------------------ charts (base R, cached per session)
def charts(P) -> list[dict]:
    """Manhattan + QQ PNGs drawn by stats/report_charts.R. No R or no data: the report goes out without figures."""
    res = P["result"]
    if res is None or not res.plot:
        return []
    folder = evidence.log_dir() / "data" / P["sid"] / "report"
    files = {"manhattan": folder / "manhattan.png", "qq": folder / "qq.png"}
    with _chart_lock:
        if not all(f.exists() for f in files.values()):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                inp = folder / "charts_input.json"
                inp.write_text(json.dumps({"plot": res.plot, "bonferroni_threshold": res.bonferroni_threshold,
                                           "lambda_gc": res.lambda_gc}), encoding="utf-8")
                subprocess.run([rscript(), str(CHART_SCRIPT), str(inp), str(folder)], capture_output=True, timeout=120)
            except Exception:  # noqa: BLE001 - figures are optional; the numbers are in the tables
                return []
    if not all(f.exists() for f in files.values()):
        return []
    return [{"id": "manhattan", "file": files["manhattan"],
             "caption": "Manhattan plot. Every tested variant; dashed line = Bonferroni threshold; orange = significant."},
            {"id": "qq", "file": files["qq"],
             "caption": f"QQ plot of observed vs expected p-values; genomic inflation λ = {res.lambda_gc}."}]


# ===================================================================== HTML
E = html.escape


def html_table(head, rows, cls="") -> str:
    if not rows:
        return ""
    num = [all(re.fullmatch(r"[-–—0-9.,eE+ \[\]≥%]*", str(r[j])) for r in rows if not isinstance(r[j], tuple))
           for j in range(len(head))]

    def cell(v, j):
        if isinstance(v, tuple):  # ("badge", OUTCOME)
            lab, c, *_ = OUTCOME.get(v[1], (v[1], "", "", ""))
            return f'<td><span class="badge {c}">{E(lab)}</span></td>'
        return f'<td{" class=num" if num[j] else ""}>{E(str(v))}</td>'
    th = "".join(f'<th{" class=num" if num[j] else ""}>{E(h)}</th>' for j, h in enumerate(head))
    body = "".join("<tr>" + "".join(cell(v, j) for j, v in enumerate(r)) + "</tr>" for r in rows)
    return f'<table class="tbl {cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def md_block(md: str) -> str:
    return _MD.render(pretty_md(md))


HTML_CSS = r"""
:root{--ink:#111827;--ink2:#4b5563;--muted:#6b7280;--line:#e5e7eb;--soft:#f8f9fb;--accent:#4f46e5;}
*{box-sizing:border-box} html{background:#eef0f4}
body{margin:0;font:15px/1.6 Inter,"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}
.page{max-width:900px;margin:32px auto;background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(16,24,40,.08);overflow:hidden}
.cover{padding:36px 48px 30px;background:linear-gradient(135deg,#1e1b4b 0%,#312e81 55%,#4338ca 100%);color:#fff;position:relative}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:-.01em;font-size:15px;opacity:.95}
.logo{width:30px;height:30px;border-radius:9px;background:rgba(255,255,255,.16);display:grid;place-items:center;font-size:15px}
.sim{position:absolute;top:34px;right:48px;background:#fde68a;color:#78350f;font-weight:800;font-size:11px;letter-spacing:.08em;padding:5px 10px;border-radius:999px}
.kicker{margin-top:26px;font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:#c7d2fe;font-weight:700}
h1.title{margin:6px 0 4px;font-size:30px;line-height:1.2;letter-spacing:-.025em;font-weight:800}
.q{font-size:17px;color:#e0e7ff;margin:0}
.meta{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border-bottom:1px solid var(--line);background:var(--soft)}
.meta > div{padding:14px 18px;border-right:1px solid var(--line)} .meta > div:nth-child(4n){border-right:0}
.meta > div:nth-child(n+5){border-top:1px solid var(--line)}
.tbl.narrow{width:auto;min-width:340px}
.meta .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700}
.meta .v{font-size:13px;font-weight:600;word-break:break-all}
.body{padding:30px 48px 40px}
.toc{border:1px solid var(--line);border-radius:10px;padding:12px 18px;margin:0 0 26px;font-size:13px;background:#fff}
.toc b{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.toc ol{margin:6px 0 0;padding-left:20px;columns:2} .toc a{color:var(--ink);text-decoration:none} .toc a:hover{color:var(--accent)}
h2{font-size:20px;letter-spacing:-.015em;margin:34px 0 12px;padding-bottom:8px;border-bottom:2px solid var(--ink);display:flex;gap:10px;align-items:baseline}
h2 .n{color:var(--accent);font-weight:800}
h3{font-size:16px;margin:24px 0 8px;color:var(--ink)}
.round{border:1px solid var(--line);border-radius:12px;padding:4px 22px 16px;margin:16px 0;overflow-x:auto}
.round h4{font-size:14.5px;margin:18px 0 6px} .round > h3{margin-top:16px}
.callout{display:flex;gap:16px;align-items:center;border-radius:12px;padding:16px 20px;margin:4px 0 18px;border:1px solid}
.callout .big{font-size:22px;font-weight:800;letter-spacing:-.01em} .callout .sub{font-size:13.5px;color:var(--ink2)}
.callout .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:800;opacity:.8}
.callout.confirm{background:#f0fdf4;border-color:#bbf7d0;color:#14532d} .callout.inconclusive{background:#fffbeb;border-color:#fde68a;color:#78350f}
.callout.fail{background:#fef2f2;border-color:#fecaca;color:#7f1d1d} .callout.info{background:#eef2ff;border-color:#c7d2fe;color:#312e81}
.callout .icon{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;font-size:22px;background:rgba(255,255,255,.7);flex:none}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:12px 0 18px}
.kpi{border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700}
.kpi .v{font-size:20px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums} .kpi .s{font-size:12px;color:var(--ink2)}
.tblwrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 14px;font-variant-numeric:tabular-nums}
th{background:var(--soft);text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink2);font-weight:700}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top} td.num,th.num{text-align:right;white-space:nowrap}
thead th{border-top:1.5px solid var(--ink);border-bottom:1px solid var(--ink)} tbody tr:last-child td{border-bottom:1.5px solid var(--ink)}
.small table, table.small{font-size:12px}
.badge{display:inline-block;font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;letter-spacing:.03em}
.badge.confirm{background:#dcfce7;color:#15803d} .badge.inconclusive{background:#fef3c7;color:#b45309} .badge.fail{background:#fee2e2;color:#b91c1c}
figure{margin:14px 0 20px;border:1px solid var(--line);border-radius:12px;padding:12px 12px 6px}
figure img{width:100%;display:block} figcaption{font-size:12.5px;color:var(--ink2);padding:8px 4px 4px} figcaption b{color:var(--ink)}
blockquote{margin:10px 0;padding:8px 14px;border-left:3px solid var(--accent);background:#f5f5ff;color:var(--ink2);border-radius:0 8px 8px 0}
code{font-family:Consolas,"JetBrains Mono",monospace;font-size:.86em;background:#f1f2f6;padding:1px 5px;border-radius:4px;color:#3730a3}
.cite{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.cite .id{flex:none;min-width:30px;height:24px;padding:0 6px;border-radius:6px;background:#eef2ff;color:var(--accent);font-weight:800;font-size:11.5px;display:grid;place-items:center}
.cite .tag{font-weight:700} .note{border-radius:10px;padding:10px 14px;font-size:13px;margin:8px 0}
.note.warn{background:#fffbeb;border:1px solid #fde68a;color:#78350f} .note.bad{background:#fef2f2;border:1px solid #fecaca;color:#7f1d1d}
.small{font-size:12px} .mono{font-family:Consolas,monospace;font-size:12px;word-break:break-all}
.integrity{margin-top:34px;border-top:1px solid var(--line);padding-top:14px;font-size:12px;color:var(--muted)}
.footer{background:var(--soft);border-top:1px solid var(--line);padding:14px 48px;font-size:11.5px;color:var(--muted);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media (max-width:760px){.cover,.body{padding-left:20px;padding-right:20px}.meta{grid-template-columns:repeat(2,1fr)}.kpis{grid-template-columns:1fr}.sim{position:static;display:inline-block;margin-top:12px}.toc ol{columns:1}}
@page{size:A4;margin:14mm}
@media print{html{background:#fff}.page{margin:0;box-shadow:none;border-radius:0;max-width:none}figure,.round,.callout,table{break-inside:avoid}h2{break-after:avoid}}
"""

INTEGRITY = ("Numbers come from logged tool calls (the R engine, the rules check, the ranking); pass/fail decisions come from code "
             "and the versioned rules file, never from the language model. Each round's report content is the reviewed version with "
             "formatting only changed (title line removed, headings re-levelled, arrows typeset); the hash-checked source (sha256 per "
             "round, also in the audit log) is the Markdown. All data are simulated / fictional.")


def render_html(P) -> str:
    m, last, S = P["meta"], P["last"], []

    def add(sid_, title, body):
        S.append((sid_, title, body))

    # 1. executive summary
    o = m["outcome"]
    if o:
        lab, cls, _, icon = OUTCOME[o]
        callout = (f'<div class="callout {cls}"><div class="icon">{icon}</div><div><div class="lbl">Final decision · code + versioned rules</div>'
                   f'<div class="big">{E(lab)} · {E(m["status"])}</div><div class="sub">{E(m["stop"] or "")}</div></div></div>')
    else:
        callout = (f'<div class="callout info"><div class="icon">i</div><div><div class="lbl">Status</div>'
                   f'<div class="big">{E(m["status"])}</div><div class="sub">{E(m["stop"] or "No round has reached the reviewer yet.")}</div></div></div>')
    kpis = ""
    if last:
        g, rng = last["gwas"], P["lambda_range"]
        kp = lambda k, v, s: f'<div class="kpi"><div class="k">{E(k)}</div><div class="v">{E(str(v))}</div><div class="s">{E(s)}</div></div>'  # noqa: E731
        qc = ", ".join(f"{k} {v}" for k, v in g["qc_failed_by"].items())
        kpis = ('<div class="kpis">' + kp("Genomic inflation λ", g["lambda_gc"], f"allowed {rng[0]} – {rng[1]}" if rng else "")
                + kp("Bonferroni hits", g["n_hits"], f"p < {g['bonferroni_threshold']}")
                + kp("Variants tested", g["n_tested"], f"of {g['n_variants']} · QC removed: {qc}") + "</div>")
    errs = "".join(f'<div class="note bad"><b>Round {E(str(r["round"]))} stopped before reaching the reviewer.</b> '
                   f'<span class="mono">{E(r["error"])}</span></div>' for r in P["rounds"] if r.get("error"))
    tr = truth_rows(P)
    add("summary", "Executive summary", callout + kpis + errs
        + (f'<h3>Decision by round</h3><div class="tblwrap">{html_table(ROUND_HEAD, round_rows(P))}</div>' if P["reps"] else "")
        + (f'<h3>Against the planted truth</h3><p class="small">Possible only because the data are simulated.</p>'
           f'{html_table(["Measure", "Value"], tr, "narrow")}' if tr else ""))

    # 2. figures + top variants
    figs = charts(P)
    if figs:
        add("figures", "Figures", "".join(
            f'<figure><img src="data:image/png;base64,{base64.b64encode(f["file"].read_bytes()).decode()}" alt="{E(f["caption"])}">'
            f'<figcaption><b>Figure {i}. </b>{E(f["caption"])}</figcaption></figure>' for i, f in enumerate(figs, 1)))
    top = top_rows(P)
    if top:
        eng = P["engine"]
        who = f", R {eng.get('R')}, coloc {eng.get('coloc')}" if eng else ""
        add("estimates", "Top variants (posterior inclusion probability)",
            f'<p class="small">Latest analysis, best first. Values copied from the R engine output (run_gwas{E(who)}).</p>'
            f'<div class="tblwrap">{html_table(TOP_HEAD, top)}</div>')

    # 3. round reports, verbatim
    rounds_html = "".join(
        f'<div class="round"><h3>Round {E(str(r["round"]))}<span class="badge {OUTCOME[r["decision"]["outcome"]][1]}" '
        f'style="margin-left:8px;vertical-align:3px">{OUTCOME[r["decision"]["outcome"]][0]}</span></h3>'
        f'{md_block(demote(drop_title(r["report_md"]), 2))}<p class="small mono">report sha256 {E(r["report_sha256"])}</p></div>'
        for r in P["reps"])
    if P["reps"]:
        add("rounds", "Round-by-round reports",
            '<p class="small">Each block is the content of the reviewed report for that round (as checked by the Reviewer agent and the '
            'number check). Only formatting differs; the hash-checked source is the Markdown.</p>' + rounds_html)

    # 4. sources
    cites, conflicts, quar = sources(P)
    if cites or conflicts or quar:
        held = ", ".join(f"{d} ({', '.join(f)})" for d, f in quar.items())
        add("sources", "Sources",
            "".join(f'<div class="note warn"><b>SOP version conflict ({E(k)}). </b>{E(v)}</div>' for k, v in conflicts.items())
            + (f'<div class="note bad"><b>Held back from the agents (possible prompt injection): </b>{E(held)}</div>' if quar else "")
            + "".join(f'<div class="cite"><div class="id">{i}</div><div><span class="tag">{E(c["citation"])}</span><br>{E(c["text"])}</div></div>'
                      for i, c in enumerate(cites, 1)))

    # 5. appendices
    ar = assay_rows(P)
    add("assay", "Appendix A · Functional assay log (simulated)",
        f'<div class="tblwrap">{html_table(ASSAY_HEAD, ar)}</div>' if ar else '<p class="small">No variant has been sent to the assay yet.</p>')
    au, tl = audit_rows(P), tool_rows(P)
    add("audit", "Appendix B · Audit trail",
        "<h3>Human decisions</h3>" + (f'<div class="tblwrap">{html_table(AUDIT_HEAD, au)}</div>' if au else '<p class="small">No decisions yet.</p>')
        + "<h3>Tool calls (every calculation, with versions)</h3>"
        + (f'<div class="tblwrap">{html_table(TOOL_HEAD, tl, "small")}</div>' if tl else '<p class="small">None.</p>'))

    meta_cell = lambda k, v: f'<div><div class="k">{E(k)}</div><div class="v">{E(str(v))}</div></div>'  # noqa: E731
    title = f"Decision: {OUTCOME[o][0]}" if o else m["status"]
    toc = "".join(f'<li><a href="#{i}">{E(t)}</a></li>' for i, t, _ in S)
    secs = "".join(f'<h2 id="{i}"><span class="n">{n}</span>{E(t)}</h2>{b}' for n, (i, t, b) in enumerate(S, 1))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(f"LabCrew Lite report · {m['session']}")}</title><style>{HTML_CSS}</style></head>
<body><div class="page">
<div class="cover"><div class="brand"><div class="logo">⌬</div>LabCrew Lite</div><div class="sim">SIMULATED DATA</div>
<div class="kicker">GWAS analysis report</div><h1 class="title">{E(title)}</h1><p class="q">“{E(m["question"])}”</p></div>
<div class="meta">{meta_cell("Session", m["session"])}{meta_cell("Domain", "GWAS (simulated study)")}{meta_cell("Status", m["status"])}{meta_cell("Reviewer", m["reviewer"])}
{meta_cell("Started", m["started"])}{meta_cell("Generated", m["generated"])}{meta_cell("Rules file", m["rules"])}{meta_cell("Report sha256", m["sha"][:16])}</div>
<div class="body"><div class="toc"><b>Contents</b><ol>{toc}</ol></div>{secs}
<div class="integrity"><b>Integrity. </b>{E(INTEGRITY)}</div></div>
<div class="footer"><span>LabCrew Lite · session {E(m["session"])} · seed {E(str(m["seed"]))}</span><span>Generated {E(m["generated"])}</span></div>
</div></body></html>
"""


# ====================================================================== PDF
def tex_esc(x: str) -> str:
    x = str(x).replace("\\", "\\textbackslash{}")
    x = re.sub(r"([{}$&#_%])", r"\\\1", x)
    return x.replace("~", "\\textasciitilde{}").replace("^", "\\textasciicircum{}")


def md_esc(x) -> str:
    return re.sub(r"([\\`*_\[\]<>|$~^#@{}])", r"\\\1", str(x))


def defuse_raw(md: str) -> str:
    """Pandoc's raw_attribute (needed for our own cover) would also let a {=latex} block in LLM text through:
    escape the brace so it prints as text. Formatting only; the hashed source is untouched."""
    return md.replace("{=", "\\{=")


def md_table(head, rows, right_numbers=True) -> str:
    if not rows:
        return ""
    rows = [[(OUTCOME.get(v[1], (v[1],))[0] if isinstance(v, tuple) else str(v)) for v in r] for r in rows]
    num = [right_numbers and all(re.fullmatch(r"[-–—0-9.,eE+ \[\]≥%]*", r[j]) for r in rows) for j in range(len(head))]
    # Pandoc sizes wrapped pipe-table columns by dash count: size each column to its longest word/value.
    w = [max(4, max(max(len(t) for v in [head[j]] + [r[j] for r in rows] for t in (v.split(" ") or [""])),
                    min(max(len(v) for v in [head[j]] + [r[j] for r in rows]), 28))) for j in range(len(head))]
    line = lambda cells: "| " + " | ".join(md_esc(c) for c in cells) + " |"  # noqa: E731
    sep = "|" + "|".join(("-" * w[j] + ":") if num[j] else (":" + "-" * w[j]) for j in range(len(head))) + "|"
    return "\n".join([line(head), sep] + [line(r) for r in rows]) + "\n\n"


def raw_tex(*parts) -> str:
    return "\n```{=latex}\n" + "".join(parts) + "\n```\n"


def tex_badge(outcome: str) -> str:
    lab, _, tex, _ = OUTCOME[outcome]
    return f"\\lcbadge{{{tex}}}{{{lab.upper()}}}"


def pdf_fonts() -> dict:
    if platform.system() == "Windows":
        return {"main": "Segoe UI", "mono": "Consolas"}
    return {"main": "DejaVu Sans", "mono": "DejaVu Sans Mono"}


def tex_header(m) -> str:
    return r"""
\usepackage{fontspec}
\usepackage{fancyhdr,lastpage,titlesec,tcolorbox,tabularx,etoolbox,colortbl}
\tcbuselibrary{skins}
\usepackage{float,needspace}\floatplacement{figure}{H}
\definecolor{lcink}{HTML}{111827}\definecolor{lcink2}{HTML}{4B5563}\definecolor{lcmuted}{HTML}{6B7280}
\definecolor{lcaccent}{HTML}{4F46E5}\definecolor{lcnavy}{HTML}{1E1B4B}\definecolor{lcline}{HTML}{E5E7EB}\definecolor{lcsoft}{HTML}{F8F9FB}
\definecolor{lcgood}{HTML}{15803D}\definecolor{lcgoodbg}{HTML}{DCFCE7}\definecolor{lcwarn}{HTML}{B45309}\definecolor{lcwarnbg}{HTML}{FEF3C7}
\definecolor{lcbad}{HTML}{B91C1C}\definecolor{lcbadbg}{HTML}{FEE2E2}\definecolor{lcsim}{HTML}{FDE68A}\definecolor{lcsimink}{HTML}{78350F}
\color{lcink}
\setlength{\parindent}{0pt}\setlength{\parskip}{5pt plus 1pt}\linespread{1.08}
\titleformat{\section}{\Large\bfseries\color{lcink}}{\textcolor{lcaccent}{\thesection}}{0.6em}{}[\vspace{2pt}{\color{lcink}\titlerule[1pt]}]
\titleformat{\subsection}{\large\bfseries\color{lcink}}{\textcolor{lcaccent}{\thesubsection}}{0.6em}{}
\titleformat{\subsubsection}{\normalsize\bfseries\color{lcink2}}{}{0em}{}
\titlespacing*{\section}{0pt}{16pt}{8pt}\titlespacing*{\subsection}{0pt}{12pt}{4pt}\titlespacing*{\subsubsection}{0pt}{9pt}{2pt}
\pagestyle{fancy}\fancyhf{}\renewcommand{\headrulewidth}{0.4pt}\renewcommand{\headrule}{{\color{lcline}\hrule height \headrulewidth}}
\fancyhead[L]{\footnotesize\color{lcmuted}\textbf{LabCrew Lite}\enspace GWAS analysis report}
\fancyhead[R]{\footnotesize\colorbox{lcsim}{\textcolor{lcsimink}{\textbf{SIMULATED DATA}}}}
\fancyfoot[L]{\footnotesize\color{lcmuted}Session """ + tex_esc(m["session"]) + r""" \enspace·\enspace Generated """ + tex_esc(m["generated"]) + r"""}
\fancyfoot[R]{\footnotesize\color{lcmuted}Page \thepage\ of \pageref*{LastPage}}
\fancypagestyle{plain}{\fancyhf{}\renewcommand{\headrulewidth}{0pt}\fancyfoot[L]{\footnotesize\color{lcmuted}Session """ + tex_esc(m["session"]) + r"""}\fancyfoot[R]{\footnotesize\color{lcmuted}Page \thepage\ of \pageref*{LastPage}}}
\AtBeginEnvironment{longtable}{\small}
\setlength{\LTpre}{4pt}\setlength{\LTpost}{8pt}
\renewcommand{\arraystretch}{1.18}
\newcommand{\lcbadge}[2]{\tcbox[on line,colback=#1bg,colframe=#1bg,boxrule=0pt,arc=6pt,left=4pt,right=4pt,top=1pt,bottom=1pt,fontupper=\scriptsize\bfseries\color{#1}]{#2}}
\usepackage{caption}\captionsetup{font={small,color=lcink2},labelfont={bf,color=lcink},justification=raggedright,singlelinecheck=false}
\AtBeginEnvironment{quote}{\color{lcink2}}
"""


def tex_cover(P) -> str:
    m, o = P["meta"], P["meta"]["outcome"]
    kv = lambda k, v: f"\\textcolor{{lcmuted}}{{\\scriptsize\\MakeUppercase{{{k}}}}}\\newline{{\\small\\bfseries {tex_esc(v)}}}"  # noqa: E731
    head = f"Decision: {OUTCOME[o][0]}" if o else m["status"]
    box = OUTCOME[o][2] if o else "lcaccent"
    return (r"""
\thispagestyle{plain}
\begin{tcolorbox}[enhanced,colback=lcnavy,colframe=lcnavy,arc=6pt,boxrule=0pt,left=16pt,right=16pt,top=14pt,bottom=16pt]
\color{white}{\small\bfseries LabCrew Lite}\hfill\colorbox{lcsim}{\textcolor{lcsimink}{\scriptsize\bfseries SIMULATED / FICTIONAL DATA}}\par\vspace{14pt}
{\scriptsize\bfseries\textcolor{lcline}{GWAS ANALYSIS REPORT}}\par\vspace{2pt}
{\fontsize{24}{28}\selectfont\bfseries """ + tex_esc(head) + r"""}\par\vspace{6pt}
{\large\textcolor{lcline}{``""" + tex_esc(m["question"]) + r"""''}}
\end{tcolorbox}
\vspace{4pt}
{\renewcommand{\arraystretch}{1.5}\begin{tabularx}{\linewidth}{@{}XXXX@{}}
""" + " & ".join([kv("Session", m["session"]), kv("Domain", "GWAS (simulated)"), kv("Status", m["status"]), kv("Reviewer", m["reviewer"])])
            + " \\\\\n" + " & ".join([kv("Started", m["started"]), kv("Generated", m["generated"]), kv("Rules file", m["rules"]),
                                      kv("Report sha256", m["sha"][:16])]) + r""" \\
\end{tabularx}}
\vspace{2pt}{\color{lcline}\hrule}\vspace{8pt}
\begin{tcolorbox}[enhanced,colback=""" + ("lcsoft" if box == "lcaccent" else box + "bg") + ",colframe=" + box
            + r""",boxrule=0pt,leftrule=4pt,arc=3pt,left=12pt,top=8pt,bottom=8pt]
{\scriptsize\bfseries\textcolor{""" + box + "}{" + ("FINAL DECISION \\textperiodcentered\\ CODE + VERSIONED RULES" if o else "STATUS") + r"""}}\par
{\Large\bfseries """ + tex_esc(f"{OUTCOME[o][0]} · {m['status']}" if o else m["status"]) + r"""}\par
{\small\color{lcink2} """ + tex_esc(m["stop"] or ("" if o else "No round has reached the reviewer yet.")) + r"""}
\end{tcolorbox}
""")


def render_pdf(P) -> bytes:
    pandoc, engine = shutil.which("pandoc"), shutil.which("xelatex")
    if not (pandoc and engine):
        raise RuntimeError("PDF export needs pandoc and xelatex (LaTeX) on the server")
    m, last, L = P["meta"], P["last"], []
    L.append(raw_tex(tex_cover(P)))

    # 1. summary
    L.append("# Executive summary\n\n")
    if last:
        g, rng = last["gwas"], P["lambda_range"]
        kp = [["Genomic inflation λ", na(g["lambda_gc"])], ["Allowed λ range", f"{rng[0]} – {rng[1]}" if rng else "—"],
              ["Bonferroni hits", na(g["n_hits"])], ["Threshold", na(g["bonferroni_threshold"])],
              ["Variants tested", f"{g['n_tested']} of {g['n_variants']}"],
              ["Removed by QC", ", ".join(f"{k} {v}" for k, v in g["qc_failed_by"].items())],
              ["Rounds run", f"{len(P['reps'])} of {m['rounds_max']}"]]
        L.append("## Key results (latest round)\n\n" + md_table(["Measure", "Value"], kp, right_numbers=False))
        L.append("## Decision by round\n\n" + md_table(ROUND_HEAD, round_rows(P)))
    for r in P["rounds"]:
        if r.get("error"):
            L.append(f"> **Round {r['round']} stopped before reaching the reviewer.** {md_esc(r['error'])}\n\n")
    if tr := truth_rows(P):
        L.append("## Against the planted truth\n\nPossible only because the data are simulated.\n\n"
                 + md_table(["Measure", "Value"], tr, right_numbers=False))

    with tempfile.TemporaryDirectory(prefix="lcl_pdf_") as tmp:
        tmp_p = Path(tmp)
        # 2. figures + top variants
        figs = charts(P)
        if figs:
            L.append(raw_tex("\\Needspace{0.5\\textheight}") + "# Figures\n\n")
            for f in figs:
                shutil.copy(f["file"], tmp_p / f"{f['id']}.png")
                L.append(f"![{md_esc(f['caption'])}]({f['id']}.png){{width=100%}}\n\n")
        if top := top_rows(P):
            L.append("# Top variants by posterior inclusion probability\n\n"
                     "Latest analysis, best first. Values copied from the R engine output (run_gwas).\n\n" + md_table(TOP_HEAD, top))

        # 3. round reports, verbatim
        if P["reps"]:
            L.append("# Round-by-round reports\n\nEach block is the content of the reviewed report for that round (as checked by the "
                     "Reviewer agent and the number check). Only formatting differs; the hash-checked source is the Markdown.\n\n")
        for r in P["reps"]:
            nm = f"Round {r['round']}"
            L.append(raw_tex(f"\\subsection[{nm}]{{{nm}\\quad{tex_badge(r['decision']['outcome'])}}}") + "\n"
                     + defuse_raw(demote(pretty_md(drop_title(r["report_md"])), 1)) + "\n\n"
                     + raw_tex("{\\scriptsize\\color{lcmuted}\\texttt{report sha256 ", tex_esc(r["report_sha256"]),
                               "}}\\par\\medskip{\\color{lcline}\\hrule}"))

        # 4. sources
        cites, conflicts, quar = sources(P)
        if cites or conflicts or quar:
            L.append("# Sources\n\n")
            for k, v in conflicts.items():
                L.append(f"> **SOP version conflict ({md_esc(k)}).** {md_esc(v)}\n\n")
            if quar:
                L.append("> **Held back from the agents (possible prompt injection):** "
                         + md_esc(", ".join(f"{d} ({', '.join(f)})" for d, f in quar.items())) + "\n\n")
            if cites:
                L.append(md_table(["#", "Source", "Excerpt"], [[str(i), c["citation"], c["text"]] for i, c in enumerate(cites, 1)],
                                  right_numbers=False))

        # 5. appendices
        L.append(raw_tex("\\clearpage") + "# Appendix A: Functional assay log (simulated)\n\n"
                 + (md_table(ASSAY_HEAD, assay_rows(P)) or "No variant has been sent to the assay yet.\n\n"))
        L.append("# Appendix B: Audit trail\n\n## Human decisions\n\n"
                 + (md_table(AUDIT_HEAD, audit_rows(P)) or "No decisions yet.\n\n")
                 + "## Tool calls (every calculation, with versions)\n\n" + (md_table(TOOL_HEAD, tool_rows(P)) or "None.\n\n"))
        L.append("\n" + raw_tex("\\vfill{\\footnotesize\\color{lcmuted}\\textbf{Integrity.} ", tex_esc(INTEGRITY),
                                " Final report sha256: \\texttt{", tex_esc(m["sha"]), "}.\\par}"))

        md_file, hdr, out = tmp_p / "report.md", tmp_p / "header.tex", tmp_p / "report.pdf"
        md_file.write_text("".join(L), encoding="utf-8")
        hdr.write_text(tex_header(m), encoding="utf-8")
        fonts = pdf_fonts()
        args = [pandoc, str(md_file), "-f", "markdown-tex_math_dollars-raw_tex-raw_html-auto_identifiers-smart+raw_attribute",
                "-o", str(out), f"--pdf-engine={engine}", "-H", str(hdr), "--resource-path", str(tmp_p),
                "--number-sections", "-V", "secnumdepth=2", "-V", "geometry:a4paper,margin=20mm,top=22mm,bottom=22mm,headsep=8pt",
                "-V", f"mainfont={fonts['main']}", "-V", f"sansfont={fonts['main']}", "-V", f"monofont={fonts['mono']}",
                "-V", "fontsize=10pt", "-V", "colorlinks=true", "-V", "linkcolor=lcaccent", "--columns=10"]   # every table full width
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=tmp)
        if not out.exists():
            raise RuntimeError("PDF build failed: " + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-12:]))
        return out.read_bytes()
