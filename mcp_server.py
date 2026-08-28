"""
OSS Scout – MCP Server
Exposes GitHub good-first-issue search as a tool for AI assistants.

Usage:
  python mcp_server.py

Claude desktop config (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "oss-scout": {
        "command": "python",
        "args": ["E:\\oss\\mcp_server.py"],
        "env": { "OSS_GITHUB_TOKEN": "ghp_your_token_here" }
      }
    }
  }
"""

import os
import base64
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from github import GitHubClient, GitHubError, RateLimitError, competition_label

# ── PAT loading ──────────────────────────────────────────────────────────────
_PAT_CACHE_FILE = Path(__file__).parent / ".oss_pat"
_PAT_CACHE_KEY  = "oss-scout-local-key"

def _xor_bytes(data: bytes, key: str) -> bytes:
    kb = key.encode()
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(data))

def _load_pat_from_file() -> str:
    try:
        raw = _PAT_CACHE_FILE.read_text(encoding="ascii").strip()
        return _xor_bytes(base64.b64decode(raw), _PAT_CACHE_KEY).decode("utf-8")
    except Exception:
        return ""

def _get_token() -> str:
    return os.environ.get("OSS_GITHUB_TOKEN", "") or _load_pat_from_file()

# ── MCP server ───────────────────────────────────────────────────────────────
mcp = FastMCP("OSS Scout")

@mcp.tool()
def find_oss_issues(
    language: str = "",
    labels: str = "good first issue",
    max_age_days: int = 60,
    max_comments: int = 5,
    require_unassigned: bool = True,
    require_no_pr: bool = True,
    limit: int = 15,
) -> str:
    """
    Search GitHub for beginner-friendly open-source issues.

    Args:
        language:          Programming language to filter by (e.g. "python", "typescript"). Leave empty for any.
        labels:            Comma-separated issue labels to search for (default: "good first issue").
        max_age_days:      Only return issues created within this many days (default: 60).
        max_comments:      Maximum number of comments an issue may have (default: 5).
        require_unassigned: Only return issues with no assignee (default: True).
        require_no_pr:     Exclude issues that already have a linked PR (default: True).
        limit:             Maximum number of issues to return (default: 15).
    """
    token = _get_token()
    client = GitHubClient(token=token)

    label_list = [l.strip() for l in labels.split(",") if l.strip()]
    lang_list   = [language.strip()] if language.strip() else []

    try:
        raw_issues = client.search_candidate_issues(
            labels=label_list,
            languages=lang_list,
            max_comments=max_comments,
            require_unassigned=require_unassigned,
            require_no_pr=require_no_pr,
            issue_age_days=max_age_days,
            max_per_query=limit * 2,
        )
    except RateLimitError as e:
        return f"❌ GitHub rate limit hit. {e}"
    except GitHubError as e:
        return f"❌ GitHub API error: {e}"

    if not raw_issues:
        return "No issues found matching your criteria. Try relaxing the filters."

    issues = raw_issues[:limit]
    lines = [f"Found **{len(issues)}** issue(s):\n"]

    for i, issue in enumerate(issues, 1):
        repo_url  = issue.get("repository_url", "")
        repo_name = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else "unknown/repo"
        title     = issue.get("title", "Untitled")
        html_url  = issue.get("html_url", "")
        comments  = issue.get("comments", 0)
        created   = (issue.get("created_at") or "")[:10]
        assignees = len(issue.get("assignees") or [])
        comp      = competition_label(
            score=None  # skip competition scoring for speed
        ) if False else ""

        label_names = [lb["name"] for lb in (issue.get("labels") or [])]
        label_str   = ", ".join(f"`{n}`" for n in label_names[:4])

        lines.append(
            f"{i}. **[{repo_name}]** {title}\n"
            f"   🔗 {html_url}\n"
            f"   📅 {created}  💬 {comments} comments  {label_str}"
        )

    return "\n\n".join(lines)


@mcp.tool()
def check_github_rate_limit() -> str:
    """Check your remaining GitHub API rate limit and when it resets."""
    token = _get_token()
    client = GitHubClient(token=token)
    try:
        rl = client.rate_limit()
        remaining = rl.get("remaining", "?")
        limit     = rl.get("limit", "?")
        reset_ts  = rl.get("reset")
        reset_str = ""
        if reset_ts:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(reset_ts), tz=timezone.utc)
            reset_str = f" Resets at {dt.strftime('%H:%M UTC')}."
        auth = "authenticated" if token else "unauthenticated (60 req/hr)"
        return f"GitHub API: {remaining}/{limit} requests remaining ({auth}).{reset_str}"
    except GitHubError as e:
        return f"❌ Could not fetch rate limit: {e}"


if __name__ == "__main__":
    mcp.run()
