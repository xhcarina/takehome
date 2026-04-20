"""Persistent session store — GitHub-backed JSON file so data survives HF Space restarts."""
import base64
import json
import os
from datetime import datetime, timezone
from typing import Optional

import requests

_STORE_REPO = "xhcarina/takehome"
_STORE_PATH = "data/sessions.json"
_SCOPE_PATH = "data/scopes.json"
_GH_API = "https://api.github.com"

def _gh_headers() -> dict:
    token = os.environ.get("GH_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class _GitHubJsonStore:
    def __init__(self, path: str, default_factory):
        self.path = path
        self._default_factory = default_factory
        self._cache = None
        self._sha = None

    def load(self):
        if self._cache is not None:
            return self._cache
        try:
            resp = requests.get(
                f"{_GH_API}/repos/{_STORE_REPO}/contents/{self.path}",
                headers=_gh_headers(), timeout=15,
            )
            if resp.status_code == 404:
                self._cache = self._default_factory()
                self._sha = None
                return self._cache
            resp.raise_for_status()
            data = resp.json()
            self._sha = data["sha"]
            self._cache = json.loads(base64.b64decode(data["content"]).decode())
            return self._cache
        except Exception as e:
            print(f"[store] load {self.path} failed: {e}")
            return self._default_factory()

    def save(self, data) -> None:
        self._cache = data
        for attempt in range(3):
            try:
                content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
                payload = {"message": f"chore: update {self.path}", "content": content}
                if self._sha:
                    payload["sha"] = self._sha
                resp = requests.put(
                    f"{_GH_API}/repos/{_STORE_REPO}/contents/{self.path}",
                    json=payload, headers=_gh_headers(), timeout=15,
                )
                if resp.status_code == 409:
                    self._cache = None
                    self.load()
                    continue
                resp.raise_for_status()
                self._sha = resp.json()["content"]["sha"]
                return
            except Exception as e:
                print(f"[store] save {self.path} failed (attempt {attempt+1}): {e}")
                break


_sessions_store = _GitHubJsonStore(_STORE_PATH, list)
_scopes_store = _GitHubJsonStore(_SCOPE_PATH, dict)


def save_session(data: dict) -> None:
    sessions = _sessions_store.load()
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    sessions.append(data)
    _sessions_store.save(sessions)


def upsert_session(session_id: str, data: dict) -> None:
    """Insert or update a session record keyed by session_id."""
    sessions = _sessions_store.load()
    for i, s in enumerate(sessions):
        if s.get("session_id") == session_id:
            sessions[i] = {**s, **data}
            _sessions_store.save(sessions)
            return
    data["session_id"] = session_id
    if "timestamp" not in data:
        data["timestamp"] = datetime.now(timezone.utc).isoformat()
    sessions.append(data)
    _sessions_store.save(sessions)


def upsert_scope(repo: str, issue_number: int, data: dict) -> None:
    """Persist scope result keyed by repo+issue so it survives restarts."""
    key = f"{repo}#{issue_number}"
    scopes = _scopes_store.load()
    scopes[key] = {**data, "timestamp": datetime.now(timezone.utc).isoformat()}
    _scopes_store.save(scopes)


def get_scope(repo: str, issue_number: int) -> dict:
    key = f"{repo}#{issue_number}"
    return _scopes_store.load().get(key, {})



def get_pending_sessions() -> list:
    """Return sessions that started but never got a final outcome."""
    return [s for s in _sessions_store.load() if s.get("session_id") and not s.get("outcome")]


def get_latest_session_for_issue(repo: str, issue_number: int) -> dict:
    """Return the most recent session record for a given repo+issue."""
    matches = [s for s in _sessions_store.load() if s.get("repo") == repo and s.get("issue_number") == issue_number]
    if not matches:
        return {}
    return sorted(matches, key=lambda s: s.get("timestamp", ""), reverse=True)[0]


def load_sessions() -> list:
    return _sessions_store.load()


def get_summary(open_bugs_now: Optional[int] = None) -> dict:
    sessions = _sessions_store.load()
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
