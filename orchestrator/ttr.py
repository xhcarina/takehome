"""Time-to-resolution computation, cost calculation, and metrics table formatting."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

GH_API = "https://api.github.com"


def _gh_headers():
    return {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

ACU_PRICE_USD = 0.45


def _parse_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _ttr_hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def get_issue_created_at(repo: str, issue_number: int) -> datetime:
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues/{issue_number}",
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return _parse_dt(resp.json()["created_at"])


def get_pr_created_at(pr_url: str) -> Optional[datetime]:
    match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not match:
        return None
    repo, pr_number = match.group(1), match.group(2)
    resp = requests.get(
        f"{GH_API}/repos/{repo}/pulls/{pr_number}",
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return _parse_dt(resp.json()["created_at"])


def compute_ttr(issue_dt: datetime, pr_dt: Optional[datetime]) -> Optional[float]:
    if pr_dt is None:
        return None
    return _ttr_hours(issue_dt, pr_dt)


def get_historical_avg_ttr(repo: str, sample: int = 50) -> Optional[float]:
    """Average close time (hours) for recently-closed issues without devin-fix label."""
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues",
        params={"state": "closed", "per_page": sample, "sort": "updated", "direction": "desc"},
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    issues = [i for i in resp.json()
              if not any(l["name"] == "devin-fix" for l in i.get("labels", []))]
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


def build_metrics_table(
    acus: float,
    cost: float,
    ttr: Optional[float],
    baseline: Optional[float],
    open_bugs: Optional[int] = None,
    issues_fixed: Optional[int] = None,
    total_cost_to_date: Optional[float] = None,
    attempts: Optional[int] = None,
) -> str:
    lines = [
        f"| ACUs consumed | `{acus}` |",
        f"| Estimated cost | `${cost:.2f}` |",
    ]
    if attempts is not None:
        lines.append(f"| Attempts | `{attempts}` |")
    lines.append(f"| Time-to-resolution | `{format_duration(ttr)}` |")
    if baseline is not None:
        lines.append(f"| Human avg TTR (apache/superset) | `{format_duration(baseline)}` |")
        if ttr and baseline > 0:
            lines.append(f"| Speedup | `{baseline / ttr:.1f}×` |")
    if open_bugs is not None:
        lines.append(f"| Total open issues | `{open_bugs}` |")
    if issues_fixed is not None:
        lines.append(f"| Total fixed by Devin | `{issues_fixed}` |")
    if total_cost_to_date is not None:
        lines.append(f"| Total cost to date | `${total_cost_to_date:.2f}` |")
    return "\n\n**Metrics**\n\n| | |\n|---|---|\n" + "\n".join(lines)
