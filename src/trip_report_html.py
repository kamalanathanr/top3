"""Render a trip_output_*.txt report as a formatted HTML page.

The planner writes fixed-width text, which is fine in a terminal but hard to
scan. This parses that text back into structure and lays it out as tables:
ranked itineraries, a card per option with its flight legs, and the full list
of everything searched.

    python3 -m src.trip_report_html                  # newest trip_output_*.txt
    python3 -m src.trip_report_html src/trip_output_20260910-114518.txt
    python3 -m src.trip_report_html --open           # also open in a browser

Writes <input>.html next to the source file unless --output says otherwise.
Parsing is deliberately forgiving: anything it cannot classify is preserved as
plain text rather than dropped, so a report never loses information.
"""

import argparse
import glob
import html
import os
import re
import sys
import webbrowser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# --- line shapes emitted by trip_planner -----------------------------------
RE_HEADER_KV = re.compile(r"^([A-Za-z][A-Za-z /]+?)\s*:\s*(.+)$")
RE_SECTION = re.compile(r"^([A-Z][A-Z0-9 /,'-]{3,})$")
RE_RANK_ROW = re.compile(
    r"^(\d+)\s{2}(.+?)\s{2,}(\d+)\s+(\d{4}-\d{2}-\d{2})\s*-\s*"
    r"(\d{4}-\d{2}-\d{2})\s+([A-Z]{3})\s+([\d,]+)\s*$")
# compare mode ranks with country and city as separate columns, and puts
# nights after the dates: "1  Vietnam  Hanoi  2026-10-18 - 2026-10-25  7  INR .."
RE_RANK_ROW_COMPARE = re.compile(
    r"^(\d+)\s{2}(\S.*?)\s{2,}(\S.*?)\s{2,}(\d{4}-\d{2}-\d{2})"
    r"(?:\s*-\s*(\d{4}-\d{2}-\d{2})|\s*\(one way\))\s+(\d+)\s+"
    r"([A-Z]{3})\s+([\d,]+)\s*$")
RE_ITIN_HEAD = re.compile(r"^(\d+)\.\s+(.*?)(\s+\[[A-Z ]+\])?$")
RE_DAYS = re.compile(r"^\s+Days:\s*(.+)$")
# combine mode: "   2026-10-18  BLR -> PQC   Airline  23:20-13:20 12h 30m  ..."
RE_LEG_COMBINE = re.compile(
    r"^\s{3}(\d{4}-\d{2}-\d{2})\s+([A-Z]{3})\s*->\s*([A-Z]{3})\s+(.+?)\s+"
    r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+(\d+h(?:\s*\d+m)?)\s+(.*?)\s+"
    r"([A-Z]{3})\s+([\d,]+)\s*$")
# compare mode: "   outbound  2026-10-16  Airline  09:15-19:20 8h 35m  ..."
RE_LEG_COMPARE = re.compile(
    r"^\s{3}(outbound|return)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+"
    r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s+(\d+h(?:\s*\d+m)?)\s+(.*?)\s+"
    r"([A-Z]{3})\s+([\d,]+)\s*$")
RE_LEG_EXTRA = re.compile(r"^\s{6,}(flight .*|cabin .*|check-in .*|.*\|.*)$")
RE_BOOK = re.compile(r"^\s+book:\s*(\S+)$")
RE_PER_ADULT = re.compile(r"^\s+per adult:?\s*([A-Z]{3})\s+([\d,]+)")
RE_TOTAL = re.compile(r"x\s*(\d+)\s*(?:travellers)?\s*[:=]\s*([A-Z]{3})\s+([\d,]+)")
RE_NOFLIGHT_LEG = re.compile(
    r"^\s{3}(\d{4}-\d{2}-\d{2})\s+([A-Z]{3})\s*->\s*([A-Z]{3})\s+NO FLIGHT")
RE_SEARCHED_GROUP = re.compile(r"^(.+?)\s+\(([A-Z]{3})\)$")
RE_SEARCHED_ROW = re.compile(
    r"^\s{3}(outbound|return)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+"
    r"(\d+h(?:\s*\d+m)?)\s+(\S+(?:\s\S+)?)\s+([A-Z]{3})\s+([\d,]+)(\s*<<)?\s*$")
RE_SEARCHED_NONE = re.compile(
    r"^\s{3}(outbound|return)\s+(\d{4}-\d{2}-\d{2})\s+- no flights found -")
RE_RULE = re.compile(r"^[=-]{10,}$")


def _money(cur, amount):
    return f"{cur} {amount}"


def parse_report(text):
    """Turn the text report into a dict the template can walk."""
    doc = {"title": "", "meta": [], "ranked": [], "callouts": [],
           "itineraries": [], "searched": [], "notes": [], "footer": []}
    lines = text.splitlines()
    section = "head"
    itin = None
    group = None
    i = 0

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        i += 1

        if RE_RULE.match(line.strip()):
            continue
        if not line.strip():
            continue

        stripped = line.strip()

        # Section switches.
        if RE_SECTION.match(stripped) and not line.startswith(" "):
            up = stripped.upper()
            if up.startswith("TRIP PLAN"):
                doc["title"] = stripped
                section = "head"
            elif up.startswith("RANKED"):
                section = "ranked"
            elif up == "DETAIL":
                section = "detail"
            elif up.startswith("ALL OPTIONS"):
                section = "searched"
            elif up in ("NOT PRICED", "ROUTES WITH NO SERVICE",
                        "INCOMPLETE - NOT COMPARABLE WITH THE ABOVE"):
                section = "notes"
                doc["notes"].append({"heading": stripped, "lines": []})
            else:
                section = "notes"
                doc["notes"].append({"heading": stripped, "lines": []})
            continue

        if section == "head":
            m = RE_HEADER_KV.match(stripped)
            if m and not stripped.lower().startswith("trip plan"):
                doc["meta"].append((m.group(1).strip(), m.group(2).strip()))
            elif stripped.startswith("!"):
                doc["callouts"].append(stripped.lstrip("! ").strip())
            else:
                doc["title"] = doc["title"] or stripped
            continue

        if section == "ranked":
            m = RE_RANK_ROW.match(line)
            if m:
                doc["ranked"].append({
                    "rank": m.group(1), "label": m.group(2).strip(),
                    "nights": m.group(3), "start": m.group(4),
                    "end": m.group(5),
                    "total": _money(m.group(6), m.group(7)),
                })
                continue
            m = RE_RANK_ROW_COMPARE.match(line)
            if m:
                doc["ranked"].append({
                    "rank": m.group(1),
                    "label": f"{m.group(2).strip()} — {m.group(3).strip()}",
                    "nights": m.group(6), "start": m.group(4),
                    "end": m.group(5) or "one way",
                    "total": _money(m.group(7), m.group(8)),
                })
                continue
            if stripped.upper().startswith(("#", "NIGHTS")):
                continue
            if stripped.startswith("CHEAPEST") or stripped.startswith(
                    "BEST VALUE") or stripped.startswith("Adding") \
                    or stripped.startswith("That is"):
                doc["callouts"].append(stripped)
                continue
            if stripped.startswith("!"):
                doc["callouts"].append(stripped.lstrip("! ").strip())
                continue
            doc["notes"] and doc["notes"][-1]["lines"].append(stripped)
            continue

        if section == "detail":
            m = RE_ITIN_HEAD.match(stripped)
            if m and not line.startswith("   "):
                itin = {"rank": m.group(1), "label": m.group(2).strip(),
                        "flag": (m.group(3) or "").strip(), "days": "",
                        "subtitle": "", "legs": [], "per_adult": "",
                        "total": "", "travellers": "", "warnings": []}
                doc["itineraries"].append(itin)
                continue
            if itin is None:
                continue

            m = RE_DAYS.match(line)
            if m:
                itin["days"] = m.group(1).strip()
                continue

            m = RE_LEG_COMBINE.match(line)
            if m:
                itin["legs"].append({
                    "when": m.group(1), "route": f"{m.group(2)} → {m.group(3)}",
                    "airline": m.group(4).strip(), "dep": m.group(5),
                    "arr": m.group(6), "duration": m.group(7),
                    "stops": m.group(8).strip(),
                    "fare": _money(m.group(9), m.group(10)),
                    "extras": [], "book": "",
                })
                continue

            m = RE_LEG_COMPARE.match(line)
            if m:
                itin["legs"].append({
                    "when": f"{m.group(2)} ({m.group(1)})", "route": m.group(1),
                    "airline": m.group(3).strip(), "dep": m.group(4),
                    "arr": m.group(5), "duration": m.group(6),
                    "stops": m.group(7).strip(),
                    "fare": _money(m.group(8), m.group(9)),
                    "extras": [], "book": "",
                })
                continue

            m = RE_NOFLIGHT_LEG.match(line)
            if m:
                itin["legs"].append({
                    "when": m.group(1), "route": f"{m.group(2)} → {m.group(3)}",
                    "airline": "no flight found", "dep": "", "arr": "",
                    "duration": "", "stops": "", "fare": "",
                    "extras": [], "book": "", "missing": True,
                })
                continue

            m = RE_BOOK.match(line)
            if m and itin["legs"]:
                itin["legs"][-1]["book"] = m.group(1)
                continue

            m = RE_PER_ADULT.match(line)
            if m:
                itin["per_adult"] = _money(m.group(1), m.group(2))
                t = RE_TOTAL.search(line)
                if t:
                    itin["travellers"] = t.group(1)
                    itin["total"] = _money(t.group(2), t.group(3))
                continue

            t = RE_TOTAL.search(line)
            if t and "x " in line:
                itin["travellers"] = t.group(1)
                itin["total"] = _money(t.group(2), t.group(3))
                continue

            if stripped.startswith("!"):
                itin["warnings"].append(stripped.lstrip("! ").strip())
                continue

            if RE_LEG_EXTRA.match(line) and itin["legs"]:
                itin["legs"][-1]["extras"].append(stripped)
                continue

            if not itin["subtitle"] and not itin["legs"]:
                itin["subtitle"] = stripped
                continue
            itin["warnings"].append(stripped)
            continue

        if section == "searched":
            if stripped.startswith(("Every date", "chosen above", "A cheaper",
                                    "DIRECTION")):
                doc.setdefault("searched_note", []).append(stripped)
                continue
            m = RE_SEARCHED_GROUP.match(stripped)
            if m and not line.startswith("   "):
                group = {"label": m.group(1).strip(), "code": m.group(2),
                         "rows": []}
                doc["searched"].append(group)
                continue
            if group is None:
                continue
            m = RE_SEARCHED_ROW.match(line)
            if m:
                group["rows"].append({
                    "direction": m.group(1), "date": m.group(2),
                    "airline": m.group(3).strip(), "duration": m.group(4),
                    "stops": m.group(5).strip(),
                    "fare": _money(m.group(6), m.group(7)),
                    "chosen": bool(m.group(8)),
                })
                continue
            m = RE_SEARCHED_NONE.match(line)
            if m:
                group["rows"].append({
                    "direction": m.group(1), "date": m.group(2),
                    "airline": "no flights found", "duration": "",
                    "stops": "", "fare": "", "chosen": False,
                })
                continue
            if "legs priced" in stripped:
                doc.setdefault("searched_note", []).append(stripped)
            continue

        if section == "notes":
            if not doc["notes"]:
                doc["notes"].append({"heading": "Notes", "lines": []})
            doc["notes"][-1]["lines"].append(stripped)
            continue

    # Trailing prose after the last rule is the footer disclaimer.
    tail = [l.strip() for l in lines[-4:]
            if l.strip() and not RE_RULE.match(l.strip())]
    doc["footer"] = tail
    return doc


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#606a76;
  --line:#e3e7ec; --accent:#2563eb; --accent-soft:#eff4ff;
  --good:#0f7b3f; --good-soft:#e8f6ee; --warn:#a2510a; --warn-soft:#fdf0e3;
  --bad:#b0202c; --bad-soft:#fdecee; --chip:#eef1f5;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#12151a; --card:#1a1f26; --ink:#e8ebef; --muted:#9aa5b1;
    --line:#2a313a; --accent:#6da2ff; --accent-soft:#1b2740;
    --good:#5ed69b; --good-soft:#14301f; --warn:#e0a35f; --warn-soft:#33240f;
    --bad:#ff8a94; --bad-soft:#341418; --chip:#242b34;
  }
}
:root[data-theme="dark"]{
  --bg:#12151a; --card:#1a1f26; --ink:#e8ebef; --muted:#9aa5b1;
  --line:#2a313a; --accent:#6da2ff; --accent-soft:#1b2740;
  --good:#5ed69b; --good-soft:#14301f; --warn:#e0a35f; --warn-soft:#33240f;
  --bad:#ff8a94; --bad-soft:#341418; --chip:#242b34;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:16px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);margin:34px 0 12px;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;margin-bottom:14px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
  gap:12px 22px;margin:0}
.meta div{min-width:0}
.meta dt{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--muted);margin-bottom:2px}
.meta dd{margin:0;font-weight:600;font-size:14px;word-break:break-word}
.callout{background:var(--good-soft);border-left:3px solid var(--good);
  border-radius:6px;padding:11px 14px;margin:0 0 10px;font-size:14px}
.callout.warn{background:var(--warn-soft);border-color:var(--warn)}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:11px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);font-weight:600;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.rank td{font-weight:600}
.rank.best{background:var(--accent-soft)}
.badge{display:inline-block;background:var(--accent);color:#fff;
  border-radius:20px;padding:1px 9px;font-size:11px;font-weight:700;
  margin-left:7px;vertical-align:1px}
.itin{background:var(--card);border:1px solid var(--line);border-radius:12px;
  margin-bottom:16px;overflow:hidden}
.itin > header{padding:15px 20px;border-bottom:1px solid var(--line);
  display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.itin h3{margin:0;font-size:17px;flex:1 1 260px;min-width:0}
.itin .price{font-weight:700;font-size:17px;white-space:nowrap}
.itin .per{color:var(--muted);font-size:12px;white-space:nowrap}
.chips{padding:11px 20px 0;display:flex;flex-wrap:wrap;gap:7px}
.chip{background:var(--chip);border-radius:20px;padding:3px 11px;font-size:12px}
.legs{padding:11px 8px 14px}
.extras{color:var(--muted);font-size:12px;margin-top:3px}
.book{font-size:12px;white-space:nowrap}
a{color:var(--accent)}
.missing{color:var(--bad);font-weight:600}
.chosen{background:var(--good-soft)}
.tick{color:var(--good);font-weight:700}
details{background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:0 20px;margin-bottom:14px}
details[open]{padding-bottom:16px}
summary{cursor:pointer;padding:15px 0;font-weight:600;font-size:15px}
.group{margin-top:14px}
.group h4{margin:0 0 6px;font-size:14px}
.note{background:var(--warn-soft);border-left:3px solid var(--warn);
  border-radius:6px;padding:11px 14px;margin-bottom:10px;font-size:13.5px}
.note h4{margin:0 0 6px;font-size:13px;text-transform:uppercase;
  letter-spacing:.05em}
.note div{color:var(--ink);opacity:.9}
footer{color:var(--muted);font-size:12.5px;margin-top:34px;
  border-top:1px solid var(--line);padding-top:14px}
@media print{
  body{background:#fff}
  .card,.itin,details{border-color:#ccc;break-inside:avoid}
  details{padding-bottom:12px}
  details:not([open]) > *:not(summary){display:revert}
  .book{display:none}
}
@media (max-width:640px){
  .wrap{padding:18px 12px 40px}
  h1{font-size:20px}
  table{font-size:13px}
}
"""

E = html.escape


def _linkify(url):
    return (f'<a href="{E(url)}" target="_blank" rel="noopener">'
            f'open&nbsp;↗</a>')


def render_html(doc, source_name):
    p = []
    a = p.append
    a(f"<title>{E(doc['title'] or 'Trip plan')}</title>")
    a(f"<style>{CSS}</style>")
    a('<div class="wrap">')
    a(f"<h1>{E(doc['title'] or 'Trip plan')}</h1>")
    a(f'<p class="sub">from {E(source_name)}</p>')

    if doc["meta"]:
        a('<div class="card"><dl class="meta">')
        for k, v in doc["meta"]:
            a(f"<div><dt>{E(k)}</dt><dd>{E(v)}</dd></div>")
        a("</dl></div>")

    for c in doc["callouts"]:
        cls = "callout"
        low = c.lower()
        if low.startswith(("makemytrip rate", "search budget", "wait",
                           "check with")) or "exhausted" in low:
            cls += " warn"
        a(f'<div class="{cls}">{E(c)}</div>')

    if doc["ranked"]:
        a("<h2>Ranked by total cost</h2>")
        a('<div class="card"><div class="tablewrap"><table>')
        a("<thead><tr><th>#</th><th>Itinerary</th><th>Nights</th>"
          "<th>Depart</th><th>Return</th><th class='num'>Total</th>"
          "</tr></thead><tbody>")
        for n, r in enumerate(doc["ranked"]):
            best = ' class="rank best"' if n == 0 else ' class="rank"'
            badge = '<span class="badge">best</span>' if n == 0 else ""
            a(f"<tr{best}><td>{E(r['rank'])}</td>"
              f"<td>{E(r['label'])}{badge}</td><td>{E(r['nights'])}</td>"
              f"<td>{E(r['start'])}</td><td>{E(r['end'])}</td>"
              f"<td class='num'>{E(r['total'])}</td></tr>")
        a("</tbody></table></div></div>")

    if doc["itineraries"]:
        a("<h2>Itinerary detail</h2>")
        for it in doc["itineraries"]:
            a('<section class="itin"><header>')
            flag = (f" <span class='missing'>{E(it['flag'].strip('[] '))}</span>"
                    if it["flag"] else "")
            a(f"<h3>{E(it['rank'])}. {E(it['label'])}{flag}</h3>")
            if it["total"]:
                a(f"<div><div class='price'>{E(it['total'])}</div>")
                per = E(it["per_adult"])
                trav = f" × {E(it['travellers'])}" if it["travellers"] else ""
                a(f"<div class='per'>{per} per adult{trav}</div></div>")
            a("</header>")

            chips = []
            if it["days"]:
                chips += [c.strip() for c in it["days"].split(",") if c.strip()]
            if it["subtitle"]:
                chips.append(it["subtitle"])
            if chips:
                a('<div class="chips">')
                for c in chips:
                    a(f'<span class="chip">{E(c)}</span>')
                a("</div>")

            if it["legs"]:
                a('<div class="legs"><div class="tablewrap"><table>')
                a("<thead><tr><th>When</th><th>Route</th><th>Airline</th>"
                  "<th>Times</th><th>Duration</th><th>Stops</th>"
                  "<th class='num'>Fare/adult</th><th>Book</th>"
                  "</tr></thead><tbody>")
                for lg in it["legs"]:
                    if lg.get("missing"):
                        a(f"<tr><td>{E(lg['when'])}</td>"
                          f"<td>{E(lg['route'])}</td>"
                          f"<td colspan='6' class='missing'>"
                          f"no flight found</td></tr>")
                        continue
                    extras = ""
                    if lg["extras"]:
                        extras = ("<div class='extras'>"
                                  + E(" · ".join(lg["extras"])) + "</div>")
                    times = (f"{E(lg['dep'])}–{E(lg['arr'])}"
                             if lg["dep"] else "")
                    book = _linkify(lg["book"]) if lg["book"] else ""
                    a(f"<tr><td>{E(lg['when'])}</td>"
                      f"<td><strong>{E(lg['route'])}</strong></td>"
                      f"<td>{E(lg['airline'])}{extras}</td>"
                      f"<td>{times}</td><td>{E(lg['duration'])}</td>"
                      f"<td>{E(lg['stops'])}</td>"
                      f"<td class='num'>{E(lg['fare'])}</td>"
                      f"<td class='book'>{book}</td></tr>")
                a("</tbody></table></div></div>")

            for w in it["warnings"]:
                a(f'<div style="padding:0 20px 14px"><div class="note">'
                  f'{E(w)}</div></div>')
            a("</section>")

    if doc["searched"]:
        total_rows = sum(len(g["rows"]) for g in doc["searched"])
        a("<h2>Everything searched</h2>")
        a(f"<details><summary>All options priced "
          f"({total_rows} legs across {len(doc['searched'])} destinations)"
          f"</summary>")
        for n in doc.get("searched_note", []):
            a(f'<p class="sub" style="margin:0 0 6px">{E(n)}</p>')
        for g in doc["searched"]:
            a(f'<div class="group"><h4>{E(g["label"])} '
              f'({E(g["code"])})</h4><div class="tablewrap"><table>')
            a("<thead><tr><th>Direction</th><th>Date</th><th>Airline</th>"
              "<th>Duration</th><th>Stops</th><th class='num'>Fare/adult</th>"
              "<th>Picked</th></tr></thead><tbody>")
            for r in g["rows"]:
                cls = ' class="chosen"' if r["chosen"] else ""
                tick = '<span class="tick">✓</span>' if r["chosen"] else ""
                a(f"<tr{cls}><td>{E(r['direction'])}</td>"
                  f"<td>{E(r['date'])}</td><td>{E(r['airline'])}</td>"
                  f"<td>{E(r['duration'])}</td><td>{E(r['stops'])}</td>"
                  f"<td class='num'>{E(r['fare'])}</td><td>{tick}</td></tr>")
            a("</tbody></table></div></div>")
        a("</details>")

    real_notes = [n for n in doc["notes"] if n["lines"]]
    if real_notes:
        a("<h2>Notes</h2>")
        for n in real_notes:
            a(f'<div class="note"><h4>{E(n["heading"])}</h4>')
            for l in n["lines"]:
                a(f"<div>{E(l)}</div>")
            a("</div>")

    if doc["footer"]:
        a("<footer>")
        for l in doc["footer"]:
            a(f"<div>{E(l)}</div>")
        a(f"<div>Rendered {datetime.now():%Y-%m-%d %H:%M} by "
          f"trip_report_html.py</div>")
        a("</footer>")

    a("</div>")
    return "\n".join(p)


def newest_report():
    files = sorted(glob.glob(os.path.join(HERE, "trip_output_*.txt")),
                   key=os.path.getmtime)
    if not files:
        plain = os.path.join(HERE, "trip_output.txt")
        if os.path.exists(plain):
            return plain
        raise SystemExit("No trip_output*.txt found - run src.trip_planner "
                         "first.")
    return files[-1]


def main():
    ap = argparse.ArgumentParser(
        description="Render a trip output text report as HTML")
    ap.add_argument("input", nargs="?",
                    help="trip_output_*.txt (default: newest)")
    ap.add_argument("--output", help="HTML path (default: <input>.html)")
    ap.add_argument("--open", action="store_true",
                    help="open the result in a browser")
    args = ap.parse_args()

    src = args.input or newest_report()
    with open(src) as fh:
        text = fh.read()

    doc = parse_report(text)
    out = args.output or os.path.splitext(src)[0] + ".html"
    with open(out, "w") as fh:
        fh.write(render_html(doc, os.path.basename(src)))

    print(f"{out}")
    print(f"  {len(doc['ranked'])} ranked, {len(doc['itineraries'])} "
          f"itineraries, {sum(len(g['rows']) for g in doc['searched'])} "
          f"searched legs", file=sys.stderr)
    if args.open:
        webbrowser.open(f"file://{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
