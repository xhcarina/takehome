"""Thin GitHub REST wrapper — post comments, create issues, fetch metadata."""
import os
import requests

GH_API = "https://api.github.com"


def _headers():
    token = os.environ["GH_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def post_comment(repo: str, issue_number: int, body: str) -> dict:
    """repo = 'owner/name'"""
    resp = requests.post(
        f"{GH_API}/repos/{repo}/issues/{issue_number}/comments",
        json={"body": body},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def create_issue(repo: str, title: str, body: str, labels: list[str] = None) -> dict:
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    resp = requests.post(
        f"{GH_API}/repos/{repo}/issues",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_label(repo: str, name: str, color: str = "e11d48", description: str = "") -> None:
    """Create label if it doesn't already exist."""
    check = requests.get(
        f"{GH_API}/repos/{repo}/labels/{name}",
        headers=_headers(),
        timeout=10,
    )
    if check.status_code == 404:
        requests.post(
            f"{GH_API}/repos/{repo}/labels",
            json={"name": name, "color": color, "description": description},
            headers=_headers(),
            timeout=10,
        ).raise_for_status()


def get_issue(repo: str, issue_number: int) -> dict:
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues/{issue_number}",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def list_open_issues(repo: str, label: str = None, limit: int = 50) -> list[dict]:
    params = {"state": "open", "per_page": limit, "sort": "created", "direction": "desc"}
    if label:
        params["labels"] = label
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues",
        params=params,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def list_closed_issues(repo: str, exclude_label: str = "devin-fix", limit: int = 50) -> list[dict]:
    """Return recently-closed issues, optionally excluding those with a given label."""
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues",
        params={"state": "closed", "per_page": limit, "sort": "updated", "direction": "desc"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    issues = resp.json()
    if exclude_label:
        issues = [i for i in issues if not any(l["name"] == exclude_label for l in i.get("labels", []))]
    return issues
