"""Render site/index.html from the tagged articles CSV and brief.md.

Input order: data/articles.csv, else data/sample_articles.csv (override with --data).
Runtime deps: stdlib, pandas, jinja2.
"""
import argparse
import datetime as dt
import html
import re
import sys
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent
DATA_CANDIDATES = [ROOT / "data" / "articles.csv", ROOT / "data" / "sample_articles.csv"]
BRIEF_PATH = ROOT / "brief.md"
EVAL_CANDIDATES = [ROOT / "eval" / "score_output.txt", ROOT / "eval" / "score.txt",
                   ROOT / "eval" / "scores.txt", ROOT / "eval" / "accuracy.txt"]
RUN_LOG = ROOT / "runs" / "log.csv"

SENTIMENTS = ["positive", "neutral", "negative"]
TOPIC_LABELS = {
    "results": "Results",
    "tencent_stake": "Tencent stake",
    "ai": "AI",
    "jet_integration": "Just Eat Takeaway",
    "ventures_investments": "Ventures & investments",
    "governance_regulation": "Governance & regulation",
    "other": "Other",
}

# ---------- data ----------

def pick_data(cli_path):
    if cli_path:
        return Path(cli_path)
    for p in DATA_CANDIDATES:
        if p.exists():
            return p
    sys.exit("No input CSV: expected data/articles.csv or data/sample_articles.csv")


def load(path):
    df = pd.read_csv(path, keep_default_na=False, dtype=str)
    for col in ("topic", "sentiment", "company"):
        df[col] = df[col].str.strip().str.lower()
    df["published_dt"] = pd.to_datetime(df["published"], errors="coerce")
    return df


def fmt_day(d):
    return f"{d.day} {d.strftime('%b')}"


def pct(n, total):
    return round(100 * n / total) if total else None


def largest_remainder(counts, total):
    """Whole-number percentages that add to 100 (largest-remainder method)."""
    if not total:
        return {k: None for k in counts}
    raw = {k: 100 * c / total for k, c in counts.items()}
    out = {k: int(v) for k, v in raw.items()}
    short = 100 - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), list(counts).index(k)))[:short]:
        out[k] += 1
    return out


def compute(df):
    prosus = df[df["company"] == "prosus"]
    peers = df[df["company"].str.startswith("peer_")]
    n_p, n_peer = len(prosus), len(peers)

    dates = prosus["published_dt"].dropna()
    if len(dates):
        start, end = dates.min(), dates.max()
        if start.year != end.year:
            span = f"{fmt_day(start)} {start.year} to {fmt_day(end)} {end.year}"
        else:
            span = f"{fmt_day(start)} to {fmt_day(end)} {end.year}"
    else:
        span = "no dates yet"

    valid = (prosus["sentiment"].isin(SENTIMENTS) & prosus["topic"].isin(TOPIC_LABELS)
             & prosus["published_dt"].notna())
    ok = prosus[valid]  # ONE valid set: charts and sentiment split all use it
    n_excluded = n_p - len(ok)
    n_valid = len(ok)
    sent_counts = {s: int((ok["sentiment"] == s).sum()) for s in SENTIMENTS}
    sent_pct = largest_remainder(sent_counts, n_valid)

    sov = pct(n_p, n_p + n_peer)

    if n_p:
        oc = prosus["outlet"].value_counts()
        top = sorted(oc.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        top_outlet = {"name": top[0], "count": int(top[1])}
    else:
        top_outlet = None

    return {
        "span": span,
        "n_excluded": n_excluded,
        "n_valid": n_valid,
        "n_prosus": n_p,
        "n_peers": n_peer,
        "sent_counts": sent_counts,
        "sent_pct": sent_pct,
        "sov": sov,
        "top_outlet": top_outlet,
        "weekly": weekly_chart(ok),
        "topics": topic_chart(ok),
    }


# ---------- charts (geometry computed here, drawn in the template) ----------

def weekly_chart(prosus):
    W, H = 600, 344
    left, right, top, bottom = 44, 10, 64, 70
    p = prosus
    if p.empty:
        return {"empty": True, "W": W, "H": H}
    monday = p["published_dt"] - pd.to_timedelta(p["published_dt"].dt.weekday, unit="D")
    p = p.assign(week=monday.dt.normalize())
    weeks = pd.date_range(p["week"].min(), p["week"].max(), freq="7D")  # includes empty weeks
    counts = {w: {s: int(((p["week"] == w) & (p["sentiment"] == s)).sum()) for s in SENTIMENTS}
              for w in weeks}
    max_total = max(sum(c.values()) for c in counts.values()) or 1
    step = nice_step(max_total)
    y_max = step * -(-max_total // step)
    plot_h = H - top - bottom
    plot_w = W - left - right
    slot = plot_w / len(weeks)
    bar_w = min(70, slot * 0.6)
    bars = []
    for i, w in enumerate(weeks):
        x = left + slot * i + (slot - bar_w) / 2
        y = H - bottom
        segs = []
        for s in SENTIMENTS:
            h = plot_h * counts[w][s] / y_max
            if h > 0:
                y -= h
                segs.append({"cls": s, "x": round(x, 1), "y": round(y, 1), "w": round(bar_w, 1),
                             "h": round(h, 1), "n": counts[w][s]})
        total = sum(counts[w].values())
        bars.append({"segs": segs, "label": f"{fmt_day(w)}", "cx": round(x + bar_w / 2, 1),
                     "total": total, "ty": round(y - 6, 1)})
    ticks = [{"v": v, "y": round(H - bottom - plot_h * v / y_max, 1)}
             for v in range(0, int(y_max) + 1, int(step))]
    return {"empty": False, "W": W, "H": H, "left": left, "right": W - right,
            "base": H - bottom, "bars": bars, "ticks": ticks, "label_y": H - bottom + 26,
            "label_every": 1 if slot >= 80 else -(-80 // int(slot))}


def nice_step(m):
    for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500):
        if m / s <= 5:
            return s
    return 1000


def topic_chart(prosus):
    W = 600
    label_w, row_h, top = 270, 40, 10
    counts = [(k, int((prosus["topic"] == k).sum())) for k in TOPIC_LABELS]
    counts.sort(key=lambda kv: (-kv[1], list(TOPIC_LABELS).index(kv[0])))
    max_c = max((c for _, c in counts), default=0) or 1
    plot_w = W - label_w - 50
    rows = []
    for i, (k, c) in enumerate(counts):
        y = top + i * row_h
        w = plot_w * c / max_c
        rows.append({"label": TOPIC_LABELS[k], "count": c, "y": y, "w": round(w, 1),
                     "ty": y + row_h / 2 + 7, "cx": round(label_w + w + 8, 1)})
    return {"W": W, "H": top * 2 + row_h * len(rows), "label_w": label_w,
            "bar_h": row_h - 12, "rows": rows, "empty": len(prosus) == 0}


# ---------- brief.md: tiny markdown subset ----------

INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")


def inline(text):
    out, pos = [], 0
    for m in INLINE_LINK.finditer(text):
        out.append(bold(html.escape(text[pos:m.start()])))
        href = m.group(2)
        if not re.match(r"^(https?://|#|/|\./|[\w-]+\.html?$)", href):
            href = "#"
        out.append(f'<a href="{html.escape(href, quote=True)}">{bold(html.escape(m.group(1)))}</a>')
        pos = m.end()
    out.append(bold(html.escape(text[pos:])))
    return "".join(out)


def bold(escaped):
    return INLINE_BOLD.sub(r"<strong>\1</strong>", escaped)


def render_md(md):
    """Headings (#..####), paragraphs, - / * / 1. lists, **bold**, [links](href).
    A top-level '# ' heading is dropped: the page supplies the section title."""
    lines = md.splitlines()
    out, para, list_tag = [], [], None

    def flush_para():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_para(); close_list(); continue
        h = re.match(r"^(#{1,4})\s+(.*)$", line)
        ul = re.match(r"^[-*]\s+(.*)$", line)
        ol = re.match(r"^\d+[.)]\s+(.*)$", line)
        if h:
            flush_para(); close_list()
            level = len(h.group(1))
            if level == 1:
                continue
            out.append(f"<h{level + 1}>{inline(h.group(2))}</h{level + 1}>")
        elif ul or ol:
            flush_para()
            tag = "ul" if ul else "ol"
            if list_tag != tag:
                close_list(); out.append(f"<{tag}>"); list_tag = tag
            out.append(f"<li>{inline((ul or ol).group(1))}</li>")
        else:
            close_list(); para.append(line)
    flush_para(); close_list()
    return "\n".join(out)


def load_brief():
    """Returns (html, is_placeholder). Never creates brief.md."""
    if not BRIEF_PATH.exists():
        return "<p>Brief pending.</p>", False
    md = BRIEF_PATH.read_text(encoding="utf-8")
    return render_md(md), "[Placeholder]" in md


# ---------- footer lines ----------

def accuracy_line():
    for p in EVAL_CANDIDATES:
        if p.exists():
            text = " · ".join(l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip())
            if text:
                return f"Accuracy: {text}"
    return "Accuracy: pending Jack's review of 50 labelled rows"


def cost_line(today):
    if not RUN_LOG.exists():
        return "Running cost: pending first full run"
    log = pd.read_csv(RUN_LOG, keep_default_na=False)
    if "usd" not in log.columns or "date" not in log.columns or log.empty:
        return "Running cost: pending first full run"
    d = pd.to_datetime(log["date"], errors="coerce")
    usd = pd.to_numeric(log["usd"], errors="coerce")
    mask = (d >= pd.Timestamp(today) - pd.Timedelta(days=30)) & usd.notna()
    if not mask.any():
        return "Running cost: pending first full run"
    return f"Running cost: USD {usd[mask].sum():.2f} over the last 30 days (AI tagging, from the run log)"


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="input CSV (default: articles.csv, else sample_articles.csv)")
    ap.add_argument("--out", default=str(ROOT / "site" / "index.html"))
    args = ap.parse_args()

    data_path = pick_data(args.data)
    df = load(data_path)
    ctx = compute(df)
    brief_html, brief_placeholder = load_brief()
    ctx.update(
        sample_banner=(data_path.name == "sample_articles.csv"
                       or ("summary" in df and bool(df["summary"].str.startswith("SYNTHETIC SAMPLE").any()))),
        n_rows=len(df),
        brief_html=Markup(brief_html),
        brief_placeholder=brief_placeholder,
        accuracy=accuracy_line(),
        cost=cost_line(dt.date.today()),
        sentiments=SENTIMENTS,
    )
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    page = env.get_template("page.html.j2").render(**ctx)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"{data_path.name}: {len(df)} rows -> {out} ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
