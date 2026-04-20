"""Persistent session store — GitHub-backed JSON file so data survives HF Space restarts."""
import base64
import json
import os
from datetime import datetime, timezone
from typing import Optional

import requests

_STORE_REPO = "xhcarina/takehome"
_STORE_PATH = "data/sessions.json"
_GH_API = "https://api.github.com"

# In-memory cache to avoid hammering GitHub API on every read
_cache: list | None = None
_cache_sha: str | None = None


def _gh_headers() -> dict:
    token = os.environ.get("GH_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _load() -> list:
    global _cache, _cache_sha
    if _cache is not None:
        return _cache
    try:
        resp = requests.get(
            f"{_GH_API}/repos/{_STORE_REPO}/contents/{_STORE_PATH}",
            headers=_gh_headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            _cache = []
            _cache_sha = None
            return _cache
        resp.raise_for_status()
        data = resp.json()
        _cache_sha = data["sha"]
        content = base64.b64decode(data["content"]).decode()
        _cache = json.loads(content)
        return _cache
    except Exception as e:
        print(f"[store] load failed: {e}")
        return []


def _save(sessions: list) -> None:
    global _cache, _cache_sha
    _cache = sessions
    for attempt in range(3):
        try:
            content = base64.b64encode(json.dumps(sessions, indent=2).encode()).decode()
            payload = {"message": "chore: update sessions store", "content": content}
            if _cache_sha:
                payload["sha"] = _cache_sha
            resp = requests.put(
                f"{_GH_API}/repos/{_STORE_REPO}/contents/{_STORE_PATH}",
                json=payload, headers=_gh_headers(), timeout=15,
            )
            if resp.status_code == 409:
                # SHA conflict — re-fetch and retry
                _cache = None
                _load()
                continue
            resp.raise_for_status()
            _cache_sha = resp.json()["content"]["sha"]
            return
        except Exception as e:
            print(f"[store] save failed (attempt {attempt+1}): {e}")
            break


def save_session(data: dict) -> None:
    sessions = _load()
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    sessions.append(data)
    _save(sessions)


def upsert_session(session_id: str, data: dict) -> None:
    """Insert or update a session record keyed by session_id."""
    sessions = _load()
    for i, s in enumerate(sessions):
        if s.get("session_id") == session_id:
            sessions[i] = {**s, **data}
            _save(sessions)
            return
    data["session_id"] = session_id
    if "timestamp" not in data:
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
    sessions.append(data)
    _save(sessions)


def upsert_scope(repo: str, issue_number: int, data: dict) -> None:
    """Persist scope result keyed by repo+issue so it survives restarts."""
    key = f"{repo}#{issue_number}"
    scopes = _load_scopes()
    scopes[key] = {**data, "timestamp": datetime.now(timezone.utc).isoformat()}
    _save_scopes(scopes)


def get_scope(repo: str, issue_number: int) -> dict:
    key = f"{repo}#{issue_number}"
    return _load_scopes().get(key, {})


_SCOPE_PATH = "data/scopes.json"
_scope_cache_local: dict | None = None
_scope_sha: str | None = None


def _load_scopes() -> dict:
    global _scope_cache_local, _scope_sha
    if _scope_cache_local is not None:
        return _scope_cache_local
    try:
        resp = requests.get(
            f"{_GH_API}/repos/{_STORE_REPO}/contents/{_SCOPE_PATH}",
            headers=_gh_headers(), timeout=15,
        )
        if resp.status_code == 404:
            _scope_cache_local = {}
            _scope_sha = None
            return _scope_cache_local
        resp.raise_for_status()
        data = resp.json()
        _scope_sha = data["sha"]
        _scope_cache_local = json.loads(base64.b64decode(data["content"]).decode())
        return _scope_cache_local
    except Exception as e:
        print(f"[store] load_scopes failed: {e}")
        return {}


def _save_scopes(scopes: dict) -> None:
    global _scope_cache_local, _scope_sha
    _scope_cache_local = scopes
    for attempt in range(3):
        try:
            content = base64.b64encode(json.dumps(scopes, indent=2).encode()).decode()
            payload = {"message": "chore: update scopes store", "content": content}
            if _scope_sha:
                payload["sha"] = _scope_sha
            resp = requests.put(
                f"{_GH_API}/repos/{_STORE_REPO}/contents/{_SCOPE_PATH}",
                json=payload, headers=_gh_headers(), timeout=15,
            )
            if resp.status_code == 409:
                _scope_cache_local = None
                _load_scopes()
                continue
            resp.raise_for_status()
            _scope_sha = resp.json()["content"]["sha"]
            return
        except Exception as e:
            print(f"[store] save_scopes failed (attempt {attempt+1}): {e}")
            break


def get_pending_sessions() -> list:
    """Return sessions that started but never got a final outcome."""
    return [s for s in _load() if s.get("session_id") and not s.get("outcome")]


def get_latest_session_for_issue(repo: str, issue_number: int) -> dict:
    """Return the most recent session record for a given repo+issue."""
    matches = [s for s in _load() if s.get("repo") == repo and s.get("issue_number") == issue_number]
    if not matches:
        return {}
    return sorted(matches, key=lambda s: s.get("timestamp", ""), reverse=True)[0]


def load_sessions() -> list:
    return _load()


def get_summary(open_bugs_now: Optional[int] = None) -> dict:
    sessions = _load()
    fixed = [s for s in sessions if s.get("outcome") == "pr_opened"]
    failed = [s for s in sessions if s.get("outcome") and s.get("outcome") != "pr_opened"]

    total_cost = sum(s.get("cost", 0) for s in sessions if s.get("outcome"))

    devin_ttrs = [s["ttr_hours"] for s in fixed if s.get("ttr_hours")]
    avg_devin_ttr = sum(devin_ttrs) / len(devin_ttrs) if devin_ttrs else None

    bug_trend = [
        {"issue": s.get("issue_number"), "open_bugs": s.get("open_bugs_at_time"), "timestamp": s.get("timestamp")}
        for s in sessions if s.get("open_bugs_at_time") is not None
    ]

    completed = [s for s in sessions if s.get("outcome")]
    recent = sorted(completed, key=lambda s: s.get("timestamp", ""), reverse=True)[:10]

    return {
        "sessions_run": len(completed),
        "issues_fixed": len(fixed),
        "failed": len(failed),
        "total_cost": round(total_cost, 2),
        "avg_devin_ttr_hours": round(avg_devin_ttr, 2) if avg_devin_ttr else None,
        "open_bugs_now": open_bugs_now,
        "bug_trend": bug_trend,
        "sessions": recent,
    }
