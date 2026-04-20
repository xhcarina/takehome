"""FastAPI service — exposes /remediate, /scan, /scope, /issues, /health, /webhook."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from typing import Optional

import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Devin Remediation Service")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": type(exc).__name__, "detail": str(exc),
                 "trace": traceback.format_exc()},
    )


@app.on_event("startup")
def _resume_pending_sessions():
    """On restart, resume polling for any sessions that never got a final outcome."""
    try:
        from store import get_pending_sessions
        pending = get_pending_sessions()
        if not pending:
            return
        print(f"[startup] resuming {len(pending)} pending session(s)")
        for s in pending:
            session_id = s["session_id"]
            os.environ["REPO"] = s.get("repo", "")
            os.environ["ISSUE_NUMBER"] = str(s.get("issue_number", ""))
            os.environ["ISSUE_TITLE"] = s.get("issue_title", "")
            os.environ["ISSUE_BODY"] = s.get("issue_body", "")
            os.environ["ACTION_PLAN"] = s.get("action_plan", "")

            def _resume(sid=session_id):
                try:
                    import remediate
                    remediate.resume(sid)
                except Exception as e:
                    print(f"[startup] resume failed for {sid}: {e}")

            threading.Thread(target=_resume, daemon=True).start()
    except Exception as e:
        print(f"[startup] pending session check failed: {e}")

_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _check_auth(request: Request) -> None:
    if not _WEBHOOK_SECRET:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_WEBHOOK_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


class RemediateRequest(BaseModel):
    repo: str
    issue_number: int
    issue_title: str
    issue_body: Optional[str] = ""
    action_plan: Optional[str] = None   # from prior scope step
    gh_token: Optional[str] = None      # overrides GH_TOKEN env var if provided



class ScopeRequest(BaseModel):
    repo: str
    issue_number: int
    issue_title: str
    issue_body: Optional[str] = ""
    gh_token: Optional[str] = None


@app.get("/")
def root():
    return {"service": "devintakehome", "status": "ok"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/issues")
def list_issues(repo: str, label: str = "devin-fix", request: Request = None):
    _check_auth(request)
    if not os.environ.get("GH_TOKEN"):
        raise HTTPException(status_code=500, detail="GH_TOKEN not configured")
    from github_client import list_open_issues
    issues = list_open_issues(repo, label=label if label else None)
    result = []
    from store import get_scope
    for issue in issues:
        scope = get_scope(repo, issue["number"])
        result.append({
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
            "labels": [l["name"] for l in issue.get("labels", [])],
            "created_at": issue["created_at"],
            "scope_status": scope.get("status", "not_started"),
            "confidence_score": scope.get("confidence_score"),
        })
    return result


@app.post("/scope")
def trigger_scope(body: ScopeRequest, request: Request):
    _check_auth(request)
    if body.gh_token:
        os.environ["GH_TOKEN"] = body.gh_token

    from store import upsert_scope
    from devin_client import create_session

    # Create session synchronously so we can return the URL immediately
    repo_url = f"https://github.com/{body.repo}"
    import scope as _scope_mod
    try:
        session = create_session(
            title=f"Scope #{body.issue_number}: {(body.issue_title or '')[:80]}",
            prompt=_scope_mod._build_prompt(body.issue_title or "", body.issue_body or "", repo_url),
            tags=["scope", "triage"],
            max_acu=15,
            structured_output_schema=_scope_mod._SCHEMA,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Devin API error: {e}")

    session_id = session["session_id"]
    session_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")
    upsert_scope(body.repo, body.issue_number, {"status": "pending", "session_url": session_url})

    def _run():
        try:
            result = _scope_mod.run_from_session(
                repo=body.repo,
                issue_number=body.issue_number,
                session_id=session_id,
                session_url=session_url,
            )
            upsert_scope(body.repo, body.issue_number, {"status": "done", **result})
        except Exception as e:
            upsert_scope(body.repo, body.issue_number, {"status": "error", "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "issue_number": body.issue_number, "session_url": session_url}


@app.get("/scope/result")
def scope_result(repo: str, issue_number: int):
    from store import get_scope
    cached = get_scope(repo, issue_number)
    if not cached:
        return {"status": "not_started"}
    return cached


@app.post("/remediate")
def trigger_remediate(body: RemediateRequest, request: Request):
    _check_auth(request)
    if body.gh_token:
        os.environ["GH_TOKEN"] = body.gh_token

    import remediate as _rem
    from devin_client import create_session
    from store import upsert_session
    from github_client import post_comment

    repo_url = f"https://github.com/{body.repo}"
    session = create_session(
        title=f"Fix #{body.issue_number}: {(body.issue_title or '')[:80]}",
        prompt=_rem.build_prompt(body.issue_title or "", body.issue_body or "", repo_url, body.action_plan or ""),
        tags=["vulnerability", "auto-remediation"],
        max_acu=50,
    )
    session_id = session["session_id"]
    session_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")

    upsert_session(session_id, {"repo": body.repo, "issue_number": body.issue_number,
                                "issue_title": body.issue_title, "session_url": session_url})
    post_comment(body.repo, body.issue_number,
                 f"🤖 **Devin is on it.**\n\nSession started: {session_url}\n\n"
                 f"I'll post an update here once a PR is ready or if I need help.")

    def _run(sid=session_id, surl=session_url):
        try:
            _rem.run_from_session(body.repo, body.issue_number, body.issue_title or "", sid, surl)
        except Exception as e:
            print(f"[remediate] run_from_session failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "issue_number": body.issue_number, "session_url": session_url}



@app.post("/webhook")
async def github_webhook(request: Request):
    """Receives GitHub App webhook events (issue labeled)."""
    # Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    if _WEBHOOK_SECRET:
        expected = "sha256=" + hmac.new(
            _WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body)

    if event == "issues" and payload.get("action") == "labeled":
        label = payload["label"]["name"]
        if label == "devin-fix":
            issue = payload["issue"]
            repo = payload["repository"]["full_name"]
            os.environ["REPO"] = repo
            os.environ["ISSUE_NUMBER"] = str(issue["number"])
            os.environ["ISSUE_TITLE"] = issue["title"]
            os.environ["ISSUE_BODY"] = issue.get("body") or ""

            def _run():
                import remediate
                remediate.run()

            threading.Thread(target=_run, daemon=True).start()
            return {"status": "started", "issue": issue["number"]}

    if event == "pull_request" and payload.get("action") == "opened":
        pr = payload["pull_request"]
        # Only handle PRs opened by Devin
        login = (pr.get("user") or {}).get("login", "")
        if "devin" in login.lower():
            repo = payload["repository"]["full_name"]
            pr_url = pr["html_url"]
            pr_body = pr.get("body") or ""

            def _post_metrics(repo=repo, pr_url=pr_url, pr_body=pr_body):
                try:
                    import re
                    import remediate
                    from store import get_pending_sessions, upsert_session
                    from devin_client import get_session, extract_pr_urls, extract_acus
                    from github_client import post_comment, list_open_issues
                    from metrics import (get_issue_created_at, get_pr_created_at,
                                         compute_ttr, get_historical_avg_ttr,
                                         compute_cost, format_duration)
                    from store import get_summary

                    # Wait for ACUs to finalize before fetching session data
                    import time as _time
                    _time.sleep(60)

                    # Find session_id from PR body
                    match = re.search(r"devin\.ai/sessions/([\w-]+)", pr_body)
                    session_id = match.group(1) if match else None

                    # Find linked issue from store — by session_id or by issue ref in PR body
                    pending = []
                    if session_id:
                        pending = [s for s in get_pending_sessions()
                                   if s.get("session_id") == session_id]
                    if not pending:
                        # Fall back: match "Fixes #N" / "Closes #N" in PR body
                        issue_match = re.search(r"(?:fixes|closes|resolves)\s+#(\d+)", pr_body, re.IGNORECASE)
                        if issue_match:
                            ref_number = int(issue_match.group(1))
                            pending = [s for s in get_pending_sessions()
                                       if s.get("repo") == repo and s.get("issue_number") == ref_number]
                    if not pending:
                        print("[webhook/pr] could not match PR to a pending session")
                        return
                    ctx = pending[0]
                    session_id = session_id or ctx.get("session_id")
                    issue_number = ctx["issue_number"]

                    # Fetch session for ACUs
                    final_session = get_session(session_id)
                    acus = extract_acus(final_session)
                    cost = compute_cost(acus)

                    # TTR
                    ttr, baseline = None, None
                    try:
                        issue_dt = get_issue_created_at(repo, issue_number)
                        pr_dt = get_pr_created_at(pr_url)
                        ttr = compute_ttr(issue_dt, pr_dt)
                        baseline = get_historical_avg_ttr("apache/superset")
                    except Exception as e:
                        print(f"[webhook/pr] metrics error (non-fatal): {e}")

                    metrics_lines = [
                        f"| ACUs consumed | `{acus}` |",
                        f"| Estimated cost | `${cost:.2f}` |",
                        f"| Time-to-resolution | `{format_duration(ttr)}` |",
                    ]
                    if baseline is not None:
                        metrics_lines.append(
                            f"| Human avg TTR (apache/superset) | `{format_duration(baseline)}` |")
                        if ttr and baseline > 0:
                            metrics_lines.append(
                                f"| Speedup | `{baseline/ttr:.1f}×` |")
                    try:
                        open_bugs = len(list_open_issues(repo, label=None))
                        summary = get_summary()
                        metrics_lines += [
                            f"| Total open issues | `{open_bugs}` |",
                            f"| Total fixed by Devin | `{summary['issues_fixed']}` |",
                            f"| Total cost to date | `${summary['total_cost']:.2f}` |",
                        ]
                    except Exception:
                        pass

                    metrics_table = (
                        "\n\n**Metrics**\n\n| | |\n|---|---|\n"
                        + "\n".join(metrics_lines)
                    )
                    comment = (
                        f"✅ **Devin opened a PR.**\n\n- {pr_url}"
                        f"{metrics_table}\n\nPlease review and merge when ready."
                    )
                    post_comment(repo, issue_number, comment)

                    upsert_session(session_id, {
                        "outcome": "pr_opened",
                        "ttr_hours": ttr,
                        "cost": cost,
                        "acus": acus,
                        "pr_urls": [pr_url],
                        "open_bugs_at_time": open_bugs if 'open_bugs' in dir() else None,
                    })
                    print(f"[webhook/pr] metrics posted for issue #{issue_number}")
                except Exception as e:
                    print(f"[webhook/pr] failed: {e}")

            threading.Thread(target=_post_metrics, daemon=True).start()
            return {"status": "metrics_started"}

    return {"status": "ignored"}


@app.post("/resume")
def resume_pending(request: Request):
    """Manually trigger resume for any pending sessions — call if metrics comment is missing."""
    _check_auth(request)
    try:
        from store import get_pending_sessions
        pending = get_pending_sessions()
        if not pending:
            return {"status": "no_pending_sessions"}
        for s in pending:
            sid = s["session_id"]
            def _run(session_id=sid):
                try:
                    import remediate
                    remediate.resume(session_id)
                except Exception as e:
                    print(f"[resume] failed for {session_id}: {e}")
            threading.Thread(target=_run, daemon=True).start()
        return {"status": "resumed", "sessions": [s["session_id"] for s in pending]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/fix/result")
def fix_result(repo: str, issue_number: int):
    from store import get_latest_session_for_issue
    session = get_latest_session_for_issue(repo, issue_number)
    if not session:
        return {"status": "not_started"}
    if session.get("outcome"):
        return {"status": "done", **session}
    return {"status": "pending", "session_url": session.get("session_url", ""), "session_id": session.get("session_id", "")}


@app.post("/session/{session_id}/terminate")
def terminate_session_endpoint(session_id: str, request: Request):
    """Terminate a stuck session so its poll thread can extract results and complete."""
    _check_auth(request)
    from devin_client import terminate_session
    terminate_session(session_id)
    return {"status": "terminated", "session_id": session_id}


@app.get("/metrics")
def get_metrics():
    from store import get_summary
    from github_client import list_open_issues
    from metrics import get_historical_avg_ttr
    try:
        repo = os.environ.get("TARGET_REPO", "")
        open_bugs = len(list_open_issues(repo, label=None)) if repo else None
    except Exception:
        open_bugs = None
    try:
        avg_human_ttr = get_historical_avg_ttr("apache/superset")
    except Exception:
        avg_human_ttr = None
    summary = get_summary(open_bugs_now=open_bugs)
    summary["avg_human_ttr_hours"] = avg_human_ttr
    return summary
