"""TTR, cost, and baseline metrics."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import requests

ACU_PRICE_USD = 0.45


def _parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _ttr_hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def get_issue_created_at(repo: str, issue_number: int) -> datetime:
    from github_client import get_issue
    issue = get_issue(repo, issue_number)
    return _parse_dt(issue["created_at"])


def get_pr_created_at(pr_url: str) -> Optional[datetime]:
    """Extract created_at from a GitHub PR URL."""
    import os
    match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not match:
        return None
    repo, pr_number = match.group(1), match.group(2)
    headers = {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return _parse_dt(resp.json()["created_at"])


def compute_ttr(issue_dt: datetime, pr_dt: Optional[datetime]) -> Optional[float]:
    if pr_dt is None:
        return None
    return _ttr_hours(issue_dt, pr_dt)


def get_historical_avg_ttr(repo: str, sample: int = 50) -> Optional[float]:
    """Average close time (hours) for recently-closed issues without devin-fix label."""
    from github_client import list_closed_issues
    issues = list_closed_issues(repo, exclude_label="devin-fix", limit=sample)
    durations = []
    for issue in issues:
        if issue.get("created_at") and issue.get("closed_at"):
            durations.append(_ttr_hours(
                _parse_dt(issue["created_at"]),
                _parse_dt(issue["closed_at"]),
            ))
    return sum(durations) / len(durations) if durations else None


def compute_cost(acus: float) -> float:
    return round(acus * ACU_PRICE_USD, 2)


def format_duration(hours: Optional[float]) -> str:
    if hours is None:
        return "n/a"
    if hours < 1:
        return f"{int(hours * 60)}m"
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}h {m}m" if m else f"{h}h"
