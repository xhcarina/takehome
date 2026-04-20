"""Fix engine + GitHub API helpers — used by both Flow A (webhook) and Flow B (CLI)."""
import os
import sys
import requests
from devin_client import create_session, poll_until_done, send_message, extract_pr_urls, extract_acus
from ttr import get_issue_created_at, get_pr_created_at, compute_ttr, get_historical_avg_ttr, compute_cost, format_duration, build_metrics_table
from store import upsert_session, get_summary

# — GitHub helpers —

GH_API = "https://api.github.com"


def _gh_headers():
    return {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def post_comment(repo: str, issue_number: int, body: str) -> dict:
    resp = requests.post(
        f"{GH_API}/repos/{repo}/issues/{issue_number}/comments",
        json={"body": body}, headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_issue(repo: str, issue_number: int) -> dict:
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues/{issue_number}",
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def list_open_issues(repo: str, label: str = None, limit: int = 50) -> list:
    params = {"state": "open", "per_page": limit, "sort": "created", "direction": "desc"}
    if label:
        params["labels"] = label
    resp = requests.get(
        f"{GH_API}/repos/{repo}/issues", params=params,
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()



def ensure_label(repo: str, name: str, color: str = "e11d48", description: str = "") -> None:
    check = requests.get(f"{GH_API}/repos/{repo}/labels/{name}", headers=_gh_headers(), timeout=10)
    if check.status_code == 404:
        requests.post(
            f"{GH_API}/repos/{repo}/labels",
            json={"name": name, "color": color, "description": description},
            headers=_gh_headers(), timeout=10,
        ).raise_for_status()


def create_issue(repo: str, title: str, body: str, labels: list = None) -> dict:
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    resp = requests.post(
        f"{GH_API}/repos/{repo}/issues", json=payload,
        headers=_gh_headers(), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

MAX_ATTEMPTS = 2

# — Fix engine —

def build_prompt(issue_title: str, issue_body: str, repo_url: str, action_plan: str = "") -> str:
    plan_section = (
        f"\n\nPre-scoped action plan (follow this):\n{action_plan}\n"
        if action_plan else ""
    )
    return f"""You are a security engineer. Your task is to fix the following vulnerability in the repository.

Repository: {repo_url}

Issue title: {issue_title}

Issue description:
{issue_body}{plan_section}

Instructions:
1. Clone the repository and identify the affected code.
2. Implement a minimal, targeted fix for the vulnerability described.
3. Write or update relevant tests if they exist.
4. Open a pull request with a clear description of the fix and why it addresses the vulnerability.
5. Do not refactor unrelated code.
"""


def _finalize_session(repo, issue_number, session_id, session_url, final_session, attempts=None):
    """Compute TTR, build comment, post it, and persist outcome. Shared by run_from_session and resume."""
    pr_urls = extract_pr_urls(final_session) if final_session else []
    acus = extract_acus(final_session) if final_session else 0
    status = final_session.get("status", "unknown") if final_session else "timeout"

    ttr, baseline, cost = None, None, compute_cost(acus)
    try:
        issue_dt = get_issue_created_at(repo, issue_number)
        pr_dt = get_pr_created_at(pr_urls[0]) if pr_urls else None
        ttr = compute_ttr(issue_dt, pr_dt)
        baseline = get_historical_avg_ttr("apache/superset")
    except Exception as e:
        print(f"[remediate] ttr failed (non-fatal): {e}")

    open_bugs = issues_fixed = total_cost_to_date = None
    try:
        open_bugs = len(list_open_issues(repo, label=None))
        summary = get_summary()
        issues_fixed = summary["issues_fixed"]
        total_cost_to_date = summary["total_cost"]
    except Exception:
        pass

    metrics_table = build_metrics_table(
        acus=acus, cost=cost, ttr=ttr, baseline=baseline,
        open_bugs=open_bugs, issues_fixed=issues_fixed,
        total_cost_to_date=total_cost_to_date, attempts=attempts,
    )

    if pr_urls:
        outcome = "pr_opened"
        comment = (f"✅ **Devin opened a PR.**\n\n"
                   + "\n".join(f"- {u}" for u in pr_urls)
                   + f"{metrics_table}\n\nPlease review and merge when ready.")
    else:
        outcome = "could_not_complete"
        suffix = f"after {attempts} attempt(s)." if attempts else ""
        comment = (f"⚠️ **Devin could not complete the fix** {suffix}\n\n"
                   f"Session: {session_url}\nFinal status: `{status}`{metrics_table}\n\nManual review required.")

    try:
        post_comment(repo, issue_number, comment)
    except Exception as e:
        print(f"[remediate] post_comment failed (non-fatal): {e}")

    try:
        upsert_session(session_id, {
            "repo": repo, "issue_number": issue_number,
            "outcome": outcome, "ttr_hours": ttr, "human_ttr_hours": baseline,
            "cost": cost, "acus": acus, "attempts": attempts,
            "pr_urls": pr_urls, "open_bugs_at_time": open_bugs,
        })
    except Exception as e:
        print(f"[remediate] store save failed (non-fatal): {e}")

    print(f"[remediate] done. outcome={outcome} prs={pr_urls}")


def run_from_session(repo: str, issue_number: int, issue_title: str,
                     session_id: str, session_url: str) -> None:
    """Flow B entry point — poll an already-created session and post results."""
    attempts = 1
    final_session = None

    while attempts <= MAX_ATTEMPTS:
        print(f"[remediate] polling attempt {attempts}/{MAX_ATTEMPTS}")
        try:
            final_session = poll_until_done(session_id, timeout=4200)
        except TimeoutError:
            print("[remediate] timed out waiting for session")
            try:
                from devin_client import get_session
                final_session = get_session(session_id)
            except Exception:
                pass
            break

        if extract_pr_urls(final_session):
            break

        if attempts < MAX_ATTEMPTS and final_session.get("status") == "running":
            print(f"[remediate] no PR found, sending follow-up (attempt {attempts})")
            send_message(session_id, "Please make sure to open a pull request with your fix before finishing.")
            attempts += 1
        else:
            break

    from store import get_pending_sessions
    if not any(s.get("session_id") == session_id for s in get_pending_sessions()):
        print("[remediate] metrics already posted via PR webhook, skipping")
        return

    _finalize_session(repo, issue_number, session_id, session_url, final_session, attempts=attempts)


def run():
    """Flow A entry point — reads context from env vars set by the webhook handler."""
    repo = os.environ["REPO"]
    issue_number = int(os.environ["ISSUE_NUMBER"])
    issue_title = os.environ["ISSUE_TITLE"]
    issue_body = os.environ.get("ISSUE_BODY", "")
    action_plan = os.environ.get("ACTION_PLAN", "")
    repo_url = f"https://github.com/{repo}"

    print(f"[remediate] repo={repo} issue=#{issue_number} title={issue_title!r}")

    session = create_session(
        title=f"Fix #{issue_number}: {issue_title[:80]}",
        prompt=build_prompt(issue_title, issue_body, repo_url, action_plan),
        tags=["vulnerability", "auto-remediation"],
        max_acu=50,
    )
    session_id = session["session_id"]
    session_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")

    upsert_session(session_id, {"repo": repo, "issue_number": issue_number,
                                "issue_title": issue_title, "session_url": session_url})
    post_comment(repo, issue_number,
                 f"🤖 **Devin is on it.**\n\nSession started: {session_url}\n\n"
                 f"I'll post an update here once a PR is ready or if I need help.")

    run_from_session(repo, issue_number, issue_title, session_id, session_url)
    sys.exit(0)


def resume(session_id: str):
    """Crash recovery — resume polling for a session interrupted by a Space restart."""
    from store import get_pending_sessions
    from devin_client import get_session

    pending = [s for s in get_pending_sessions() if s.get("session_id") == session_id]
    if not pending:
        return
    ctx = pending[0]
    repo = ctx.get("repo", "")
    issue_number = int(ctx.get("issue_number", 0))
    session_url = ctx.get("session_url", f"https://app.devin.ai/sessions/{session_id}")

    print(f"[resume] session={session_id} repo={repo} issue=#{issue_number}")

    try:
        final_session = poll_until_done(session_id, timeout=4200)
    except TimeoutError:
        try:
            final_session = get_session(session_id)
        except Exception:
            return

    _finalize_session(repo, issue_number, session_id, session_url, final_session)


if __name__ == "__main__":
    run()
