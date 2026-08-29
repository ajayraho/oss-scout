import base64
import html
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

from github import (
    GitHubClient,
    GitHubError,
    RateLimitError,
    issue_age_matches,
    repository_activity_matches,
    competition_score,
    competition_label,
    parse_date,
)

# ============================================================
# CONFIG
# ============================================================

st.set_page_config(
    page_title="OSS Scout",
    page_icon="◉",
    layout="wide",
)

APP_NAME = "OSS Scout"
MAX_REPOS = 100

TOKEN_LABEL = "GitHub Personal Access Token"
TOKEN_PLACEHOLDER = "github_pat_..."
REMEMBER_LABEL = "Remember this token on this device"

_DEVICON = "https://raw.githubusercontent.com/devicons/devicon/master/icons/{0}/{0}-original.svg"

LANGUAGE_ICON_URLS = {
    "Python":     _DEVICON.format("python"),
    "JavaScript": _DEVICON.format("javascript"),
    "TypeScript": _DEVICON.format("typescript"),
    "Java":       _DEVICON.format("java"),
    "Go":         _DEVICON.format("go"),
    "Rust":       _DEVICON.format("rust"),
    "C++":        _DEVICON.format("cplusplus"),
    "C":          _DEVICON.format("c"),
    "C#":         _DEVICON.format("csharp"),
    "Ruby":       _DEVICON.format("ruby"),
    "PHP":        _DEVICON.format("php"),
    "Swift":      _DEVICON.format("swift"),
    "Kotlin":     _DEVICON.format("kotlin"),
    "Scala":      _DEVICON.format("scala"),
    "R":          _DEVICON.format("r"),
    "Dart":       _DEVICON.format("dart"),
    "Haskell":    _DEVICON.format("haskell"),
    "Elixir":     _DEVICON.format("elixir"),
    "Lua":        _DEVICON.format("lua"),
    "Julia":      _DEVICON.format("julia"),
    "Perl":       _DEVICON.format("perl"),
    "Clojure":    _DEVICON.format("clojure"),
    "Shell":      _DEVICON.format("bash"),
    "Zig":        "https://raw.githubusercontent.com/ziglang/logo/master/zig-mark.svg",
}
LANGUAGE_OPTIONS = list(LANGUAGE_ICON_URLS.keys())

# Closest solid-circle emoji to each language's GitHub accent color —
# used in the multiselect format_func so options show a colored dot.
# Plain text filled-circle (U+25CF) — renders at normal text size,
# much smaller than emoji circles. Color is applied via the CSS rule
# below that injects SVG dots into the BaseWeb listbox at render time.
LANGUAGE_DOT = {lang: "●" for lang in LANGUAGE_ICON_URLS}

# GitHub's own per-language colour convention - used for each repository
# card's left accent bar, so the colour means something instead of being
# decorative.
LANGUAGE_COLORS = {
    "Python":     "#3572A5",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "Java":       "#b07219",
    "Go":         "#00ADD8",
    "Rust":       "#dea584",
    "C++":        "#f34b7d",
    "C":          "#555555",
    "C#":         "#178600",
    "Ruby":       "#701516",
    "PHP":        "#4F5D95",
    "Swift":      "#F05138",
    "Kotlin":     "#A97BFF",
    "Scala":      "#c22d40",
    "R":          "#198CE7",
    "Dart":       "#00B4AB",
    "Haskell":    "#5e5086",
    "Elixir":     "#6e4a7e",
    "Lua":        "#000080",
    "Julia":      "#a270ba",
    "Perl":       "#0298c3",
    "Clojure":    "#db5855",
    "Shell":      "#89e051",
    "Zig":        "#ec915c",
}
DEFAULT_LANGUAGE_COLOR = "#9a978c"

# ── Logo ──────────────────────────────────────────────────────
# Loaded once at startup from the same directory as this file.
# The raw SVG has width/height hard-coded to 1254px with no
# viewBox, so we inject one so the browser can scale it to any
# target size without distortion.

_logo_data_uri = ""

_logo_path = Path(__file__).parent / "logo.png"
if _logo_path.exists():
    _logo_b64 = base64.b64encode(_logo_path.read_bytes()).decode()
    _logo_data_uri = f"data:image/png;base64,{_logo_b64}"

# ── localStorage reader component ────────────────────────────────────────────
# Declaring a real Streamlit component (served from disk, same origin as the
# app) lets us read localStorage directly and return the value to Python.
# This is the only reliable way to get localStorage data into Python's session
# state without a race condition: components.html() creates an iframe on a
# different origin where we can't reach the parent's storage, and the blur()
# trick requires a Streamlit rerun to complete before the user can act.

# Issue labels get colour-coded pastel pills, keyed by keyword match against
# the label's real name (an issue can carry labels beyond the ones filtered
# on) so the badges stay meaningful instead of one flat accent colour.
LABEL_COLOR_RULES = [
    (("good first issue", "good first pr", "beginner"), "purple"),
    (("help wanted",), "orange"),
    (("documentation", "docs"), "blue"),
    (("bug", "fix"), "red"),
    (("enhancement", "feature"), "green"),
]


def label_class(name):
    low = (name or "").lower()
    for keywords, css_class in LABEL_COLOR_RULES:
        if any(keyword in low for keyword in keywords):
            return css_class
    return "neutral"


def relative_time(iso_value):
    dt = parse_date(iso_value)
    if not dt:
        return "unknown"

    delta = datetime.now(timezone.utc) - dt
    seconds = delta.total_seconds()

    if seconds < 3600:
        return "just now"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds < 86400 * 30:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    if seconds < 86400 * 365:
        months = int(seconds // (86400 * 30))
        return f"{months} month{'s' if months != 1 else ''} ago"

    years = int(seconds // (86400 * 365))
    return f"{years} year{'s' if years != 1 else ''} ago"


SORT_LABELS = {
    "Competition": "Sort by: Most relevant",
    "Recently updated": "Sort by: Recently updated",
    "Fewest comments": "Sort by: Fewest comments",
    "Fewest stars": "Sort by: Fewest stars",
    "Most stars": "Sort by: Most stars",
}


def sort_results(results, choice):
    """Pure client-side sort - never re-hits the GitHub API, so changing
    the sort order is instant and free."""
    results = list(results)

    if choice == "Competition":
        results.sort(key=lambda x: x["competition_score"])
    elif choice == "Recently updated":
        results.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    elif choice == "Fewest comments":
        results.sort(key=lambda x: x["comments"])
    elif choice == "Fewest stars":
        results.sort(key=lambda x: x["stars"])
    elif choice == "Most stars":
        results.sort(key=lambda x: x["stars"], reverse=True)

    return results


def group_by_repo(results):
    """Groups already-sorted issues by repository, preserving the order
    each repo first appears in - so both the repo cards and the issues
    inside them follow the current sort choice."""
    grouped = {}

    for item in results:
        key = item["repo"]
        if key not in grouped:
            grouped[key] = {
                "repo": item["repo"],
                "repo_url": item["repo_url"],
                "stars": item["stars"],
                "language": item["language"],
                "avatar_url": item.get("avatar_url"),
                "issues": [],
            }
        grouped[key]["issues"].append(item)

    return list(grouped.values())


def render_banner(kind, icon, title, body=""):
    body_html = f"<br>{body}" if body else ""
    return (
        f'<div class="banner banner-{kind}"><span class="banner-icon">{icon}</span>'
        f'<div><strong>{title}</strong>{body_html}</div></div>'
    )


def render_expandable_banner(kind, icon, title, body, detail_rows_html, action_label="View details"):
    # Built as one flat, zero-indent string on purpose: st.markdown still
    # runs unsafe_allow_html content through a Markdown parser first, and
    # an indented line following a blank line gets read as a literal
    # code block instead of passed through as HTML - that's what caused
    # stray closing tags to show up as visible text.
    return (
        f'<details class="banner banner-{kind}">'
        f'<summary class="banner-summary">'
        f'<span class="banner-icon">{icon}</span>'
        f'<div class="banner-text"><strong>{title}</strong><br>{body}</div>'
        f'<span class="banner-action">{action_label}</span>'
        f'</summary>'
        f'<div class="banner-details">{detail_rows_html}</div>'
        f'</details>'
    )


# ============================================================
# THEME - palette lifted from the OSI reference: warm paper,
# soft charcoal ink, and a blue -> pink -> coral gradient wash
# used as a corner accent, echoing that page's soft gradient blur.
# ============================================================

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700;800&display=swap');

:root {
    --bg: #f1f0ec;
    --surface: #ffffff;
    --surface-2: #faf9f6;
    --ink: #33322e;
    --muted: #8b897f;
    --line: #e3e0d8;

    --grad-blue: #a7c4e8;
    --grad-pink: #e3b4c9;
    --grad-coral: #f0a57a;

    --dark-pill: #2e2d2a;

    --low-tint: #e7f3ec;   --low-text: #0b7a56;
    --med-tint: #fbf0dd;   --med-text: #9a5b00;
    --high-tint: #fbe9e7;  --high-text: #a33b30;
}

html, body, .stApp {
    font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

html {
    background: var(--bg);
}

.stApp {
    background:
        radial-gradient(90% 70% at 12% -10%, rgba(167,196,232,0.50), transparent 58%),
        radial-gradient(80% 65% at 88% 8%, rgba(227,180,201,0.42), transparent 58%),
        radial-gradient(85% 70% at 78% 60%, rgba(240,165,122,0.34), transparent 62%),
        radial-gradient(90% 75% at 8% 95%, rgba(167,196,232,0.30), transparent 62%),
        var(--bg);
    background-attachment: fixed;
    color: var(--ink);
    min-height: 100vh;
}

[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stHeader"],
.main {
    background: transparent !important;
}

[data-testid="stDecoration"] {
    display: none;
}

.stApp input, .stApp textarea, .stApp select, .stApp button {
    font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

.stApp input, .stApp textarea {
    border-radius: 8px !important;
    border-color: var(--line) !important;
    background: rgba(255,255,255,0.6) !important;
}

.block-container {
    max-width: 1760px;
    padding-top: 0.5rem;
    padding-left: 2.5rem;
    padding-right: 2.5rem;
    padding-bottom: 0;
}

[data-baseweb="select"] > div,
[data-baseweb="base-input"],
.stApp input:not([type="checkbox"]) {
    min-height: 42px !important;
    border-radius: 8px !important;
}

div[data-testid="stNumberInput"] button {
    display: none !important;
}

div[data-testid="stNumberInput"] input {
    text-align: left;
}

span[data-baseweb="tag"] {
    background-color: #efe6fb !important;
    border-color: #efe6fb !important;
    color: #6b3fa0 !important;
}

span[data-baseweb="tag"] svg {
    fill: #6b3fa0 !important;
}

.masthead {
    position: relative;
    padding: 4px 4px 10px;
    margin-bottom: 4px;
}

.masthead-identity {
    display: flex;
    align-items: center;
    gap: 16px;
}

.masthead-logo {
    width: 120px;
    height: 120px;
    flex-shrink: 0;
}

.app-title {
    font-size: 36px;
    font-weight: 300;
    letter-spacing: -0.01em;
    color: var(--ink);
}

.app-title strong { font-weight: 800; }

.app-subtitle {
    color: var(--muted);
    margin-top: 6px;
    font-size: 15px;
    font-weight: 400;
}

[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(1):not([data-testid="stColumn"] [data-testid="stColumn"]) {
    background: rgba(255,255,255,0.55);
    -webkit-backdrop-filter: blur(24px) saturate(160%);
    backdrop-filter: blur(24px) saturate(160%);
    border: 1px solid rgba(255,255,255,0.7);
    box-shadow: 0 20px 60px rgba(51,50,46,0.06);
    border-radius: 20px;
    padding: 22px 22px 18px;
    align-self: flex-start;
    max-height: calc(100vh - 11rem);
    overflow-y: auto;
    scrollbar-width: none;
}

[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(1):not([data-testid="stColumn"] [data-testid="stColumn"])::-webkit-scrollbar { display: none; }

[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(2):not([data-testid="stColumn"] [data-testid="stColumn"]) {
    padding: 4px 4px 4px 6px;
}

[data-testid="stForm"] [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    padding: 0;
    background: transparent;
    box-shadow: none;
    border: none;
}

[data-testid="stForm"] [data-testid="stVerticalBlock"] {
    gap: 0.6rem;
}

[data-testid="stForm"] {
    border: none !important;
    padding: 0 !important;
    background: transparent !important;
}

[data-testid="stExpander"] {
    border: 1px solid rgba(255,255,255,0.7) !important;
    border-radius: 12px !important;
    background: rgba(255,255,255,0.45) !important;
    -webkit-backdrop-filter: blur(16px) saturate(150%);
    backdrop-filter: blur(16px) saturate(150%);
    margin-bottom: 0;
}

/* Streamlit injects gap as an inline style on stVerticalBlock, so we
   need !important to override it. Flatten ALL vertical-block gaps in
   the left panel to a small value, then explicitly restore the form's
   own inner spacing (the form rule is more specific so it wins). */
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(1):not([data-testid="stColumn"] [data-testid="stColumn"]) [data-testid="stVerticalBlock"] {
    gap: 6px !important;
}
[data-testid="stForm"] [data-testid="stVerticalBlock"] {
    gap: 0.6rem !important;
}

.section-label {
    font-weight: 700;
    letter-spacing: .08em;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    margin: 20px 0 10px 0;
}

.section-label:first-child { margin-top: 0; }

.lang-icon-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: -6px 0 14px 2px;
}

.lang-icon-chip {
    width: 18px;
    height: 18px;
    padding: 3px;
    border-radius: 6px;
    background: rgba(255,255,255,0.7);
    border: 1px solid var(--line);
}

.stFormSubmitButton > button {
    background: var(--dark-pill) !important;
    border: none !important;
    color: #fff !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
}

.stFormSubmitButton > button:hover {
    filter: brightness(1.18);
}

[data-testid="stColumn"]:not([data-testid="stForm"] [data-testid="stColumn"]):not([data-testid="stExpander"] [data-testid="stColumn"]) [data-testid="stSelectbox"] {
    margin-left: auto;
}

.results-eyebrow {
    font-weight: 700;
    letter-spacing: .08em;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 14px;
}

.banner {
    display: flex;
    gap: 12px;
    align-items: flex-start;
    padding: 14px 16px;
    border-radius: 12px;
    border: 1px solid var(--line);
    margin-bottom: 16px;
    font-size: 13.5px;
    line-height: 1.5;
}

details.banner { display: block; padding: 0; overflow: hidden; }
details.banner summary { list-style: none; cursor: pointer; }
details.banner summary::-webkit-details-marker { display: none; }

.banner-summary {
    display: flex;
    gap: 12px;
    align-items: flex-start;
    padding: 14px 16px;
}

.banner-text { flex: 1; }

.banner-action {
    flex-shrink: 0;
    background: rgba(255,255,255,0.85);
    border: 1px solid rgba(51,50,46,0.12);
    border-radius: 999px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
}

.banner-details {
    padding: 0 16px 14px 46px;
    font-size: 12.5px;
}

.banner-icon { font-size: 15px; line-height: 1.4; }

.banner-error   { background: var(--high-tint); border-color: transparent; color: var(--high-text); }
.banner-warning { background: var(--med-tint);  border-color: transparent; color: var(--med-text); }
.banner-success { background: var(--low-tint);  border-color: transparent; color: var(--low-text); }
.banner-quota   {
    background: rgba(255,255,255,0.5);
    -webkit-backdrop-filter: blur(14px);
    backdrop-filter: blur(14px);
    color: var(--muted);
}

.badge {
    display: inline-block;
    position: relative;
    cursor: default;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: .04em;
    text-transform: uppercase;
    vertical-align: middle;
    margin-left: 8px;
}

.badge-low    { background: var(--low-tint);  color: var(--low-text); }
.badge-medium { background: var(--med-tint);  color: var(--med-text); }
.badge-high   { background: var(--high-tint); color: var(--high-text); }

.badge[data-tooltip]::after {
    content: attr(data-tooltip);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: rgba(15,15,30,.92);
    color: #f0f0f0;
    font-size: 11px;
    font-weight: 400;
    letter-spacing: 0;
    text-transform: none;
    white-space: nowrap;
    padding: 5px 11px;
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
    pointer-events: none;
    opacity: 0;
    transition: opacity .15s ease;
    z-index: 9999;
}
.badge[data-tooltip]:hover::after { opacity: 1; }

.label-pill {
    display: inline-block;
    padding: 2px 9px;
    border-radius: 999px;
    font-size: 10.5px;
    font-weight: 600;
    white-space: nowrap;
    margin: 0 4px 4px 0;
}

.label-purple  { background: #efe6fb; color: #6b3fa0; }
.label-green   { background: #e2f5e9; color: #1f7a4c; }
.label-orange  { background: #fdecd8; color: #a15c0a; }
.label-blue    { background: #e3edfc; color: #2456b0; }
.label-red     { background: #fbe4e4; color: #a33b30; }
.label-neutral { background: #efeee9; color: #6b6a63; }

details.repo-card {
    margin-bottom: 14px;
    border: 1px solid rgba(255,255,255,0.7);
    border-left: 4px solid var(--line);
    border-radius: 14px;
    background: rgba(255,255,255,0.55);
    -webkit-backdrop-filter: blur(18px) saturate(150%);
    backdrop-filter: blur(18px) saturate(150%);
    overflow: hidden;
    transition: background .15s ease, box-shadow .15s ease;
}

details.repo-card:hover {
    background: rgba(255,255,255,0.72);
    box-shadow: 0 8px 24px rgba(51,50,46,0.08);
}

details.repo-card summary {
    list-style: none;
    cursor: pointer;
}

details.repo-card summary::-webkit-details-marker { display: none; }

.repo-summary {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 20px;
}

.repo-avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    flex-shrink: 0;
    object-fit: cover;
}

.repo-avatar-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--dark-pill);
    color: #fff;
    font-weight: 700;
    font-size: 14px;
}

.repo-summary-main { flex: 1; min-width: 0; }

.repo-name {
    font-size: 15.5px;
    font-weight: 700;
    color: var(--ink);
}

.repo-stats {
    color: var(--muted);
    font-size: 12.5px;
    margin-top: 2px;
    font-variant-numeric: tabular-nums;
}

.repo-link-btn {
    flex-shrink: 0;
    text-decoration: none;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--low-text);
    background: var(--low-tint);
    border-radius: 999px;
    padding: 7px 14px;
    white-space: nowrap;
}

.repo-link-btn:hover { filter: brightness(0.97); }

.repo-chevron {
    flex-shrink: 0;
    color: var(--muted);
    font-size: 13px;
    transition: transform .15s ease;
}

details.repo-card[open] .repo-chevron {
    transform: rotate(180deg);
}

.repo-issue-table {
    border-top: 1px solid var(--line);
    overflow-x: auto;
}

.repo-issue-table table {
    width: 100%;
    min-width: 640px;
    border-collapse: collapse;
    font-size: 12.5px;
    table-layout: fixed;
}

.repo-issue-table col.col-issue { width: auto; }
.repo-issue-table col.col-labels { width: 20%; }
.repo-issue-table col.col-comments { width: 8%; }
.repo-issue-table col.col-opened { width: 11%; }
.repo-issue-table col.col-assignee { width: 12%; }
.repo-issue-table col.col-pr { width: 13%; }
.repo-issue-table col.col-actions { width: 10%; }

.repo-issue-table th {
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: var(--muted);
    padding: 11px 12px;
    background: rgba(255,255,255,0.35);
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
}

.repo-issue-table td {
    padding: 11px 12px;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
}

.repo-issue-table th:first-child, .repo-issue-table td:first-child { padding-left: 20px; }
.repo-issue-table th:last-child, .repo-issue-table td:last-child { padding-right: 20px; }

.repo-issue-table tbody tr:last-child td { border-bottom: none; }

.repo-issue-table tbody tr:nth-child(even) td { background: rgba(255,255,255,0.22); }

.repo-issue-table tbody tr:hover td { background: rgba(255,255,255,0.6); }

.issue-title-cell a {
    color: var(--ink);
    font-weight: 600;
    text-decoration: none;
}

.issue-title-cell a:hover { text-decoration: underline; }

.issue-row-action {
    text-decoration: none;
    font-weight: 600;
    color: var(--ink);
    white-space: nowrap;
}

.issue-row-action:hover { text-decoration: underline; }

.empty-state {
    padding: 60px 30px;
    text-align: center;
    border: 1px dashed rgba(51,50,46,0.18);
    border-radius: 18px;
    background: rgba(255,255,255,0.4);
    -webkit-backdrop-filter: blur(18px) saturate(150%);
    backdrop-filter: blur(18px) saturate(150%);
}

.empty-icon { font-size: 26px; margin-bottom: 12px; }
.empty-title { font-size: 17px; font-weight: 700; }
.empty-text { color: var(--muted); margin-top: 8px; font-size: 13.5px; }

[data-testid="stDownloadButton"] > button {
    background: var(--dark-pill) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
}

[data-testid="stButton"] > button {
    background: rgba(255,255,255,0.75) !important;
    border: 1px solid var(--high-text) !important;
    color: var(--high-text) !important;
    font-weight: 600 !important;
    border-radius: 10px !important;
    min-height: 42px !important;
}

[data-testid="stButton"] > button:hover {
    background: var(--high-tint) !important;
}

[data-testid="stButton"] {
    margin-top: 8px;
    margin-bottom: 8px;
}

/* ---- Fixed-viewport layout: page never scrolls.
   Both columns are fixed-height with their own scroll surfaces.
   The 11rem offset = ~50px Streamlit chrome + 8px container top +
   ~120px logo masthead + ~14px masthead padding + ~20px footer gap.
   Adjust only this value if the columns sit too high/low. ---- */

html, body {
    overflow: hidden !important;
}

[data-testid="stAppViewContainer"],
[data-testid="stMain"] {
    overflow: hidden !important;
    height: 100% !important;
}

/* Results column — matches left column height exactly */
[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(2):not([data-testid="stColumn"] [data-testid="stColumn"]) {
    max-height: calc(100vh - 11rem);
    overflow-y: auto;
    scrollbar-width: none;
}

[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:nth-of-type(2):not([data-testid="stColumn"] [data-testid="stColumn"])::-webkit-scrollbar { display: none; }

/* ---- Token validation status ---- */
.token-status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 999px;
    margin-top: 4px;
    margin-bottom: 6px;
}
.token-status.valid   { background: var(--low-tint);  color: var(--low-text); }
.token-status.invalid { background: var(--high-tint); color: var(--high-text); }

/* ---- Rate-limit pill in results header ---- */
.rl-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 600;
    color: var(--muted);
    background: rgba(255,255,255,0.55);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 2px 9px;
    vertical-align: middle;
    margin-left: 8px;
    letter-spacing: 0;
}

/* ---- Issue row icon buttons (copy + bookmark) ---- */
.icon-btn {
    background: none;
    border: 1px solid transparent;
    cursor: pointer;
    padding: 2px 5px;
    border-radius: 6px;
    font-size: 13px;
    line-height: 1;
    color: var(--muted);
    vertical-align: middle;
    transition: background .12s, color .12s, border-color .12s;
}
.repo-bm-btn {
    font-size: 18px;
    padding: 2px 7px;
    color: var(--muted);
}
.repo-bm-btn.bm-active { color: #f5a623; }
.icon-btn:hover          { background: rgba(51,50,46,0.07); color: var(--ink); }
.icon-btn.bm-active      { color: #c0902a; }
.icon-btn.copy-ok        { color: var(--low-text); }
.col-actions-wide        { width: 10%; }

/* ---- Bookmarks panel ---- */
.bm-panel {
    background: rgba(255,253,248,0.90);
    -webkit-backdrop-filter: blur(18px) saturate(140%);
    backdrop-filter: blur(18px) saturate(140%);
    border: 1px solid rgba(200,185,155,0.45);
    border-left: 3px solid #c0902a;
    border-radius: 12px;
    padding: 12px 16px 10px;
    margin-bottom: 14px;
    box-shadow: 0 4px 20px rgba(60,40,10,0.06);
}
.bm-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
}
.bm-panel-title {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: #8a6020;
}
.bm-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 0;
    border-bottom: 1px solid var(--line);
    font-size: 12.5px;
}
.bm-item:last-child { border-bottom: none; }
.bm-item-star { color: #c0902a; flex-shrink: 0; }
.bm-item a    { color: var(--ink); text-decoration: none; flex: 1; min-width: 0;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bm-item a:hover { text-decoration: underline; }
.bm-remove { flex-shrink: 0; font-size: 11px; color: var(--muted); }

/* ---- Compact export button override ---- */
[data-testid="stDownloadButton"] > button {
    font-size: 12px !important;
    padding: 6px 12px !important;
    min-height: 34px !important;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# LOCAL-STORAGE TOKEN PERSISTENCE (best-effort, obfuscated)
# ============================================================

# ── File-based PAT cache ──────────────────────────────────────────────────────
# The PAT is saved to a small file next to app.py so Python can restore it on
# startup without needing a JavaScript bridge.  The file also acts as a fallback
# when the user opens the app in a browser that hasn't stored anything yet.
# The file is obfuscated (not encrypted); keep it out of version control via
# .gitignore (.oss_pat is already listed there).
_PAT_CACHE_FILE = Path(__file__).parent / ".oss_pat"
_PAT_CACHE_KEY  = "oss-scout-local-key"


def _xor_bytes(data: bytes, key: str) -> bytes:
    kb = key.encode()
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(data))


def _save_pat_to_file(tok: str) -> None:
    """Obfuscate and write the PAT to the local cache file."""
    import base64
    try:
        encoded = base64.b64encode(_xor_bytes(tok.encode("utf-8"), _PAT_CACHE_KEY)).decode()
        _PAT_CACHE_FILE.write_text(encoded, encoding="ascii")
    except Exception:
        pass


def _load_pat_from_file() -> str:
    """Read and decode the PAT from the local cache file."""
    import base64
    try:
        raw = _PAT_CACHE_FILE.read_text(encoding="ascii").strip()
        return _xor_bytes(base64.b64decode(raw), _PAT_CACHE_KEY).decode("utf-8")
    except Exception:
        return ""


def _delete_pat_file() -> None:
    try:
        _PAT_CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass



def sync_token_with_local_storage(token: str, remember: bool):
    """Write (or delete) the PAT in localStorage.

    The token value is passed from Python as a JSON string — no DOM scraping.
    window.parent.document access from a components.html srcdoc iframe can
    fail with a cross-origin SecurityError in some browsers; window.parent
    .localStorage does not have that problem and works reliably.

    Rules:
    • remember=True  + token present → encode and save.
    • remember=False               → delete (regardless of whether token present).
    • remember=True  + no token    → nothing to save; leave existing entry alone.
    """
    import json as _j
    tok = token.strip()

    # Guard: if the token field and session state are both empty we're in
    # cold-load state — don't run the delete branch and wipe the key before
    # the JS injection has had a chance to read and fill it.
    if not tok and not st.session_state.get("_oss_tok"):
        return

    token_json   = _j.dumps(tok)          # safely quoted JS string
    remember_js  = "true" if remember else "false"

    script = f"""
<script>
(function() {{
    const STORAGE_KEY = "oss_scout_pat_v1";
    const XOR_KEY     = "oss-scout-local-key";
    const TOKEN       = {token_json};
    const REMEMBER    = {remember_js};

    function xorCipher(str, key) {{
        let out = "";
        for (let i = 0; i < str.length; i++) {{
            out += String.fromCharCode(str.charCodeAt(i) ^ key.charCodeAt(i % key.length));
        }}
        return out;
    }}
    function encode(value) {{
        try {{ return btoa(unescape(encodeURIComponent(xorCipher(value, XOR_KEY)))); }}
        catch (e) {{ return ""; }}
    }}

    try {{
        if (!REMEMBER) {{
            // User deliberately unchecked — delete the saved entry.
            window.parent.localStorage.removeItem(STORAGE_KEY);
        }} else if (TOKEN) {{
            const enc = encode(TOKEN);
            if (enc) window.parent.localStorage.setItem(STORAGE_KEY, enc);
        }}
    }} catch (e) {{}}
}})();
</script>
"""

    components.html(script, height=0)

    # Mirror the save/delete to the Python-side cache file so the token
    # can be restored on next startup without needing a JS bridge.
    if not remember:
        _delete_pat_file()
    elif tok:
        _save_pat_to_file(tok)


def inject_language_dots():
    """Replace the plain ● prefix in BaseWeb multiselect option labels and
    selected tags with a fixed-size accent-coloured circle.

    Approach: for every [role="option"] and [data-baseweb="tag"] in the
    parent document, find the innermost element whose trimmed text starts
    with ● and whose remainder is a known language name, then rewrite its
    innerHTML with a background-circle <span> + the language name.
    A periodic rescan (100 ms) catches virtualized / lazily-rendered rows
    that the MutationObserver might receive in a batch after first render."""
    import json as _json
    colors_js = _json.dumps(LANGUAGE_COLORS)
    script = f"""
<script>
(function() {{
    var COLORS = {colors_js};
    var doc = window.parent.document;
    var DOT = "\\u25CF";  /* ● */

    function dot(color) {{
        return '<span style="display:inline-block;width:9px;height:9px;'
             + 'border-radius:50%;background:' + color + ';'
             + 'vertical-align:middle;flex-shrink:0;'
             + 'margin-right:9px;position:relative;top:-1px;"></span>';
    }}

    function tryColor(el) {{
        if (!el || el.dataset.ldDone) return;
        var raw = (el.innerText || el.textContent || "").trim();
        if (raw.charAt(0) !== DOT) return;
        var lang = raw.slice(1).trim();
        var color = COLORS[lang];
        if (!color) return;
        el.dataset.ldDone = "1";
        el.innerHTML = dot(color) + lang;
    }}

    function scan() {{
        /* ---- dropdown list options ---- */
        doc.querySelectorAll('[role="option"]').forEach(function(opt) {{
            if (opt.dataset.ldDone) return;
            var colored = false;
            /* walk children deepest-first to find the text node container */
            var all = Array.from(opt.querySelectorAll('span,div,li'));
            /* prefer innermost: sort by depth (more ancestors = deeper) */
            all.sort(function(a, b) {{
                var da = 0, db = 0, n;
                n = a; while (n !== opt) {{ da++; n = n.parentElement; }}
                n = b; while (n !== opt) {{ db++; n = n.parentElement; }}
                return db - da;
            }});
            for (var i = 0; i < all.length; i++) {{
                var raw = (all[i].innerText || all[i].textContent || "").trim();
                if (raw.charAt(0) === DOT) {{
                    var lang = raw.slice(1).trim();
                    if (COLORS[lang]) {{ tryColor(all[i]); opt.dataset.ldDone = "1"; colored = true; }}
                    break;
                }}
            }}
            /* fallback: try the option element itself */
            if (!colored) tryColor(opt);
        }});

        /* ---- selected tag chips ---- */
        doc.querySelectorAll('[data-baseweb="tag"]').forEach(function(tag) {{
            var spans = tag.querySelectorAll('span');
            spans.forEach(tryColor);
        }});
    }}

    scan();
    /* Debounced observer - DOM writes inside scan() would otherwise
       re-fire the observer immediately, creating a tight mutation loop. */
    var _dotsTimer = null;
    var obs = new MutationObserver(function() {{
        if (_dotsTimer) return;
        _dotsTimer = setTimeout(function() {{ _dotsTimer = null; scan(); }}, 120);
    }});
    obs.observe(doc.body, {{ childList: true, subtree: true }});
    /* Periodic fallback for virtualised rows - self-stops after ~2 s so it
       does not keep running (and firing the observer) during a long scan. */
    var _dotRounds = 0;
    var _dotInterval = setInterval(function() {{
        scan();
        if (++_dotRounds >= 12) clearInterval(_dotInterval);
    }}, 180);
}})();
</script>
"""
    components.html(script, height=0)


def inject_bookmarks():
    """Client-side bookmarks stored in localStorage.
    Wires click handlers on [data-bm-url] (star) and [data-copy-url] (copy)
    buttons embedded in the results HTML, and renders a bookmark panel above
    the first repo card whenever saved items exist."""
    script = """
<script>
(function() {
    var doc = window.parent.document;
    var BM_KEY  = "oss_scout_bookmarks_v1";
    var _busy = false;

    function loadBm()  {
        try { return JSON.parse(window.parent.localStorage.getItem(BM_KEY) || "[]"); }
        catch(e) { return []; }
    }
    function saveBm(list) {
        try { window.parent.localStorage.setItem(BM_KEY, JSON.stringify(list)); }
        catch(e) {}
    }

    /* ── Sync ★/☆ state on all visible bookmark buttons ── */
    function syncBtns() {
        var saved = loadBm().map(function(b) { return b.url; });
        doc.querySelectorAll('[data-bm-url]').forEach(function(btn) {
            if (saved.indexOf(btn.dataset.bmUrl) >= 0) {
                btn.textContent = "★";
                btn.classList.add("bm-active");
            } else {
                btn.textContent = "☆";
                btn.classList.remove("bm-active");
            }
        });
    }

    /* ── Bookmarks panel ── */
    function renderPanel() {
        if (_busy) return;
        _busy = true;
        try {
            var list = loadBm();
            var panelId = "oss-bm-panel";

            if (!list.length) {
                var ex = doc.getElementById(panelId);
                if (ex) ex.remove();
                return;
            }

            var items = list.map(function(b) {
                var safe = b.title.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
                var safeUrl = b.url.replace(/"/g,"&quot;");
                return '<div class="bm-item">'
                    + '<span class="bm-item-star">★</span>'
                    + '<a href="' + safeUrl + '" target="_blank" rel="noopener">' + safe + '</a>'
                    + '<button class="icon-btn bm-remove" data-rm-url="' + safeUrl + '" title="Remove">✕</button>'
                    + '</div>';
            }).join("");

            var inner = '<div class="bm-panel-header">'
                + '<span class="bm-panel-title">★ Bookmarks (' + list.length + ')</span>'
                + '<button class="icon-btn" id="oss-bm-clear" title="Clear all" style="font-size:11px;">Clear all</button>'
                + '</div>'
                + items;

            /* Update innerHTML instead of replacing outerHTML - keeps the element
               alive so clicks on links inside the panel are never interrupted. */
            var panel = doc.getElementById(panelId);
            if (panel) {
                panel.innerHTML = inner;
            } else {
                panel = doc.createElement('div');
                panel.id = panelId;
                panel.className = 'bm-panel';
                panel.innerHTML = inner;
                var anchor = doc.querySelector('.repo-card') || doc.querySelector('.empty-state');
                if (anchor) anchor.parentNode.insertBefore(panel, anchor);
                else return;
            }

            panel.querySelectorAll('[data-rm-url]').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    var url = btn.dataset.rmUrl;
                    saveBm(loadBm().filter(function(b) { return b.url !== url; }));
                    syncBtns();
                    renderPanel();
                });
            });

            var clr = doc.getElementById('oss-bm-clear');
            if (clr) {
                clr.addEventListener('click', function() {
                    saveBm([]);
                    syncBtns();
                    renderPanel();
                });
            }
        } finally {
            setTimeout(function() { _busy = false; }, 120);
        }
    }

    /* ── Toggle bookmark ── */
    function toggleBookmark(btn) {
        var url   = btn.dataset.bmUrl;
        var title = btn.dataset.bmTitle || url;
        var list  = loadBm();
        var idx   = -1;
        for (var i = 0; i < list.length; i++) { if (list[i].url === url) { idx = i; break; } }
        if (idx >= 0) { list.splice(idx, 1); } else { list.push({ url: url, title: title }); }
        saveBm(list);
        syncBtns();
        renderPanel();
    }

    /* ── Copy link ── */
    function copyUrl(btn) {
        var url = btn.dataset.copyUrl;
        function flashOk() {
            var orig = btn.textContent;
            btn.textContent = "✓";
            btn.classList.add("copy-ok");
            setTimeout(function() {
                btn.textContent = orig;
                btn.classList.remove("copy-ok");
            }, 1500);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(flashOk).catch(function() {
                fallbackCopy(url, flashOk);
            });
        } else {
            fallbackCopy(url, flashOk);
        }
    }

    function fallbackCopy(text, cb) {
        var ta = doc.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
        doc.body.appendChild(ta);
        ta.focus();
        ta.select();
        try { doc.execCommand("copy"); } catch(e) {}
        doc.body.removeChild(ta);
        if (cb) cb();
    }

    /* ── Attach handlers to new buttons ── */
    function setupBtns() {
        doc.querySelectorAll('[data-bm-url]:not([data-bm-bound])').forEach(function(btn) {
            btn.dataset.bmBound = "1";
            btn.addEventListener('click', function(e) { e.preventDefault(); e.stopPropagation(); toggleBookmark(btn); });
        });
        doc.querySelectorAll('[data-copy-url]:not([data-copy-bound])').forEach(function(btn) {
            btn.dataset.copyBound = "1";
            btn.addEventListener('click', function() { copyUrl(btn); });
        });
        syncBtns();
    }

    /* ── Boot ── */
    setupBtns();
    renderPanel();

    /* Debounced MutationObserver: rapid-fire DOM changes during scan
       (progress bar, caption updates) collapse into one deferred call
       instead of spinning in a tight mutation → DOM-write → mutation loop. */
    var _obsTimer = null;
    var obs = new MutationObserver(function() {
        if (_obsTimer) return;
        _obsTimer = setTimeout(function() {
            _obsTimer = null;
            setupBtns();
            if (!_busy) renderPanel();
        }, 120);
    });
    obs.observe(doc.body, { childList: true, subtree: true });
})();
</script>
"""
    components.html(script, height=0)


def inject_pat_help_modal():
    """Inject a friendly PAT-creation guide modal triggered by the ? icon
    next to the token field.  The icon is also recoloured to a warm red so
    it catches the eye.  Everything runs client-side; no data ever leaves
    the browser."""
    import json as _json
    label_js = _json.dumps(TOKEN_LABEL)

    script = f"""
<script>
(function() {{
    var TOKEN_LABEL = {label_js};
    var doc = window.parent.document;

    /* ── Modal skeleton ───────────────────────────────────────────── */
    var MODAL_ID = "oss-pat-overlay";
    if (!doc.getElementById(MODAL_ID)) {{
        var wrap = doc.createElement("div");
        wrap.id = MODAL_ID;
        wrap.style.cssText = [
            "display:none", "position:fixed", "inset:0", "z-index:999999",
            "background:rgba(30,25,20,0.52)",
            "backdrop-filter:blur(6px)",
            "-webkit-backdrop-filter:blur(6px)",
            "align-items:center", "justify-content:center"
        ].join(";");

        wrap.innerHTML = `
<div style="
    background:rgba(255,253,250,0.97);
    border:1px solid rgba(200,190,180,0.5);
    border-radius:18px;
    padding:30px 32px 26px;
    max-width:500px;
    width:calc(100% - 32px);
    position:relative;
    box-shadow:0 28px 80px rgba(60,40,20,0.18);
    color:#2e2c28;
    font-family:system-ui,-apple-system,sans-serif;
    font-size:14px;
    line-height:1.6;
">
  <!-- close -->
  <button id="oss-pat-close" style="
      position:absolute;top:14px;right:16px;
      background:none;border:none;color:#888;
      font-size:22px;cursor:pointer;line-height:1;
      padding:2px 6px;border-radius:6px;
  " aria-label="Close">&times;</button>

  <!-- header -->
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
    <span style="font-size:22px;">🔑</span>
    <h2 style="margin:0;font-size:16px;font-weight:700;color:#1a1816;">
      How to create a GitHub Personal Access Token
    </h2>
  </div>
  <p style="margin:0 0 18px;color:#706860;font-size:13px;">
    A token is optional but strongly recommended — it raises GitHub's
    rate limit from&nbsp;<strong>60</strong>&nbsp;to&nbsp;<strong>5,000</strong>&nbsp;requests/hour.
  </p>

  <!-- steps -->
  <div style="
      background:rgba(240,235,228,0.55);
      border-radius:10px;padding:14px 16px;margin-bottom:14px;
      border:1px solid rgba(200,190,180,0.4);
  ">
    <div style="font-weight:600;color:#2e2c28;margin-bottom:10px;">
      Steps (takes about 60 seconds)
    </div>
    <ol style="margin:0;padding-left:20px;color:#4a4640;">
      <li style="margin-bottom:5px;">
        Open&nbsp;<a href="https://github.com/settings/tokens/new"
            target="_blank"
            style="color:#c0392b;text-decoration:underline;">
          github.com/settings/tokens/new</a>
      </li>
      <li style="margin-bottom:5px;">
        Give it a name, e.g.&nbsp;
        <code style="background:rgba(0,0,0,0.07);padding:1px 5px;border-radius:4px;">OSS Scout</code>
      </li>
      <li style="margin-bottom:5px;">Set an expiration (90 days is a sensible default)</li>
      <li style="margin-bottom:5px;">
        Under <strong>Select scopes</strong>, tick only&nbsp;
        <code style="background:rgba(0,0,0,0.07);padding:1px 5px;border-radius:4px;">public_repo</code>
        &nbsp;— that is all OSS Scout ever needs
      </li>
      <li style="margin-bottom:5px;">Click <strong>Generate token</strong> at the bottom</li>
      <li>Copy the token immediately — GitHub won't show it again</li>
    </ol>
  </div>

  <!-- privacy -->
  <div style="
      background:rgba(40,160,80,0.06);
      border:1px solid rgba(40,160,80,0.2);
      border-radius:10px;padding:11px 14px;margin-bottom:10px;
  ">
    <div style="font-weight:600;color:#1e6e3a;margin-bottom:3px;">🔒 Your token never leaves your device</div>
    <p style="margin:0;color:#2e5c3a;font-size:13px;">
      OSS Scout has <strong>no backend server</strong>. Every GitHub API call is made
      directly from your browser to GitHub — we never see your token.
      Enabling "Remember" saves it only in <em>this browser's local storage</em>,
      lightly encoded, and only on this device.
    </p>
  </div>

  <!-- min-permissions -->
  <div style="
      background:rgba(200,140,0,0.06);
      border:1px solid rgba(200,140,0,0.22);
      border-radius:10px;padding:11px 14px;margin-bottom:20px;
  ">
    <div style="font-weight:600;color:#7a5000;margin-bottom:3px;">⚡ Minimal permissions = minimal risk</div>
    <p style="margin:0;color:#6a4800;font-size:13px;">
      With only <code style="background:rgba(0,0,0,0.07);padding:1px 5px;border-radius:4px;">public_repo</code>
      selected, the token can only read public repositories — it cannot
      write, delete, or access any private content.
      You can revoke it any time from GitHub's token settings.
    </p>
  </div>

  <!-- CTA -->
  <div style="text-align:center;">
    <a href="https://github.com/settings/tokens/new" target="_blank" style="
        display:inline-block;
        background:linear-gradient(135deg,#c0392b,#e05050);
        color:#fff;text-decoration:none;
        padding:10px 24px;border-radius:9px;
        font-weight:600;font-size:13px;
        box-shadow:0 4px 14px rgba(192,57,43,0.28);
        letter-spacing:0.01em;
    ">Open GitHub → Create token</a>
  </div>
</div>
        `;
        doc.body.appendChild(wrap);

        /* close handlers */
        doc.getElementById("oss-pat-close").addEventListener("click", closeModal);
        wrap.addEventListener("click", function(e) {{
            if (e.target === wrap) closeModal();
        }});
        if (!doc.body.dataset.ossPatEsc) {{
            doc.body.dataset.ossPatEsc = "1";
            doc.addEventListener("keydown", function(e) {{
                if (e.key === "Escape") closeModal();
            }});
        }}
    }}

    function openModal()  {{ var o = doc.getElementById(MODAL_ID); if (o) o.style.display = "flex"; }}
    function closeModal() {{ var o = doc.getElementById(MODAL_ID); if (o) o.style.display = "none"; }}

    /* ── Find the ? icon and attach handler ──────────────────────────── */
    function setupBtn() {{
        /* Search DOWN from the stTextInput container that holds TOKEN_LABEL,
           not up from every stTooltipIcon on the page — avoids matching
           sibling widgets that share a common ancestor containing the text. */
        doc.querySelectorAll('[data-testid="stTextInput"]').forEach(function(container) {{
            if ((container.textContent || "").indexOf(TOKEN_LABEL) === -1) return;
            var btn = container.querySelector('[data-testid="stTooltipIcon"]');
            if (!btn || btn.dataset.ossPatBound) return;

            btn.dataset.ossPatBound = "1";
            btn.style.setProperty("color", "#c0392b", "important");

            /* Capture-phase listener fires before Streamlit's own tooltip */
            btn.addEventListener("click", function(e) {{
                e.preventDefault();
                e.stopImmediatePropagation();
                openModal();
            }}, true);
        }});
    }}

    setupBtn();
    var obs = new MutationObserver(setupBtn);
    obs.observe(doc.body, {{ childList: true, subtree: true }});
}})();
</script>
"""
    components.html(script, height=0)


def inject_logo():
    """Inject the logo SVG into both the browser favicon and the masthead.

    Streamlit's HTML sanitizer strips data: URIs from <img src> attributes
    even with unsafe_allow_html=True, so passing the logo via st.markdown
    silently produces a broken image. Injecting via components.html avoids
    that sanitizer entirely — the script runs in an iframe but writes into
    window.parent.document, which Streamlit serves on the same origin.

    Two targets:
      1. <link rel="icon"> in <head> — replaces Streamlit's own favicon.
      2. .masthead-identity div — prepends the <img> so the logo sits left
         of the title. A MutationObserver covers the case where the masthead
         hasn't rendered yet when the script first fires.
    """
    if not _logo_data_uri:
        return

    script = f"""
<script>
(function() {{
    const doc = window.parent.document;
    const DATA_URI = "{_logo_data_uri}";

    // --- Favicon ---
    doc.querySelectorAll('link[rel~="icon"]').forEach(function(el) {{ el.remove(); }});
    const link = doc.createElement("link");
    link.rel  = "icon";
    link.type = "image/svg+xml";
    link.href = DATA_URI;
    doc.head.appendChild(link);

    // --- Masthead logo ---
    function tryInjectLogo() {{
        const identity = doc.querySelector(".masthead-identity");
        if (!identity || identity.dataset.ossLogo) return;
        identity.dataset.ossLogo = "1";
        const img = doc.createElement("img");
        img.src       = DATA_URI;
        img.className = "masthead-logo";
        img.alt       = "OSS Scout";
        identity.insertBefore(img, identity.firstChild);
    }}

    tryInjectLogo();

    // Watch for the masthead in case it isn't in the DOM yet.
    const obs = new MutationObserver(function() {{ tryInjectLogo(); }});
    obs.observe(doc.body, {{ childList: true, subtree: true }});
}})();
</script>
"""
    components.html(script, height=0)


def inject_language_icons():
    """Inject real devicon SVG icons into the Language multiselect via
    a MutationObserver running in the parent page. Two targets:

    1. Dropdown list options (the `ul[role="listbox"] li` items that
       appear when the widget is open) - prepend a small img before
       each option's text so the picker shows [icon] Python, etc.

    2. Selected-value pills ([data-baseweb="tag"]) that are already
       visible above the dropdown - same treatment so the pill also
       shows [icon] Python instead of plain text.

    The observer fires on every DOM mutation so new pills added when
    the user selects a language are covered without an extra call.
    """
    import json as _json
    icon_map_js = _json.dumps(LANGUAGE_ICON_URLS)

    script = f"""
<script>
(function() {{
    const ICON_MAP = {icon_map_js};
    const doc = window.parent.document;

    function imgFor(url) {{
        const img = doc.createElement("img");
        img.src = url;
        img.style.cssText = (
            "width:15px;height:15px;margin-right:6px;vertical-align:middle;" +
            "border-radius:3px;flex-shrink:0;display:inline-block;"
        );
        return img;
    }}

    // Dropdown list options (lazy-rendered when the widget opens).
    function injectListOptions(root) {{
        root.querySelectorAll('li[role="option"]').forEach(function(item) {{
            if (item.dataset.ossIcon) return;
            // The visible text is in the first non-empty span child.
            const span = Array.from(item.querySelectorAll("span"))
                .find(function(s) {{ return s.textContent.trim() in ICON_MAP; }});
            if (!span) return;
            item.dataset.ossIcon = "1";
            span.insertBefore(imgFor(ICON_MAP[span.textContent.trim()]), span.firstChild);
        }});
    }}

    // Selected pills rendered above the input ([data-baseweb="tag"]).
    function injectPills() {{
        doc.querySelectorAll('[data-baseweb="tag"]').forEach(function(tag) {{
            if (tag.dataset.ossIcon) return;
            const span = Array.from(tag.querySelectorAll("span"))
                .find(function(s) {{ return s.textContent.trim() in ICON_MAP; }});
            if (!span) return;
            tag.dataset.ossIcon = "1";
            span.insertBefore(imgFor(ICON_MAP[span.textContent.trim()]), span.firstChild);
        }});
    }}

    const obs = new MutationObserver(function(mutations) {{
        let checkPills = false;
        for (const m of mutations) {{
            for (const node of m.addedNodes) {{
                if (node.nodeType !== 1) continue;
                if (node.getAttribute && node.getAttribute("role") === "listbox") {{
                    injectListOptions(node);
                }} else if (node.querySelector) {{
                    const lb = node.querySelector('[role="listbox"]');
                    if (lb) injectListOptions(lb);
                }}
                checkPills = true;
            }}
        }}
        if (checkPills) injectPills();
    }});

    /* Wrap in a debounce so rapid DOM updates during a scan (progress bar,
       caption etc.) don't spin the observer on every frame. */
    var _iconsTimer = null;
    var _rawObs = obs;
    var obs2 = new MutationObserver(function(mutations) {{
        if (_iconsTimer) return;
        _iconsTimer = setTimeout(function() {{
            _iconsTimer = null;
            // replay the original handler logic
            let checkPills = false;
            for (const m of mutations) {{
                for (const node of m.addedNodes) {{
                    if (node.nodeType !== 1) continue;
                    if (node.getAttribute && node.getAttribute("role") === "listbox") {{
                        injectListOptions(node);
                    }} else if (node.querySelector) {{
                        const lb = node.querySelector('[role="listbox"]');
                        if (lb) injectListOptions(lb);
                    }}
                    checkPills = true;
                }}
            }}
            if (checkPills) injectPills();
        }}, 120);
    }});
    obs2.observe(doc.body, {{ childList: true, subtree: true }});

    // Run once immediately for pills already in the DOM on load.
    injectPills();
}})();
</script>
"""
    components.html(script, height=0)


# ============================================================
# HEADER
# ============================================================

st.markdown(
    '<div class="masthead">'
    '<div class="masthead-identity">'
    '<div>'
    '<div class="app-title">OSS <strong>Scout</strong></div>'
    '<div class="app-subtitle">Find your next open-source contribution.</div>'
    '</div>'
    '</div>'
    '</div>',
    unsafe_allow_html=True,
)


# ============================================================
# TWO-PANE LAYOUT - controls on the left, results feed on the
# right. No st.sidebar anywhere; this is a plain st.columns split
# styled to look like two custom panels, not default Streamlit chrome.
# ============================================================

col_left, col_right = st.columns([2, 3], gap="large")

# ── Restore PAT from local cache file (set once per fresh session) ──────────
if "_oss_pat_loaded" not in st.session_state and not st.session_state.get("_oss_tok"):
    st.session_state["_oss_pat_loaded"] = True
    _cached_tok = _load_pat_from_file()
    if _cached_tok:
        st.session_state["_oss_tok"] = _cached_tok
        st.session_state["_oss_rem"] = True

with col_left:

    # Open the settings panel on first render so the password input is in the
    # DOM and the localStorage injection can find it.  Once the token lands in
    # session state the expander stays closed on subsequent renders.
    _exp_open = not bool(st.session_state.get("_oss_tok"))
    with st.expander("⚙ GitHub API settings", expanded=_exp_open):

        st.caption(
            "A PAT is optional but strongly recommended - without one, "
            "GitHub's public API has a much smaller rate limit."
        )

        token = st.text_input(
            TOKEN_LABEL,
            key="_oss_tok",
            type="password",
            placeholder=TOKEN_PLACEHOLDER,
            help="Use a token that can read public repositories.",
        )

        remember_token = st.checkbox(
            REMEMBER_LABEL,
            key="_oss_rem",
            value=False,
            help=(
                "Checking this saves your token to this browser's local storage; "
                "it will auto-fill next time you open the app."
                " Unchecking deletes the saved entry immediately."
            ),
        )

        st.caption(
            "Saved to this browser's local storage, lightly obfuscated - "
            "not real encryption. Only enable this on a device you trust."
        )

        if token.strip():
            _t = token.strip()
            _cache = st.session_state.get("oss_scout_token_val", {})
            if _cache.get("token") != _t:
                # Token changed — re-validate against the real API.
                try:
                    _quota = GitHubClient(_t).rate_limit()
                    st.session_state["oss_scout_token_val"] = {
                        "token": _t, "valid": True,
                        "remaining": _quota.get("remaining"),
                        "limit": _quota.get("limit"),
                    }
                except Exception as _exc:
                    st.session_state["oss_scout_token_val"] = {
                        "token": _t, "valid": False, "error": str(_exc),
                    }
            _v = st.session_state.get("oss_scout_token_val", {})
            if _v.get("valid"):
                _r = _v.get("remaining", 0)
                _lim = _v.get("limit", 5000)
                st.markdown(
                    f'<div class="token-status valid">'
                    f'✓ Token valid &nbsp;·&nbsp; {_r:,} / {_lim:,} req remaining'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            elif _v.get("token"):
                st.markdown(
                    '<div class="token-status invalid">✗ Invalid token or API error</div>',
                    unsafe_allow_html=True,
                )

    sync_token_with_local_storage(token, remember_token)
    inject_pat_help_modal()
    inject_bookmarks()
    inject_language_icons()
    inject_logo()
    inject_language_dots()

    with st.form("filters_form"):

        st.markdown('<div class="section-label">Repository</div>', unsafe_allow_html=True)

        languages = st.multiselect(
            "Language (none selected = any)",
            LANGUAGE_OPTIONS,
            default=["Python"],
            format_func=lambda x: f"{LANGUAGE_DOT.get(x, '⚫')} {x}",
        )

        repo_activity = st.selectbox(
            "Repo last updated within",
            [None, 30, 90, 180, 365],
            format_func=lambda x: "Any time" if x is None else f"{x} days",
            help="Only include repos whose last activity (push, PR, issue, etc.) falls within this window.",
        )

        r2c1, r2c2 = st.columns(2)
        with r2c1:
            min_stars = st.number_input("Minimum stars", min_value=0, value=500, step=100)
        with r2c2:
            max_stars = st.number_input("Maximum stars", min_value=1, value=2000, step=100)

        st.markdown('<div class="section-label">Issues</div>', unsafe_allow_html=True)

        labels = st.multiselect(
            "Labels (any match)",
            ["good first issue", "good first pr", "help wanted"],
            default=["good first issue"],
        )

        _custom_raw = st.text_input(
            "Custom labels",
            placeholder="e.g. bug, hacktoberfest, easy",
            help="Add any label not in the list above. Separate multiple labels with commas.",
            label_visibility="visible",
        )
        # Merge custom labels (split on comma, strip whitespace, drop empties)
        _custom = [l.strip() for l in _custom_raw.split(",") if l.strip()]
        labels = list(dict.fromkeys(labels + _custom))  # dedupe, preserve order

        r3c1, r3c2 = st.columns(2)
        with r3c1:
            max_comments = st.number_input("Maximum comments", min_value=0, value=10, step=1)
        with r3c2:
            issue_age = st.selectbox(
                "Opened within",
                [None, 7, 30, 60, 90],
                format_func=lambda x: "Any" if x is None else f"Last {x} days",
            )

        st.markdown('<div class="section-label">Display</div>', unsafe_allow_html=True)

        r4c1, r4c2 = st.columns(2)
        with r4c1:
            require_unassigned = st.checkbox("No assignee", value=True)
        with r4c2:
            require_no_pr = st.checkbox("No linked PRs", value=True)

        max_results = st.selectbox(
            "Issue limit", [10, 25, 50, 100], index=1,
            format_func=lambda x: f"Up to {x} issues",
        )

        st.markdown("<br>", unsafe_allow_html=True)

        search = st.form_submit_button(
            "🔍  Find issues",
            type="primary",
            use_container_width=True,
        )


with col_right:

    # --------------------------------------------------------
    # SEARCH - issue-first strategy.
    #
    # Phase 1 (init): query /search/issues directly for issues that
    # already match label / language / comments / assignee / linked-PR /
    # age criteria - GitHub does the hard filtering, not us. This costs
    # only O(labels × languages) API calls instead of O(repos).
    #
    # Phase 2 (incremental ticks): for each unique repository that
    # appeared in the results, fetch its details (star count, last
    # activity) and discard repos outside the user's filters. State
    # lives in st.session_state so it survives the reruns each tick
    # triggers, and the pause button works throughout phase 2.
    # --------------------------------------------------------

    if search:

        if min_stars > max_stars:
            st.markdown(
                render_banner("error", "✕", "Minimum stars cannot be greater than maximum stars."),
                unsafe_allow_html=True,
            )
            st.stop()

        _lang_str  = ", ".join(languages) if languages else "any language"
        _label_str = ", ".join(labels)    if labels    else "any label"

        _prog_bar  = st.progress(0, text="Starting search…")
        _prog_text = st.empty()

        def _on_retry(attempt, wait):
            _prog_text.info(f"⏳ Rate limit hit — waiting {wait}s, retry {attempt}/{GitHubClient._BACKOFF_RETRIES}…")

        def _on_search_progress(i, total, lang):
            pct = int(i / total * 100)
            label_str = lang if lang else "any language"
            _prog_bar.progress(pct, text=f"Searching {label_str}… ({i+1}/{total})")
            _prog_text.empty()

        client = GitHubClient(token, on_retry=_on_retry, on_search_progress=_on_search_progress)

        with st.spinner(f"Searching GitHub — {_label_str} · {_lang_str}…"):
            try:
                quota = client.rate_limit()
                remaining = quota.get("remaining")
                limit = quota.get("limit")
            except RateLimitError as exc:
                st.markdown(render_banner("error", "✕", "GitHub API rate limit reached", str(exc)), unsafe_allow_html=True)
                st.stop()
            except GitHubError as exc:
                st.markdown(render_banner("error", "✕", "GitHub API error", str(exc)), unsafe_allow_html=True)
                st.stop()

            try:
                raw_issues = client.search_candidate_issues(
                    labels=labels,
                    languages=languages,
                    max_comments=max_comments,
                    require_unassigned=require_unassigned,
                    require_no_pr=require_no_pr,
                    issue_age_days=issue_age,
                    max_per_query=min(100, max_results * 4),
                )
            except RateLimitError as exc:
                st.markdown(render_banner("error", "✕", "GitHub API rate limit reached", str(exc)), unsafe_allow_html=True)
                st.stop()
            except GitHubError as exc:
                st.markdown(render_banner("error", "✕", "Could not search GitHub issues", str(exc)), unsafe_allow_html=True)
                st.stop()

        _prog_bar.empty()
        _prog_text.empty()

        skipped = getattr(client, "_search_skipped", [])
        if skipped:
            skipped_str = ", ".join(skipped)
            st.warning(
                f"⚠️ Rate limit hit mid-search — results shown are **partial**. "
                f"Languages skipped: **{skipped_str}**. "
                f"Try again in ~60s, or reduce the number of languages selected.",
                icon="⚠️",
            )

        if not raw_issues:
            st.markdown(
                render_banner(
                    "warning", "!", "No matching issues found.",
                    "Try broadening your filters — fewer labels, wider star range, or more comments allowed.",
                ),
                unsafe_allow_html=True,
            )
            st.stop()

        # Collect unique repo API URLs in the order they first appear so the
        # verification phase processes the most recently updated repos first
        # (that's the sort order /search/issues returns).
        seen_repo_url_set = set()
        pending_repos = []
        for _issue in raw_issues:
            _url = _issue.get("repository_url", "")
            if _url and _url not in seen_repo_url_set:
                seen_repo_url_set.add(_url)
                pending_repos.append(_url)

        # Clear stale results from any previous scan so they don't bleed
        # into this scan's progress UI on the first few ticks.
        st.session_state.pop("oss_scout_results", None)
        st.session_state.pop("oss_scout_meta", None)

        st.session_state["oss_scout_scan"] = {
            "active": True,
            "finalized": False,
            "paused": False,
            "stopped_early": False,
            "client": client,
            "raw_issues": raw_issues,
            "pending_repos": pending_repos,
            "seen_repos": {},
            "total": len(pending_repos),
            "repo_index": 0,
            "results": [],
            "repo_failures": [],
            "scanned": 0,
            "rate_limit_error": None,
            "quota_line": (remaining, limit) if remaining is not None and limit is not None else None,
            "filters": {
                "repo_activity": repo_activity,
                "min_stars": min_stars,
                "max_stars": max_stars,
                "require_no_pr": require_no_pr,
                "max_results": max_results,
            },
        }

    scan = st.session_state.get("oss_scout_scan")

    if scan and scan["active"]:

        f = scan["filters"]
        client = scan["client"]
        total = scan["total"]

        if scan.get("quota_line"):
            _rem, _lim = scan["quota_line"]
            st.markdown(
                render_banner("quota", "◉", f"GitHub API quota: {_rem:,} / {_lim:,} requests remaining"),
                unsafe_allow_html=True,
            )
            if _rem < 20:
                st.markdown(
                    render_banner(
                        "warning", "!",
                        "Your remaining quota is very low - this scan may stop early.",
                        "Add a GitHub PAT (or wait for the reset) for more headroom.",
                    ),
                    unsafe_allow_html=True,
                )

        # Use st.empty() placeholders so we can update progress live during
        # the while loop without triggering Streamlit reruns. This is more
        # reliable than the one-repo-per-rerun approach, which could get
        # stuck when a rerun was swallowed or a tick rendered stale UI.
        _prog = st.progress(0)
        _cap  = st.empty()

        while True:
            index = scan["repo_index"]

            # Termination check first - stops the loop cleanly when we run
            # out of repos, hit the result cap, or hit a rate limit.
            if (
                scan["rate_limit_error"]
                or len(scan["results"]) >= f["max_results"]
                or index >= total
            ):
                scan["active"] = False
                break

            repo_url = scan["pending_repos"][index]
            repo_full_name = "/".join(repo_url.split("/")[-2:])

            _prog.progress(min(int((index / total) * 100), 100) if total else 100)
            _cap.caption(f"Verifying **{repo_full_name}** · {index + 1}/{total}")

            try:
                repo = client.get_repo(repo_url)

                if repo:
                    scan["seen_repos"][repo_url] = repo
                    stars = repo.get("stargazers_count", 0)

                    if (
                        f["min_stars"] <= stars <= f["max_stars"]
                        and repository_activity_matches(repo, f["repo_activity"])
                    ):
                        scan["scanned"] += 1

                        repo_issues = [
                            iss for iss in scan["raw_issues"]
                            if iss.get("repository_url") == repo_url
                        ]

                        for issue in repo_issues:
                            score = competition_score(
                                comments=issue.get("comments", 0),
                                linked_prs=0,
                                assignees=len(issue.get("assignees") or []),
                            )

                            scan["results"].append({
                                "repo": repo["full_name"],
                                "repo_url": repo["html_url"],
                                "stars": repo.get("stargazers_count", 0),
                                "forks": repo.get("forks_count", 0),
                                "language": repo.get("language") or "",
                                "avatar_url": (repo.get("owner") or {}).get("avatar_url"),
                                "issue": issue["title"],
                                "issue_number": issue["number"],
                                "issue_url": issue["html_url"],
                                "body": (issue.get("body") or "").strip(),
                                "labels": [
                                    label.get("name", "") for label in issue.get("labels", [])
                                ],
                                "comments": issue.get("comments", 0),
                                "created_at": issue.get("created_at"),
                                "updated_at": issue.get("updated_at"),
                                "linked_prs": 0,
                                "linked_pr_list": [],
                                "pr_checked": f["require_no_pr"],
                                "assignees": len(issue.get("assignees") or []),
                                "assignee_logins": [
                                    a.get("login") for a in (issue.get("assignees") or []) if a.get("login")
                                ],
                                "competition_score": score,
                                "competition_label": competition_label(score),
                            })

                            if len(scan["results"]) >= f["max_results"]:
                                break

            except RateLimitError as exc:
                scan["rate_limit_error"] = str(exc)
            except GitHubError as exc:
                scan["repo_failures"].append((repo_full_name, str(exc)))

            scan["repo_index"] += 1

        # Clear the live progress UI now that the loop is done.
        _prog.empty()
        _cap.empty()

    if scan and not scan["active"] and not scan["finalized"]:
        scan["finalized"] = True

        st.session_state["oss_scout_results"] = scan["results"]
        st.session_state["oss_scout_meta"] = {
            "scanned": scan["scanned"],
            "repo_failures": scan["repo_failures"],
            "rate_limit_error": scan["rate_limit_error"],
            "stopped_early": scan["stopped_early"],
        }
        if scan.get("quota_line"):
            st.session_state["oss_scout_quota"] = scan["quota_line"]

    # --------------------------------------------------------
    # RENDER - always from session_state, so it persists across
    # reruns that aren't a new search.
    # --------------------------------------------------------

    if st.session_state.get("oss_scout_results") is not None:

        results = st.session_state["oss_scout_results"]
        meta = st.session_state["oss_scout_meta"]
        scanned = meta["scanned"]
        repo_failures = meta["repo_failures"]
        rate_limit_error = meta["rate_limit_error"]
        stopped_early = meta.get("stopped_early", False)

        if stopped_early:
            st.markdown(
                render_banner(
                    "warning", "⏹",
                    f"Search stopped after checking {scanned} repositories",
                    "You stopped the search early - showing partial results below. "
                    "No further GitHub API requests were made.",
                ),
                unsafe_allow_html=True,
            )
            if repo_failures:
                details_html = "".join(
                    f'<div class="fail-row"><strong>{html.escape(name)}</strong> — {html.escape(reason)}</div>'
                    for name, reason in repo_failures
                )
                st.markdown(
                    render_expandable_banner(
                        "warning", "!",
                        f"{len(repo_failures)} repositories failed before you stopped the search",
                        "",
                        details_html,
                        action_label="View failures",
                    ),
                    unsafe_allow_html=True,
                )
        elif rate_limit_error:
            details_html = "".join(
                f'<div class="fail-row"><strong>{html.escape(name)}</strong> — {html.escape(reason)}</div>'
                for name, reason in repo_failures
            ) or '<div class="fail-row">No repositories failed before the rate limit was hit.</div>'
            st.markdown(
                render_expandable_banner(
                    "error", "✕",
                    f"Scan stopped after checking {scanned} repositories",
                    f"{html.escape(rate_limit_error)} Add/use a GitHub PAT above and run the scan again.",
                    details_html,
                ),
                unsafe_allow_html=True,
            )
        elif repo_failures:
            details_html = "".join(
                f'<div class="fail-row"><strong>{html.escape(name)}</strong> — {html.escape(reason)}</div>'
                for name, reason in repo_failures
            )
            st.markdown(
                render_expandable_banner(
                    "warning", "!",
                    f"Scan completed with {len(repo_failures)} repository/API failures",
                    f"{scanned} repositories were scanned successfully.",
                    details_html,
                    action_label="View failures",
                ),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                render_banner("success", "✓", "Scan complete", f"{scanned} repositories checked."),
                unsafe_allow_html=True,
            )

        if results:

            def safe_url(url):
                """Reject non-http(s) URLs; HTML-escape for attribute context."""
                if not url or not isinstance(url, str):
                    return "#"
                if not url.startswith(("https://", "http://")):
                    return "#"
                return html.escape(url, quote=True)

            badge_class = {"LOW": "badge-low", "MEDIUM": "badge-medium", "HIGH": "badge-high"}
            badge_tooltip = {
                "LOW":    "Few comments, no one assigned, no open PRs — good chance to be first!",
                "MEDIUM": "Some discussion or activity. Worth a shot, but move fast.",
                "HIGH":   "Heavy activity — many comments, assignees, or open PRs already.",
            }

            repo_count = len({r["repo"] for r in results})

            # Render sort dropdown first (needed to derive sorted_results),
            # then fill in the other header columns — Streamlit buffers each
            # column independently so visual order matches the columns() call.
            hdr_l, hdr_export, hdr_sort = st.columns([2.8, 0.65, 1.35])

            with hdr_sort:
                sort_choice = st.selectbox(
                    "Sort",
                    list(SORT_LABELS.keys()),
                    format_func=lambda k: SORT_LABELS[k],
                    label_visibility="collapsed",
                    key="oss_scout_sort",
                )

            sorted_results = sort_results(results, sort_choice)
            repo_list      = group_by_repo(sorted_results)
            df_export      = pd.DataFrame(sorted_results)

            # Rate-limit pill (from last scan or token validation)
            _quota = st.session_state.get("oss_scout_quota")
            if not _quota:
                _tv = st.session_state.get("oss_scout_token_val", {})
                if _tv.get("valid"):
                    _quota = (_tv.get("remaining"), _tv.get("limit"))
            rl_html = ""
            if _quota:
                _rem, _lim = _quota
                if _rem is not None and _lim is not None:
                    rl_html = (
                        f' <span class="rl-pill">'
                        f'◉&nbsp;{_rem:,}&nbsp;/&nbsp;{_lim:,}&nbsp;req</span>'
                    )

            with hdr_l:
                st.markdown(
                    f'<div class="results-eyebrow">'
                    f'{repo_count} repositor{"y" if repo_count == 1 else "ies"} '
                    f'· {len(results)} issue{"" if len(results) == 1 else "s"}'
                    f'{rl_html}</div>',
                    unsafe_allow_html=True,
                )

            with hdr_export:
                st.download_button(
                    "↓ CSV",
                    data=df_export.to_csv(index=False).encode("utf-8"),
                    file_name="oss_scout_results.csv",
                    mime="text/csv",
                    use_container_width=True,
                    key="oss_scout_export",
                )

            cards_html = []
            for repo_entry in repo_list:

                avatar_url = repo_entry["avatar_url"]
                if avatar_url:
                    avatar_html = f'<img class="repo-avatar" src="{html.escape(avatar_url, quote=True)}" alt="">'
                else:
                    initial = html.escape(repo_entry["repo"].split("/")[0][:1].upper())
                    avatar_html = f'<div class="repo-avatar repo-avatar-fallback">{initial}</div>'

                accent = LANGUAGE_COLORS.get(repo_entry["language"], DEFAULT_LANGUAGE_COLOR)
                n_issues = len(repo_entry["issues"])
                repo_name_safe = html.escape(repo_entry["repo"])
                language_safe = html.escape(repo_entry["language"] or "Unknown")

                rows_html = []
                for result in repo_entry["issues"]:

                    label_pills = "".join(
                        f'<span class="label-pill label-{label_class(name)}">{html.escape(name)}</span>'
                        for name in result.get("labels", [])
                    ) or '<span class="label-pill label-neutral">none</span>'

                    if result.get("assignee_logins"):
                        assignee_display = html.escape(
                            ", ".join(f"@{a}" for a in result["assignee_logins"])
                        )
                    else:
                        assignee_display = "No assignee"

                    if not result.get("pr_checked"):
                        pr_display = "—"
                    elif result.get("linked_pr_list"):
                        first_pr = result["linked_pr_list"][0]
                        extra = len(result["linked_pr_list"]) - 1
                        extra_html = f" +{extra}" if extra > 0 else ""
                        pr_title_safe = html.escape(first_pr["title"][:28])
                        pr_display = (
                            f'<a href="{safe_url(first_pr["url"])}" target="_blank" rel="noopener">'
                            f'{pr_title_safe}</a>{extra_html}'
                        )
                    else:
                        pr_display = "No linked PR"

                    # Flat, zero-indent, single-line HTML - see the note on
                    # render_expandable_banner above. A multi-line, indented
                    # template here (this used to be a triple-quoted f-string
                    # with one <tr> block per line) put a blank line between
                    # every joined row, which Markdown then read as a literal
                    # code block instead of passing through as HTML - exactly
                    # the stray "</tbody></table></div>" text that showed up
                    # on screen.
                    rows_html.append(
                        '<tr>'
                        f'<td class="issue-title-cell">'
                        f'<a href="{safe_url(result["issue_url"])}" target="_blank" rel="noopener">{html.escape(result["issue"])}</a> '
                        f'<span class="badge {badge_class[result["competition_label"]]}" data-tooltip="{badge_tooltip[result["competition_label"]]}">{result["competition_label"]}</span>'
                        f'</td>'
                        f'<td>{label_pills}</td>'
                        f'<td>💬 {result["comments"]}</td>'
                        f'<td>{relative_time(result["created_at"])}</td>'
                        f'<td>{assignee_display}</td>'
                        f'<td>{pr_display}</td>'

                        '</tr>'
                    )

                repo_link_url = repo_entry["repo_url"]
                cards_html.append(
                    f'<details class="repo-card" style="border-left-color: {accent};">'
                    f'<summary class="repo-summary">'
                    f'{avatar_html}'
                    f'<div class="repo-summary-main">'
                    f'<div class="repo-name">{repo_name_safe}</div>'
                    f'<div class="repo-stats">⭐ {repo_entry["stars"]:,} · {language_safe} · '
                    f'{n_issues} matching issue{"" if n_issues == 1 else "s"}</div>'
                    f'</div>'
                    f'<button class="icon-btn repo-bm-btn" '
                    f'data-bm-url="{html.escape(repo_link_url)}" '
                    f'data-bm-title="{repo_name_safe}" '
                    f'title="Bookmark" '
                    f'onclick="event.preventDefault(); event.stopPropagation();">☆</button>'
                    f'<a class="repo-link-btn" href="{safe_url(repo_link_url)}" target="_blank" rel="noopener" '
                    f'onclick="event.stopPropagation()">'
                    f'Repository ↗</a>'
                    f'<span class="repo-chevron">⌄</span>'
                    f'</summary>'
                    f'<div class="repo-issue-table"><table>'
                    f'<colgroup>'
                    f'<col class="col-issue"><col class="col-labels"><col class="col-comments">'
                    f'<col class="col-opened"><col class="col-assignee"><col class="col-pr">'
                    f'</colgroup>'
                    f'<thead><tr><th>Issue</th><th>Labels</th><th>Comments</th><th>Opened</th>'
                    f'<th>Assignee</th><th>Linked PR</th></tr></thead>'
                    f'<tbody>{"".join(rows_html)}</tbody>'
                    f'</table></div>'
                    f'</details>'
                )

            st.markdown("".join(cards_html), unsafe_allow_html=True)

        elif not rate_limit_error:
            st.markdown(
                """
<div class="empty-state">
    <div class="empty-icon">◌</div>
    <div class="empty-title">No issues found</div>
    <div class="empty-text">The scan completed successfully, but none of the issues matched your current filters.</div>
</div>
""",
                unsafe_allow_html=True,
            )

    else:
        st.markdown(
            """
<div class="empty-state">
    <div class="empty-icon">◎</div>
    <div class="empty-title">Your results will show up here</div>
    <div class="empty-text">Set your filters on the left and run a search.</div>
</div>
""",
            unsafe_allow_html=True,
        )


