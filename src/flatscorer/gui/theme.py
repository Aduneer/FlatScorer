"""The GUI's visual theme, as one CSS string.

Deliberately a Python constant rather than a `.css` file. Under the eventual
PyInstaller build every non-Python file has to be declared as an `--add-data`
entry and can go missing from the bundle; a module cannot. An unstyled app is
exactly the kind of breakage the non-technical user this ships for could not
diagnose, so the theme travels as code.
"""

from __future__ import annotations

import streamlit as st

CSS = """\
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&family=Space+Mono:wght@400;700&display=swap');

/* Light theme (default) — Field Guide: a cartographer's notebook, paper variant */
:root {
    --paper: #f6f1e4;
    --card: #fffdf7;
    --sidebar: #efe8d4;
    --inset: #ece3cc;
    --ink: #3a3226;
    --ink-muted: #7a6f5c;
    --ink-on-primary: #f6f1e4;
    --pine: #2f5233;
    --pine-hover: #1f3a22;
    --pine-rgb: 47, 82, 51;
    --rust: #b5651d;
    --rust-hover: #8f4d15;
    --rust-rgb: 181, 101, 29;
    --moss: #6b7a3f;
    --border: #ddd2b4;
    --border-strong: #c4b596;
    --watermark-compass: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'%3E%3Cg stroke='%232f5233' stroke-width='1.2' fill='none'%3E%3Ccircle cx='120' cy='120' r='66' stroke-opacity='0.16'/%3E%3Ccircle cx='120' cy='120' r='40' stroke-width='0.9' stroke-opacity='0.12'/%3E%3Cline x1='120' y1='54' x2='120' y2='40' stroke-opacity='0.16'/%3E%3Cline x1='186' y1='120' x2='200' y2='120' stroke-opacity='0.16'/%3E%3Cline x1='120' y1='186' x2='120' y2='200' stroke-opacity='0.16'/%3E%3Cline x1='54' y1='120' x2='40' y2='120' stroke-opacity='0.16'/%3E%3C/g%3E%3Cpath d='M120,84 L132,120 L108,120 Z' fill='%23b5651d' fill-opacity='0.14'/%3E%3Cpath d='M108,120 L132,120 L120,156 Z' fill='%232f5233' fill-opacity='0.14'/%3E%3C/svg%3E");
    --watermark-route: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60' viewBox='0 0 60 60'%3E%3Cpath d='M-10,50 L20,20 M10,70 L50,30 M40,80 L80,40' stroke='%232f5233' stroke-width='1.1' stroke-dasharray='1.5 4.5' stroke-linecap='round' stroke-opacity='0.22'/%3E%3C/svg%3E");
}

/* Dark theme — the same field guide, read by lantern light */
@media (prefers-color-scheme: dark) {
    :root {
        --paper: #1c1810;
        --card: #262116;
        --sidebar: #201b10;
        --inset: #2e2818;
        --ink: #f0e6d2;
        --ink-muted: #b8ab8c;
        --ink-on-primary: #1c1810;
        --pine: #6fae74;
        --pine-hover: #8fc794;
        --pine-rgb: 111, 174, 116;
        --rust: #e0894a;
        --rust-hover: #f0a468;
        --rust-rgb: 224, 137, 74;
        --moss: #9db56a;
        --border: #3a3225;
        --border-strong: #4a4030;
        --watermark-compass: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'%3E%3Cg stroke='%236fae74' stroke-width='1.2' fill='none'%3E%3Ccircle cx='120' cy='120' r='66' stroke-opacity='0.18'/%3E%3Ccircle cx='120' cy='120' r='40' stroke-width='0.9' stroke-opacity='0.14'/%3E%3Cline x1='120' y1='54' x2='120' y2='40' stroke-opacity='0.18'/%3E%3Cline x1='186' y1='120' x2='200' y2='120' stroke-opacity='0.18'/%3E%3Cline x1='120' y1='186' x2='120' y2='200' stroke-opacity='0.18'/%3E%3Cline x1='54' y1='120' x2='40' y2='120' stroke-opacity='0.18'/%3E%3C/g%3E%3Cpath d='M120,84 L132,120 L108,120 Z' fill='%23e0894a' fill-opacity='0.16'/%3E%3Cpath d='M108,120 L132,120 L120,156 Z' fill='%236fae74' fill-opacity='0.16'/%3E%3C/svg%3E");
        --watermark-route: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='60' viewBox='0 0 60 60'%3E%3Cpath d='M-10,50 L20,20 M10,70 L50,30 M40,80 L80,40' stroke='%236fae74' stroke-width='1.1' stroke-dasharray='1.5 4.5' stroke-linecap='round' stroke-opacity='0.24'/%3E%3C/svg%3E");
    }
}

/* Belt-and-suspenders base layer: Streamlit's own theme.light/theme.dark
   resolution doesn't reliably reach every native chrome element (notably
   the top header/toolbar), so pin the raw document background too —
   this is what was showing through as a stubborn light strip. */
html, body {
    background-color: var(--paper) !important;
}

/* Main container background — faint trail hatching + a compass rose watermark */
.stApp {
    background-color: var(--paper);
    background-image: var(--watermark-route), var(--watermark-compass);
    background-repeat: repeat, no-repeat;
    background-position: top left, bottom -60px right -60px;
    background-size: 60px 60px, 420px 420px;
    background-attachment: fixed, fixed;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: var(--ink);
}

/* Force the native header/toolbar bar (hamburger menu, "Deploy", the
   "..." menu) to follow our palette directly — Streamlit's dual-theme
   feature (added in 1.51) doesn't consistently repaint this bar itself,
   so we don't rely on it here at all. */
[data-testid="stHeader"],
[data-testid="stAppHeader"],
[data-testid="stToolbar"],
[data-testid="stAppToolbar"],
[data-testid="stToolbarActions"],
[data-testid="stDecoration"] {
    background-color: var(--paper) !important;
    background-image: none !important;
}

[data-testid="stHeader"] *,
[data-testid="stAppHeader"] *,
[data-testid="stToolbar"] *,
[data-testid="stAppToolbar"] *,
[data-testid="stToolbarActions"] * {
    color: var(--ink) !important;
}

[data-testid="stHeader"] svg,
[data-testid="stAppHeader"] svg,
[data-testid="stToolbar"] svg,
[data-testid="stAppToolbar"] svg,
[data-testid="stToolbarActions"] svg {
    fill: currentColor !important;
}

/* Sidebar Styling */
[data-testid="stSidebar"] {
    background-color: var(--sidebar);
    background-image: var(--watermark-route);
    background-repeat: repeat;
    background-size: 60px 60px;
    border-right: 1px solid var(--border);
}

/* Widen sidebar to comfortably fit larger navigation tabs */
[data-testid="stSidebar"] > div:first-child {
    min-width: 340px;
    max-width: 340px;
}

/* Prominent Sidebar Navigation Radio Buttons ("tabs") */
[data-testid="stSidebar"] div[role="radiogroup"] {
    gap: 14px;
    padding: 8px 0;
}

[data-testid="stSidebar"] div[role="radiogroup"] > label {
    background-color: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 22px 22px;
    margin: 0;
    transition: all 0.2s ease-in-out;
    cursor: pointer;
    width: 100%;
    min-height: 64px;
}

[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
    border-color: var(--pine);
    background-color: var(--inset);
    transform: translateX(2px);
    box-shadow: 0 3px 10px rgba(var(--pine-rgb), 0.15);
}

/* Selected State for Sidebar Tabs */
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] {
    background-color: var(--pine) !important;
    border-color: var(--pine-hover) !important;
    box-shadow: 0 4px 12px rgba(var(--pine-rgb), 0.3) !important;
}

[data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] p,
[data-testid="stSidebar"] div[role="radiogroup"] > label[data-selected="true"] span {
    color: var(--ink-on-primary) !important;
    font-weight: 700 !important;
    font-size: 1.35rem !important;
}

[data-testid="stSidebar"] div[role="radiogroup"] > label p,
[data-testid="stSidebar"] div[role="radiogroup"] > label span {
    font-size: 1.3rem !important;
    font-weight: 600;
    color: var(--ink);
    margin: 0;
    line-height: 1.3;
}

/* Hide standard small radio circle icon inside sidebar nav, without
   touching the label text/emoji that shares its wrapper container */
[data-testid="stSidebar"] div[role="radiogroup"] label div:has(+ [data-testid="stMarkdownContainer"]) {
    display: none !important;
}

/* Header typography matching SVG Banner */
.fs-header {
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
}

.fs-title {
    font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
    font-weight: 600;
    font-size: 2.6rem;
    color: var(--ink);
    letter-spacing: -0.5px;
    margin: 0;
    line-height: 1.1;
}

.fs-subtitle {
    font-family: 'Space Mono', monospace;
    font-size: 0.88rem;
    color: var(--ink-muted);
    margin-top: 6px;
    margin-bottom: 12px;
}

.fs-tags {
    font-family: 'Space Mono', monospace;
    font-size: 0.85rem;
}

.fs-tag-python { color: var(--pine); font-weight: 600; }
.fs-tag-osm { color: var(--moss); font-weight: 600; }
.fs-tag-gis { color: var(--rust); font-weight: 600; }
.fs-tag-cli { color: var(--ink-muted); font-weight: 600; }
.fs-tag-sep { color: var(--border-strong); margin: 0 6px; }

/* Card Container */
.fs-card {
    background-color: var(--card);
    border: 1px solid var(--border);
    border-left: 3px solid var(--pine);
    border-radius: 10px;
    padding: 22px 26px;
    margin-bottom: 24px;
}

.fs-card-title {
    font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
    font-size: 1.3rem;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 8px;
}

.fs-card-desc {
    font-size: 0.92rem;
    color: var(--ink-muted);
    margin-bottom: 16px;
}

/* Buttons Styling */
div.stButton > button[kind="primary"] {
    background-color: var(--pine) !important;
    color: var(--ink-on-primary) !important;
    border: 1px solid var(--pine-hover) !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    padding: 10px 24px !important;
    transition: all 0.2s ease !important;
    box-shadow: 0 4px 10px rgba(var(--pine-rgb), 0.25) !important;
}

div.stButton > button[kind="primary"]:hover {
    background-color: var(--pine-hover) !important;
    color: var(--ink-on-primary) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 14px rgba(var(--pine-rgb), 0.4) !important;
}

div.stDownloadButton > button {
    border: 1px solid var(--border-strong) !important;
    border-radius: 8px !important;
    background-color: var(--card) !important;
    color: var(--ink) !important;
    font-weight: 600 !important;
    transition: all 0.2s ease !important;
}

div.stDownloadButton > button:hover {
    border-color: var(--pine) !important;
    color: var(--pine) !important;
    background-color: var(--inset) !important;
}

/* Stat pill badge */
.fs-stat-pill {
    background-color: var(--inset);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-family: 'Space Mono', monospace;
    font-size: 0.85rem;
    color: var(--ink-muted);
    margin-bottom: 16px;
}

.fs-stat-pill strong {
    color: var(--ink);
}

/* Winner panel — a rubber-stamped "top pick", like a passport stamp */
.fs-winner-panel {
    display: flex;
    align-items: center;
    gap: 20px;
    background-color: var(--card);
    border: 1px dashed var(--border-strong);
    border-left: 3px solid var(--rust);
    border-radius: 10px;
    padding: 16px 22px;
    margin-bottom: 20px;
}

.fs-stamp {
    flex-shrink: 0;
    width: 104px;
    height: 104px;
    border-radius: 50%;
    border: 3px double var(--rust);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    transform: rotate(-8deg);
    color: var(--rust);
    font-family: 'Space Mono', monospace;
    text-align: center;
    background-color: rgba(var(--rust-rgb), 0.05);
}

.fs-stamp-label {
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
}

.fs-stamp-score {
    font-size: 1.55rem;
    font-weight: 700;
    margin-top: 2px;
}

.fs-winner-eyebrow {
    font-family: 'Space Mono', monospace;
    font-size: 0.8rem;
    color: var(--ink-muted);
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 4px;
}

.fs-winner-name {
    font-family: 'Fraunces', Georgia, 'Times New Roman', serif;
    font-size: 1.4rem;
    font-weight: 600;
    color: var(--ink);
}

.fs-winner-sub {
    font-family: 'Space Mono', monospace;
    font-size: 0.85rem;
    color: var(--ink-muted);
    margin-top: 2px;
}

/* Rust rather than pine: the one outbound link on the page, and the
   only thing in the panel that leaves the app. */
.fs-winner-link {
    display: inline-block;
    margin-top: 10px;
    padding: 4px 12px;
    border: 1px solid var(--rust);
    border-radius: 3px;
    font-family: 'Space Mono', monospace;
    font-size: 0.8rem;
    color: var(--rust) !important;
    text-decoration: none !important;
    transition: background 0.15s ease, color 0.15s ease;
}

.fs-winner-link:hover {
    background: var(--rust);
    color: var(--ink-on-primary) !important;
}
"""


def inject():
    """Apply the theme. Must run after `st.set_page_config`."""
    st.markdown(CSS, unsafe_allow_html=True)
