"""Mode 1 — Remediate a specific GitHub issue via Devin."""
import os
import sys
from devin_client import create_session, poll_until_done, send_message, extract_pr_urls, extract_acus
from github_client import post_comment, list_open_issues
from metrics import get_issue_created_at, get_pr_created_at, compute_ttr, get_historical_avg_ttr, compute_cost, format_duration
from store import save_session, upsert_session, get_summary

MAX_ATTEMPTS = 2


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


def run_from_session(repo: str, issue_number: int, issue_title: str,
                     session_id: str, session_url: str) -> None:
    """Poll an already-created session and post the final comment. Called by app.py background thread."""
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

        pr_urls = extract_pr_urls(final_session)
        if pr_urls:
            break

        if attempts < MAX_ATTEMPTS and final_session.get("status") == "running":
            print(f"[remediate] no PR found, sending follow-up (attempt {attempts})")
            send_message(session_id, "Please make sure to open a pull request with your fix before finishing.")
            attempts += 1
        else:
            break

    # If the PR webhook already posted metrics, skip to avoid duplicate comment
    from store import get_pending_sessions
    already_done = [s for s in get_pending_sessions() if s.get("session_id") == session_id]
    if not already_done:
        print(f"[remediate] metrics already posted via PR webhook, skipping")
        return

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
        print(f"[remediate] metrics collection failed (non-fatal): {e}")

    metrics_lines = [
        f"| ACUs consumed | `{acus}` |",
        f"| Estimated cost | `${cost:.2f}` |",
        f"| Attempts | `{attempts}` |",
        f"| Time-to-resolution | `{format_duration(ttr)}` |",
    ]
    if baseline is not None:
        metrics_lines.append(f"| Human avg TTR (apache/superset) | `{format_duration(baseline)}` |")
        if ttr and baseline > 0:
            metrics_lines.append(f"| Speedup | `{baseline/ttr:.1f}×` |")

    try:
        open_bugs = len(list_open_issues(repo, label=None))
        summary = get_summary()
        metrics_lines += [
            f"| Total open issues | `{open_bugs}` |",
            f"| Total fixed by Devin | `{summary['issues_fixed']}` |",
            f"| Total cost to date | `${summary['total_cost']:.2f}` |",
        ]
    except Exception as e:
        print(f"[remediate] aggregate metrics failed (non-fatal): {e}")
        open_bugs = None

    metrics_table = "\n\n**Metrics**\n\n| | |\n|---|---|\n" + "\n".join(metrics_lines)

    if pr_urls:
        outcome = "pr_opened"
        pr_list = "\n".join(f"- {u}" for u in pr_urls)
        comment = f"✅ **Devin opened a PR.**\n\n{pr_list}{metrics_table}\n\nPlease review and merge when ready."
    else:
        outcome = "could_not_complete"
        comment = (f"⚠️ **Devin could not complete the fix** after {attempts} attempt(s).\n\n"
                   f"Session: {session_url}\nFinal status: `{status}`{metrics_table}\n\nManual review required.")

    post_comment(repo, issue_number, comment)

    try:
        upsert_session(session_id, {
            "repo": repo,
            "issue_number": issue_number,
            "outcome": outcome,
            "ttr_hours": ttr,
            "human_ttr_hours": baseline,
            "cost": cost,
            "acus": acus,
            "attempts": attempts,
            "pr_urls": pr_urls,
            "open_bugs_at_time": open_bugs,
        })
    except Exception as e:
        print(f"[remediate] store save failed (non-fatal): {e}")

    print(f"[remediate] done. outcome={outcome} prs={pr_urls}")


def run():
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
    """Resume polling for a session that was interrupted (e.g. server restart)."""
    from devin_client import get_session, poll_until_done, extract_pr_urls, extract_acus
    from store import upsert_session, get_pending_sessions

    # Find stored context for this session
    pending = [s for s in get_pending_sessions() if s.get("session_id") == session_id]
    if not pending:
        return
    ctx = pending[0]

    repo = ctx.get("repo") or os.environ.get("REPO", "")
    issue_number = int(ctx.get("issue_number") or os.environ.get("ISSUE_NUMBER", 0))
    session_url = ctx.get("session_url", f"https://app.devin.ai/sessions/{session_id}")

    print(f"[resume] session={session_id} repo={repo} issue=#{issue_number}")

    try:
        final_session = poll_until_done(session_id, timeout=4200)
    except TimeoutError:
        try:
            final_session = get_session(session_id)
        except Exception:
            return

    pr_urls = extract_pr_urls(final_session)
    acus = extract_acus(final_session)
    status = final_session.get("status", "unknown")

    ttr, baseline, cost = None, None, compute_cost(acus)
    try:
        issue_dt = get_issue_created_at(repo, issue_number)
        pr_dt = get_pr_created_at(pr_urls[0]) if pr_urls else None
        ttr = compute_ttr(issue_dt, pr_dt)
        baseline = get_historical_avg_ttr("apache/superset")
    except Exception as e:
        print(f"[resume] metrics failed (non-fatal): {e}")

    metrics_lines = [
        f"| ACUs consumed | `{acus}` |",
        f"| Estimated cost | `${cost:.2f}` |",
        f"| Time-to-resolution | `{format_duration(ttr)}` |",
    ]
    if baseline is not None:
        metrics_lines.append(f"| Human avg TTR (apache/superset) | `{format_duration(baseline)}` |")
        if ttr and baseline > 0:
            speedup = baseline / ttr
            metrics_lines.append(f"| Speedup | `{speedup:.1f}×` |")

    try:
        open_bugs = len(list_open_issues(repo, label=None))
        summary = get_summary()
        metrics_lines.append(f"| Total open issues | `{open_bugs}` |")
        metrics_lines.append(f"| Total fixed by Devin | `{summary['issues_fixed']}` |")
        metrics_lines.append(f"| Total cost to date | `${summary['total_cost']:.2f}` |")
    except Exception:
        pass

    metrics_table = "\n\n**Metrics**\n\n| | |\n|---|---|\n" + "\n".join(metrics_lines)

    if pr_urls:
        outcome = "pr_opened"
        pr_list = "\n".join(f"- {u}" for u in pr_urls)
        comment = f"✅ **Devin opened a PR.**\n\n{pr_list}{metrics_table}\n\nPlease review and merge when ready."
    else:
        outcome = "could_not_complete"
        comment = (
            f"⚠️ **Devin could not complete the fix.**\n\n"
            f"Session: {session_url}\nFinal status: `{status}`{metrics_table}\n\nManual review required."
        )

    try:
        post_comment(repo, issue_number, comment)
    except Exception as e:
        print(f"[resume] post_comment failed: {e}")

    try:
        open_bugs = len(list_open_issues(repo, label=None))
        upsert_session(session_id, {
            "outcome": outcome,
            "ttr_hours": ttr,
            "cost": cost,
            "acus": acus,
            "pr_urls": pr_urls,
            "open_bugs_at_time": open_bugs,
        })
    except Exception as e:
        print(f"[resume] store update failed: {e}")

    print(f"[resume] done. outcome={outcome} prs={pr_urls}")


if __name__ == "__main__":
    run()
