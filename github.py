"""
GitHub API client and pure-python helpers for OSS Scout.

This module owns every call to the GitHub REST API plus the filtering and
scoring logic used to turn raw API responses into the results the UI shows.
app.py should not talk to the GitHub API directly - it only imports from here.
"""

import time
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


GITHUB_API = "https://api.github.com"
APP_NAME = "OSS-Scout"

# Small delay between requests. GitHub asks integrators to stay well under
# a handful of requests/second on endpoints like search; this keeps a scan
# clear of secondary rate limits without meaningfully slowing it down.
API_DELAY = 1.0
SEARCH_QUERY_DELAY = 2.0  # extra pause between search queries to avoid secondary rate limits


class GitHubError(Exception):
    """Raised for any GitHub API failure that isn't a rate limit."""


class RateLimitError(GitHubError):
    """Raised specifically when GitHub's rate limit has been hit."""


class GitHubClient:

    def __init__(self, token: Optional[str] = None, on_retry=None, on_search_progress=None):
        """
        on_retry: optional callable(attempt, wait_seconds) called before each
                  backoff sleep so the caller can update a progress UI.
        """
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": APP_NAME,
        })
        self._on_retry = on_retry
        self._on_search_progress = on_search_progress

        token = (token or "").strip()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    # --------------------------------------------------------
    # HTTP
    # --------------------------------------------------------

    # Exponential backoff config for secondary rate limits.
    # Waits: 2s, 4s, 8s, 16s — then gives up and shows the error.
    _BACKOFF_BASE    = 2
    _BACKOFF_RETRIES = 6

    def _is_secondary_rate_limit(self, response) -> bool:
        """Return True if GitHub is asking us to slow down (secondary limit)."""
        if response.status_code not in (403, 429):
            return False
        try:
            msg = response.json().get("message", "").lower()
        except Exception:
            msg = response.text.lower()
        return "secondary rate limit" in msg or response.status_code == 429

    def get(self, endpoint, params=None):
        url = endpoint if endpoint.startswith("http") else GITHUB_API + endpoint

        for attempt in range(self._BACKOFF_RETRIES + 1):
            try:
                response = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                raise GitHubError(f"Could not reach GitHub API: {exc}") from exc

            remaining = response.headers.get("X-RateLimit-Remaining")
            reset      = response.headers.get("X-RateLimit-Reset")

            # ── Secondary rate limit → backoff and retry ──────────────────
            if self._is_secondary_rate_limit(response):
                if attempt < self._BACKOFF_RETRIES:
                    wait = self._BACKOFF_BASE ** (attempt + 1)
                    # Honour Retry-After header when present
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, int(retry_after))
                        except ValueError:
                            pass
                    if callable(self._on_retry):
                        self._on_retry(attempt + 1, wait)
                    time.sleep(wait)
                    continue  # retry
                # All retries exhausted — surface to the caller
                try:
                    message = response.json().get("message", "Secondary rate limit")
                except Exception:
                    message = "Secondary rate limit"
                raise RateLimitError(
                    f"GitHub secondary rate limit hit after {self._BACKOFF_RETRIES} retries. "
                    "Please wait a few minutes and try again."
                )

            # ── Hard quota exhausted ──────────────────────────────────────
            if response.status_code == 401:
                raise GitHubError(
                    "GitHub rejected the token (401). Check that the PAT is "
                    "valid, not expired, and was copied correctly."
                )

            if response.status_code == 403:
                if remaining == "0":
                    raise RateLimitError(
                        "GitHub API rate limit reached." + self._format_reset(reset)
                    )
                try:
                    message = response.json().get("message", "Forbidden")
                except Exception:
                    message = response.text
                raise GitHubError(f"GitHub returned 403: {message}")

            if response.status_code == 404:
                return None

            if response.status_code >= 400:
                try:
                    message = response.json().get("message", response.text)
                except Exception:
                    message = response.text
                raise GitHubError(f"GitHub API returned {response.status_code}: {message}")

            try:
                data = response.json()
            except Exception as exc:
                raise GitHubError("GitHub returned an invalid JSON response.") from exc

            time.sleep(API_DELAY)
            return data

    @staticmethod
    def _format_reset(reset):
        if not reset:
            return ""

        try:
            dt = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            return f" It resets at {dt.strftime('%H:%M UTC')}."
        except Exception:
            return ""

    # --------------------------------------------------------
    # Rate limit
    # --------------------------------------------------------

    def rate_limit(self):
        data = self.get("/rate_limit")
        core = (data or {}).get("resources", {}).get("core", {})
        return {
            "remaining": core.get("remaining"),
            "limit": core.get("limit"),
            "reset": core.get("reset"),
        }

    # --------------------------------------------------------
    # Repository search
    # --------------------------------------------------------

    def search_repositories(self, languages, min_stars, max_stars, max_repositories=100):
        """`languages` is a list (possibly empty - meaning "any language").

        GitHub's repo search doesn't support OR-ing multiple `language:`
        qualifiers in one query, so a multi-language filter runs one
        query per language (each capped so the combined total still
        respects `max_repositories`) and merges/dedupes the results,
        instead of trying to cram several qualifiers into a single query.
        """
        languages = [lang for lang in (languages or []) if lang]

        if len(languages) <= 1:
            return self._search_repositories_single(
                languages[0] if languages else None, min_stars, max_stars, max_repositories,
            )

        per_language_cap = max(1, max_repositories // len(languages))
        seen = set()
        combined = []

        for lang in languages:
            batch = self._search_repositories_single(lang, min_stars, max_stars, per_language_cap)
            for repo in batch:
                key = repo.get("full_name")
                if key and key not in seen:
                    seen.add(key)
                    combined.append(repo)

        return combined[:max_repositories]

    def _search_repositories_single(self, language, min_stars, max_stars, max_repositories):
        query_parts = [f"stars:{min_stars}..{max_stars}", "archived:false"]

        if language:
            query_parts.append(f"language:{language}")

        query = " ".join(query_parts)

        repositories = []
        page = 1

        while len(repositories) < max_repositories:
            per_page = min(100, max_repositories - len(repositories))

            data = self.get(
                "/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "asc",
                    "per_page": per_page,
                    "page": page,
                },
            )

            if not data:
                break

            items = data.get("items", [])
            repositories.extend(items)

            if len(items) < per_page:
                break

            page += 1

        return repositories[:max_repositories]

    # --------------------------------------------------------
    # Issue-first search (primary strategy)
    # --------------------------------------------------------

    def search_candidate_issues(self, labels, languages, max_comments,
                                require_unassigned, require_no_pr,
                                issue_age_days=None, max_per_query=100):
        """Search /search/issues directly for all (label × language) pairs.

        This is the primary search strategy: instead of finding repos first
        and then scanning their issues, we query issues that already match
        our criteria and then verify their repos for star/activity filters.

        Labels and languages are OR'd — one query per (label, language) pair,
        deduplicated by issue ID. All other qualifiers (comments, assignee,
        linked PR, age) are pushed server-side so GitHub does the heavy
        filtering, not us.

        Returns a flat, deduplicated list of raw issue dicts. Each issue
        carries `repository_url` (the API URL of its parent repo) which
        the caller uses to do per-repo star/activity verification.
        """
        labels_to_query = [la for la in (labels or []) if la] or [None]
        langs_to_query  = [lg for lg in (languages or []) if lg] or [None]

        seen_ids = set()
        raw_issues = []

        # One query per (label, language) pair.
        # A random 3-6s pause between queries keeps us clear of GitHub's
        # secondary rate limits without hammering the API.
        pairs = [(la, lg) for la in labels_to_query for lg in langs_to_query]
        total_pairs = len(pairs)
        self._search_skipped = []  # languages skipped due to rate limit

        for i, (label, lang) in enumerate(pairs):
            if callable(getattr(self, "_on_search_progress", None)):
                self._on_search_progress(i, total_pairs, lang)

            try:
                batch = self._search_issues_single(
                    label=label,
                    language=lang,
                    max_comments=max_comments,
                    require_unassigned=require_unassigned,
                    require_no_pr=require_no_pr,
                    issue_age_days=issue_age_days,
                    max_per_query=max_per_query,
                )
            except RateLimitError:
                # Rate limit exhausted — record skipped languages and stop.
                # Return whatever we've collected so far as partial results.
                remaining = [lg for (_, lg) in pairs[i:] if lg]
                self._search_skipped = list(dict.fromkeys(remaining))  # dedupe, preserve order
                if callable(getattr(self, "_on_search_progress", None)):
                    self._on_search_progress(total_pairs, total_pairs, None)
                break

            for issue in batch:
                iid = issue.get("id")
                if iid and iid not in seen_ids:
                    seen_ids.add(iid)
                    raw_issues.append(issue)

            if i < total_pairs - 1:
                time.sleep(random.uniform(3, 6))

        return raw_issues

    def _search_issues_single(self, label, language, max_comments,
                              require_unassigned, require_no_pr,
                              issue_age_days=None, max_per_query=100):
        """One /search/issues query. Returns up to max_per_query issue dicts,
        sorted by most recently updated descending."""
        parts = ["is:open", "is:issue"]

        if label:
            parts.append(f'label:"{label}"')

        if language:
            parts.append(f"language:{language}")

        if max_comments is not None:
            parts.append(f"comments:0..{max_comments}")

        if require_unassigned:
            parts.append("no:assignee")

        if require_no_pr:
            # -linked:pr excludes issues that have a PR referencing them
            # with a closing keyword. This is a good server-side proxy that
            # avoids expensive per-issue timeline calls.
            parts.append("-linked:pr")

        if issue_age_days is not None:
            # "opened within N days" = created on or after (today - N days).
            cutoff = (datetime.now(timezone.utc) - timedelta(days=issue_age_days)).date()
            parts.append(f"created:>={cutoff.isoformat()}")

        query = " ".join(parts)

        issues = []
        page = 1

        while len(issues) < max_per_query:
            per_page = min(100, max_per_query - len(issues))
            data = self.get("/search/issues", params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            })

            if not data:
                break

            items = data.get("items", [])
            # /search/issues returns both issues and PRs - exclude PRs.
            issues_only = [item for item in items if "pull_request" not in item]
            issues.extend(issues_only)

            if len(items) < per_page:
                break

            page += 1

        return issues[:max_per_query]

    def get_repo(self, repo_url):
        """Fetch a repository by its GitHub API URL."""
        return self.get(repo_url)

    # --------------------------------------------------------
    # Issues (kept for reference / backwards compat)
    # --------------------------------------------------------

    def get_candidate_issues(self, repo, labels, max_comments):
        """Open issues (not PRs) for `repo` carrying ANY of `labels` and
        with at most `max_comments` comments.

        GitHub's issues endpoint treats a comma-separated `labels` param as
        an AND filter - an issue has to carry every label listed. That's
        the wrong semantics for this app: picking both "good first issue"
        and "help wanted" should widen the search, not narrow it to issues
        that have both. So the label filter is only pushed server-side when
        there's exactly one label (where AND and OR are identical); with
        multiple labels the open issues are fetched and filtered here for
        an ANY match instead.
        """
        params = {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
        }

        if len(labels) == 1:
            params["labels"] = labels[0]

        data = self.get(f"/repos/{repo}/issues", params=params)

        if not data:
            return []

        candidates = []

        for issue in data:
            # The issues endpoint also returns pull requests.
            if "pull_request" in issue:
                continue

            if not labels_match(issue, labels):
                continue

            if issue.get("comments", 0) > max_comments:
                continue

            candidates.append(issue)

        return candidates

    def get_linked_prs(self, repo, issue_number):
        data = self.get(
            f"/repos/{repo}/issues/{issue_number}/timeline",
            params={"per_page": 100},
        )

        if not isinstance(data, list):
            return []

        prs = {}

        for event in data:
            if not isinstance(event, dict):
                continue

            if event.get("event") != "cross-referenced":
                continue

            source = event.get("source") or {}
            source_issue = source.get("issue") or {}

            if not source_issue.get("pull_request"):
                continue

            url = source_issue.get("html_url")
            if url:
                prs[url] = {
                    "title": source_issue.get("title", "Pull Request"),
                    "url": url,
                }

        return list(prs.values())


# ============================================================
# DATE HELPERS
# ============================================================

def parse_date(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def issue_age_matches(issue, minimum_days):
    if minimum_days is None:
        return True

    created = parse_date(issue.get("created_at"))
    if not created:
        return False

    return created >= (datetime.now(timezone.utc) - timedelta(days=minimum_days))


def repository_activity_matches(repo, maximum_days):
    if maximum_days is None:
        return True

    updated = parse_date(repo.get("updated_at"))
    if not updated:
        return False

    return updated >= (datetime.now(timezone.utc) - timedelta(days=maximum_days))


# ============================================================
# LABEL MATCHING
# ============================================================

def labels_match(issue, wanted_labels):
    """True if `issue` carries ANY of `wanted_labels` (or none were asked for)."""
    if not wanted_labels:
        return True

    issue_labels = {
        label.get("name", "").lower() for label in issue.get("labels", [])
    }

    wanted = {label.lower() for label in wanted_labels}

    return bool(issue_labels & wanted)


# ============================================================
# COMPETITION SCORE
# ============================================================

def competition_score(comments, linked_prs, assignees):
    return comments * 3 + linked_prs * 25 + assignees * 25


def competition_label(score):
    if score <= 5:
        return "LOW"
    if score <= 20:
        return "MEDIUM"
    return "HIGH"
