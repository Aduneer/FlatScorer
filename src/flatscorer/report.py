"""The standalone overview report: one card per scored flat, with its breakdown.

`score_breakdown()` is the most valuable thing the engine computes and, until
this module existed, the least visible thing it emitted - printed as a text
table into a collapsed log expander and then discarded. This renders it.

Layer 1, exactly like `mapping.py`: it may import the leaves, and nothing above
imports it except `scorer`.

Both HTML surfaces this package produces escape what they interpolate. The
report is a file the user opens locally and sends to other people, and a config
is meant to be passed around, so a value out of someone else's config must not
be able to close an attribute and open a tag of its own.
"""

from __future__ import annotations

import base64
import html
import math
import os
from typing import Any

import pandas as pd

from .routing import COMMUTE_COLUMN_SUFFIXES, TRAVEL_MODES
from .scoring import SCORE_SCALE_MAX, score_colour

# The human wording for each breakdown key. Destination terms are keyed
# `dest_<name>` and are labelled from the name itself, so they need no entry.
TERM_LABELS = {
    "supermarket": "Supermarkets",
    "bakery": "Bakeries",
    "pharmacy": "Pharmacies",
    "gym": "Gyms",
    "transit": "Transit",
    "green": "Green space",
    # Named for the good thing, not the bad one: the term scores *distance from*
    # a busy road, so a full bar means quiet.
    "noise": "Quiet",
    "rent": "Rent",
}


# Local photos are embedded rather than linked, so the report stays one
# sendable file. The cap is measured on the file on disk, before base64 (which
# inflates by roughly a third): without it, three phone photos turn a report
# into a 40 MB attachment.
MAX_EMBEDDED_IMAGE_BYTES = 2 * 1024 * 1024

# The extension supplies the data: URI's MIME type, so the set of formats we can
# embed is exactly the set we can name.
EMBEDDABLE_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_src(image: str | None, name: str, log) -> str | None:
    """The value for a card's `src="..."`, or None to fall back to the dial.

    Every failure here is soft and logged rather than raised. A config is passed
    between machines, so a path valid where it was written and absent where it
    is read is routine - and the cost of getting it wrong is a card that shows
    its score dial instead of a photo, which is what a card with no photo shows
    anyway. Nothing here justifies failing a multi-minute run.

    An http(s) value is never fetched: it goes into the attribute as written
    (escaped), and the reader's browser decides whether it loads.
    """
    if not image:
        return None
    if image.lower().startswith(("http://", "https://")):
        return html.escape(image, quote=True)

    extension = os.path.splitext(image)[1].lower()
    mime = EMBEDDABLE_IMAGE_TYPES.get(extension)
    if mime is None:
        log(f"[!] {name}: cannot embed '{image}' - only "
            f"{', '.join(sorted(EMBEDDABLE_IMAGE_TYPES))} can be embedded. Using the score dial.")
        return None
    try:
        size = os.path.getsize(image)
    except OSError as exc:
        log(f"[!] {name}: cannot read image '{image}' ({exc.strerror or exc}). "
            "Using the score dial.")
        return None
    if size > MAX_EMBEDDED_IMAGE_BYTES:
        log(f"[!] {name}: image '{image}' is {size / 1024 / 1024:.1f} MB, over the "
            f"{MAX_EMBEDDED_IMAGE_BYTES / 1024 / 1024:.0f} MB embed limit. Using the score dial.")
        return None
    try:
        with open(image, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        log(f"[!] {name}: cannot read image '{image}' ({exc.strerror or exc}). "
            "Using the score dial.")
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def term_label(key: str) -> str:
    """The human wording for one breakdown key."""
    if key.startswith("dest_"):
        return f"→ {key[len('dest_'):]}"
    return TERM_LABELS.get(key, key)


def term_order(breakdowns: dict[str, dict[str, dict[str, float]]]) -> list[str]:
    """The order terms render in, most influential first.

    Computed once from the first breakdown and applied to every card. Shares are
    a property of the weights, not of the candidate, so every breakdown would
    sort the same way - but sorting each card independently would still let a
    tie break differently card to card, and a column that doesn't line up
    defeats the point of the deck. The name is the tiebreak, so the order is
    fully determined.
    """
    if not breakdowns:
        return []
    first = next(iter(breakdowns.values()))
    return sorted(first, key=lambda key: (-first[key]["share"], key))


def bar_rows(breakdown: dict[str, dict[str, float]], order: list[str]) -> list[dict[str, Any]]:
    """One bar per term: how much it could contribute, and how much it did.

    `contribution = SCORE_SCALE_MAX * share * normalized`, so the track (the
    points available) is the share and the fill fraction reduces to exactly
    `normalized`. Tracks therefore sum to the full lane on every card and fills
    sum to the score - which is what lets a reader see that a flat lost on a
    heavily weighted term rather than on a trivial one.
    """
    rows = []
    for key in order:
        term = breakdown[key]
        track_pct = term["share"] * 100.0
        rows.append({
            "key": key,
            "label": term_label(key),
            "track_pct": track_pct,
            "fill_pct": track_pct * term["normalized"],
            "points": term["contribution"],
        })
    return rows


# Inline, not a .css file, for the same reason `gui/theme.py` is a Python
# string: the report has to be one self-contained file that can be emailed.
REPORT_CSS = """\
:root {
  --paper:#f6f1e4; --card:#fffdf7; --inset:#ece3cc; --ink:#3a3226;
  --ink-muted:#7a6f5c; --border:#ddd2b4; --track:#e6dcc2;
  --green:#2f5233; --orange:#b5651d; --red:#9e3b3b;
}
* { box-sizing:border-box; }
body { margin:0; padding:28px 20px 56px; background:var(--paper); color:var(--ink);
       font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.fs-wrap { max-width:1040px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
.fs-sub { color:var(--ink-muted); margin:0 0 24px; font-size:14px; }
.fs-card { background:var(--card); border:1px solid var(--border); border-radius:14px;
           overflow:hidden; display:grid; grid-template-columns:190px 1fr; margin-bottom:14px; }
.fs-media { position:relative; min-height:100%; background:var(--inset); }
.fs-photo { width:100%; height:100%; object-fit:cover; display:block; }
.fs-dial { display:flex; flex-direction:column; align-items:center; justify-content:center;
           height:100%; min-height:150px; color:#fff; }
.fs-dial-green { background:var(--green); }
.fs-dial-orange { background:var(--orange); }
.fs-dial-red { background:var(--red); }
.fs-dial-num { font-size:42px; font-weight:700; line-height:1; }
.fs-dial-of { font-size:11px; letter-spacing:.12em; text-transform:uppercase; opacity:.8; margin-top:6px; }
.fs-rank { position:absolute; top:10px; left:10px; background:rgba(0,0,0,.55); color:#fff;
           font-size:12px; font-weight:700; padding:3px 9px; border-radius:99px; }
.fs-body { padding:16px 18px; display:grid; grid-template-columns:1.05fr 1fr; gap:22px; }
.fs-name { font-weight:600; font-size:17px; margin:0 0 2px; }
.fs-addr { color:var(--ink-muted); font-size:13px; margin:0 0 12px; }
.fs-chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
.fs-chip { background:var(--inset); border-radius:99px; padding:3px 10px; font-size:12.5px;
           color:var(--ink-muted); }
.fs-chip strong { color:var(--ink); font-weight:600; }
.fs-link { font-size:13px; color:var(--green); font-weight:600; text-decoration:none; }
.fs-bar { display:grid; grid-template-columns:104px 1fr 46px; align-items:center; gap:10px;
          font-size:12px; margin-bottom:5px; }
.fs-bar-label { color:var(--ink-muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.fs-lane { position:relative; height:9px; }
.fs-track { position:absolute; left:0; top:0; bottom:0; background:var(--track); border-radius:3px; }
.fs-fill { position:absolute; left:0; top:0; bottom:0; border-radius:3px; }
.fs-fill-green { background:var(--green); }
.fs-fill-orange { background:var(--orange); }
.fs-fill-red { background:var(--red); }
.fs-pts { text-align:right; color:var(--ink-muted); font-variant-numeric:tabular-nums; }
.fs-credit { color:var(--ink-muted); font-size:12px; margin:28px 0 0;
             padding-top:14px; border-top:1px solid var(--border); }
.fs-credit a { color:var(--ink-muted); }
.fs-note { background:var(--card); border:1px solid var(--border); border-left:4px solid var(--orange);
           border-radius:10px; padding:12px 16px; margin-bottom:14px; font-size:13.5px; }
details { margin-top:22px; }
summary { cursor:pointer; color:var(--ink-muted); font-size:13.5px; }
table { border-collapse:collapse; margin-top:12px; font-size:12.5px; width:100%; }
th, td { border:1px solid var(--border); padding:5px 8px; text-align:right; }
th:first-child, td:first-child { text-align:left; }
@media (max-width:760px) {
  .fs-card { grid-template-columns:1fr; }
  .fs-body { grid-template-columns:1fr; }
}
"""


def _commute_chips(row: pd.Series, columns: Any) -> list[str]:
    """One chip per commute column, e.g. 'Work 9.0 min walking'.

    Mirrors the map popup's loop: the column suffix identifies the mode, and
    TRAVEL_MODES holds the wording, so a new mode needs no change here.
    """
    chips = []
    for col in columns:
        suffix = next((s for s in COMMUTE_COLUMN_SUFFIXES if col.endswith(s)), None)
        if suffix is None:
            continue
        label = col[:-len(suffix)].replace("_", " ").title()
        verb = next(spec["verb"] for spec in TRAVEL_MODES.values()
                    if suffix == f"_{spec['column_suffix']}")
        chips.append(f"{html.escape(label)} <strong>{row[col]} min</strong> {html.escape(verb)}")
    return chips


def _cell(value: Any) -> str:
    """A frame value as escaped text, with pandas' NaN rendered as blank."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return html.escape(str(value))


def _media_html(rank: int, score: float, src: str | None) -> str:
    """The card's left panel: the photo if there is one, else the score dial.

    Never empty. A deck where only the flats you bothered to photograph look
    finished is worse than one with no photos at all, and the dial reuses the
    map pins' own colour bands so the whole tool speaks one colour language.
    """
    if src:
        inner = f'<img class="fs-photo" src="{src}" alt="">'
    else:
        inner = (f'<div class="fs-dial fs-dial-{score_colour(score)}">'
                 f'<div class="fs-dial-num">{score:.1f}</div>'
                 f'<div class="fs-dial-of">out of {SCORE_SCALE_MAX:.0f}</div></div>')
    return f'<div class="fs-media">{inner}<span class="fs-rank">#{rank}</span></div>'


def _card_html(rank: int, row: pd.Series, breakdown: dict[str, dict[str, float]],
               order: list[str], src: str | None) -> str:
    score = float(row["score"])
    colour = score_colour(score)

    chips = [f'<span class="fs-chip">€<strong>{_cell(row.get("rent_eur"))}</strong>/mo</span>']
    chips += [f'<span class="fs-chip">{c}</span>' for c in _commute_chips(row, row.index)]
    for label, key in (("supermarkets", "supermarkets"), ("transit stops", "transit_stops"),
                       ("m² green", "green_area_m2"), ("m to a busy road", "dist_busy_road_m")):
        if key in row.index:
            chips.append(f'<span class="fs-chip"><strong>{_cell(row[key])}</strong> {label}</span>')

    url = row.get("url")
    url = "" if url is None or (isinstance(url, float) and math.isnan(url)) else str(url).strip()
    # On the end of the preceding line, never on a line of its own: a
    # whitespace-only line terminates a raw-HTML block and leaks closing tags.
    link = (f'<a class="fs-link" href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">\U0001F517 View listing ↗</a>') if url else ""

    # A flat with no breakdown (e.g. it reached the frame without one) renders
    # with no bars rather than taking the whole report down: bar_rows() raises
    # a bare KeyError on any name `order` has that `breakdown` lacks.
    bars = "".join(
        f'<div class="fs-bar"><span class="fs-bar-label">{html.escape(bar["label"])}</span>'
        f'<span class="fs-lane"><span class="fs-track" style="width:{bar["track_pct"]:.3f}%"></span>'
        f'<span class="fs-fill fs-fill-{colour}" style="width:{bar["fill_pct"]:.3f}%"></span></span>'
        f'<span class="fs-pts">{bar["points"]:.2f}</span></div>'
        for bar in (bar_rows(breakdown, order) if breakdown else [])
    )

    return (f'<article class="fs-card">{_media_html(rank, score, src)}'
            f'<div class="fs-body"><div>'
            f'<p class="fs-name">{_cell(row["name"])}</p>'
            f'<p class="fs-addr">Score {score:.2f} / {SCORE_SCALE_MAX:.0f}</p>'
            f'<div class="fs-chips">{"".join(chips)}</div>{link}'
            f'</div><div>{bars}</div></div></article>')


def _table_html(df: pd.DataFrame) -> str:
    """The ranked table, collapsed - so the file is complete without competing
    with the deck for attention. `lat`/`lon` are plumbing, and `image` was never
    in the frame."""
    visible = df.drop(columns=["lat", "lon"], errors="ignore")
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in visible.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in row) + "</tr>"
        for row in visible.itertuples(index=False)
    )
    return (f"<details><summary>Exact numbers</summary>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>")


# Every number on this page is derived from OpenStreetMap, and ODbL requires a
# Produced Work to say so. The map gets this for free - folium's tile layer
# carries the notice - but this report is a standalone file built by hand, and
# it is the artifact designed to be *sent to other people*, which is exactly
# when the attribution has to travel with it.
CREDIT_HTML = (
    '<p class="fs-credit">Amenity, transit, green space and street network data '
    '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors, available under the '
    '<a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>. '
    'Generated by <a href="https://github.com/Aduneer/FlatScorer">FlatScorer</a>.</p>'
)


def generate_report(df: pd.DataFrame, breakdowns: dict[str, dict[str, dict[str, float]]],
                    resolved_destinations: dict[str, Any], html_file: str,
                    images: dict[str, str] | None = None, *,
                    failed_candidates: Any = (), log=None) -> None:
    """Write the overview report: one card per scored flat, with its breakdown.

    `log` takes the same one-string callable `FlatScorer._log` is, matching
    `generate_map`, so the report narrates itself when driven by the engine and
    stays silent when not.
    """
    log = log or (lambda _msg: None)
    images = images or {}
    order = term_order(breakdowns)

    cards = []
    for rank, (_, row) in enumerate(df.iterrows(), start=1):
        name = row["name"]
        src = image_src(images.get(name), str(name), log)
        cards.append(_card_html(rank, row, breakdowns.get(name, {}), order, src))

    # The GUI reports these loudly, but this file gets sent to other people and
    # would otherwise drop a flat without saying so.
    notice = ""
    failed = list(failed_candidates or ())
    if failed:
        items = "".join(f"<li><strong>{html.escape(str(n))}</strong> — "
                        f"{html.escape(str(a))}</li>" for n, a in failed)
        notice = (f'<div class="fs-note">{len(failed)} candidate(s) could not be geocoded '
                  f"and are missing from this ranking:<ul>{items}</ul></div>")

    destination_names = ", ".join(html.escape(str(n)) for n in resolved_destinations) or "none"
    page = (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>FlatScorer — overview</title><style>{REPORT_CSS}</style></head><body>"
            f'<div class="fs-wrap"><h1>Apartment overview</h1>'
            f'<p class="fs-sub">{len(df)} flat(s) scored out of {SCORE_SCALE_MAX:.0f}. '
            f"Commute destinations: {destination_names}. Each bar is as wide as the points "
            f"that criterion could contribute; the filled part is what this flat earned.</p>"
            f"{notice}{''.join(cards)}{_table_html(df)}{CREDIT_HTML}</div></body></html>\n")

    with open(html_file, "w", encoding="utf-8") as handle:
        handle.write(page)
    log(f"[+] Saved overview report to {html_file}")
