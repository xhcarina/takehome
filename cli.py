#!/usr/bin/env python3
"""CLI client for the Devin Remediation Service.

First-time setup:
  python cli.py login

Environment variables (override config file):
  REMEDIATION_SERVICE_URL  Base URL of the hosted service
  WEBHOOK_SECRET           Bearer token for authenticating requests
  TARGET_REPO              Default repo in owner/name format
  GH_TOKEN                 GitHub token
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

_CONFIG_PATH = Path.home() / ".devin-fix" / "config.json"


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


_cfg = _load_config()

SERVICE_URL = (
    os.environ.get("REMEDIATION_SERVICE_URL")
    or _cfg.get("service_url")
    or "https://xhcarina-devintakehome.hf.space"
).rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or _cfg.get("webhook_secret", "")
DEFAULT_REPO = os.environ.get("TARGET_REPO") or _cfg.get("default_repo", "xhcarina/superset")
GH_TOKEN = os.environ.get("GH_TOKEN") or _cfg.get("gh_token", "")


def _auth_headers():
    h = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        h["Authorization"] = f"Bearer {WEBHOOK_SECRET}"
    return h


def _gh_headers():
    if not GH_TOKEN:
        print("Error: GH_TOKEN not set.", file=sys.stderr)
        sys.exit(1)
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_issue(repo: str, number: int) -> dict:
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        headers=_gh_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        print(f"Error: Issue #{number} not found in {repo}.", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def _col(text: str, width: int) -> str:
    text = str(text)
    return text[:width].ljust(width)


def _score_badge(score) -> str:
    if score is None:
        return "—"
    try:
        s = int(score)
    except (ValueError, TypeError):
        return str(score)
    if s >= 8:
        return f"{s}/10 ✓"
    if s >= 5:
        return f"{s}/10 !"
    return f"{s}/10 ✗"


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_issues(args):
    """List open issues, with confidence scores if already scoped."""
    repo = args.repo
    label = args.label

    if not GH_TOKEN:
        print("Error: GH_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    params = {"state": "open", "per_page": 50, "sort": "created", "direction": "desc"}
    if label:
        params["labels"] = label
    resp = requests.get(
        f"https://api.github.com/repos/{repo}/issues",
        params=params,
        headers=_gh_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    issues = [i for i in resp.json() if "pull_request" not in i]

    if not issues:
        print(f"No open issues found in {repo}" + (f" with label '{label}'" if label else "") + ".")
        return

    # Fetch cached scope scores from service
    scores: dict[int, str] = {}
    try:
        for issue in issues:
            r = requests.get(
                f"{SERVICE_URL}/scope/result",
                params={"repo": repo, "issue_number": issue["number"]},
                timeout=5,
            )
            if r.ok:
                data = r.json()
                if data.get("status") == "done":
                    scores[issue["number"]] = data.get("confidence_score")
    except Exception:
        pass  # service unreachable — show issues without scores

    # Print table
    print(f"\nOpen issues in {repo}" + (f"  [label: {label}]" if label else "") + "\n")
    header = f"  {'#':<6} {'Score':<9} {'Title'}"
    print(header)
    print("  " + "─" * (len(header) + 4))
    for issue in issues:
        num = issue["number"]
        score = _score_badge(scores.get(num))
        title = issue["title"][:72]
        print(f"  {str('#'+str(num)):<6} {score:<9} {title}")
    print()


def cmd_scope(args):
    """Scope an issue: Devin analyzes it and assigns a confidence score."""
    repo = args.repo
    issue_number = args.issue_number

    issue = _fetch_issue(repo, issue_number)
    title = issue["title"]
    body = issue.get("body") or ""

    payload = {
        "repo": repo,
        "issue_number": issue_number,
        "issue_title": title,
        "issue_body": body,
    }
    if GH_TOKEN:
        payload["gh_token"] = GH_TOKEN

    resp = requests.post(
        f"{SERVICE_URL}/scope",
        json=payload,
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code == 401:
        print("Error: Invalid WEBHOOK_SECRET.", file=sys.stderr)
        sys.exit(1)
    if not resp.ok:
        print(f"Error {resp.status_code} from server: {resp.text}", file=sys.stderr)
        sys.exit(1)

    session_url = resp.json().get("session_url", "")
    print(f"\nScoping issue #{issue_number}: {title}")
    if session_url:
        print(f"  Session : {session_url}")
    print("Devin is analyzing", end="", flush=True)

    deadline = time.time() + 2400  # 40 min max
    while time.time() < deadline:
        time.sleep(15)
        print(".", end="", flush=True)
        try:
            r = requests.get(
                f"{SERVICE_URL}/scope/result",
                params={"repo": repo, "issue_number": issue_number},
                timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()
            status = data.get("status", "pending")

            if status == "done":
                print("\n")
                score = data.get("confidence_score", "n/a")
                reasoning = data.get("reasoning", "")
                plan = data.get("action_plan", "")
                session_url = data.get("session_url", "")

                badge = _score_badge(score)
                print(f"  Confidence Score : {badge}")
                print(f"  Reasoning        : {reasoning}")
                print(f"\n  Action Plan:\n")
                for line in (plan or "").splitlines():
                    print(f"    {line}")
                print()
                return
            if status == "error":
                print(f"\nError during scope: {data.get('error')}", file=sys.stderr)
                sys.exit(1)
        except Exception:
            continue

    print("\nTimed out waiting for scope result.", file=sys.stderr)
    sys.exit(1)


def cmd_fix(args):
    """Trigger Devin to fix an issue and wait for the PR."""
    repo = args.repo
    issue_number = args.issue_number

    issue = _fetch_issue(repo, issue_number)
    title = issue["title"]
    body = issue.get("body") or ""

    # Use action plan from prior scope if available (optional — fix works without it)
    action_plan = ""
    try:
        r = requests.get(
            f"{SERVICE_URL}/scope/result",
            params={"repo": repo, "issue_number": issue_number},
            timeout=5,
        )
        if r.ok and r.json().get("status") == "done":
            action_plan = r.json().get("action_plan", "")
    except Exception:
        pass

    payload = {
        "repo": repo,
        "issue_number": issue_number,
        "issue_title": title,
        "issue_body": body,
        "action_plan": action_plan,
    }
    if GH_TOKEN:
        payload["gh_token"] = GH_TOKEN

    resp = requests.post(
        f"{SERVICE_URL}/remediate",
        json=payload,
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code == 401:
        print("Error: Invalid WEBHOOK_SECRET.", file=sys.stderr)
        sys.exit(1)
    resp.raise_for_status()

    session_url = resp.json().get("session_url", "")

    print(f"\nFix started for issue #{issue_number}: {title}")
    print(f"  GitHub  : {issue['html_url']}")
    if session_url:
        print(f"  Session : {session_url}")
    print("Devin is working", end="", flush=True)

    deadline = time.time() + 5400  # 90 min max
    while time.time() < deadline:
        time.sleep(15)
        print(".", end="", flush=True)
        try:
            r = requests.get(
                f"{SERVICE_URL}/fix/result",
                params={"repo": repo, "issue_number": issue_number},
                timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()

            if data.get("status") == "done":
                print("\n")
                outcome = data.get("outcome", "")
                prs = data.get("pr_urls", [])

                def _fmt(h):
                    if h is None: return "n/a"
                    hh, mm = int(h), int((h % 1) * 60)
                    return f"{hh}h {mm}m" if hh else f"{mm}m"

                if outcome == "pr_opened" and prs:
                    print(f"  PR opened : {prs[0]}\n")
                else:
                    print(f"  Devin could not complete the fix.\n")

                # Metrics block
                acus = data.get("acus")
                cost = data.get("cost")
                ttr = data.get("ttr_hours")
                human_ttr = data.get("human_ttr_hours")
                open_bugs = data.get("open_bugs_at_time")
                if acus is not None:   print(f"  ACUs consumed              : {acus}")
                if cost is not None:   print(f"  Estimated cost             : ${cost:.2f}")
                if ttr is not None:    print(f"  Time-to-resolution         : {_fmt(ttr)}")
                if human_ttr:          print(f"  Human avg TTR (superset)   : {_fmt(human_ttr)}")
                if human_ttr and ttr:  print(f"  Speedup                    : {human_ttr/ttr:.1f}×")
                if open_bugs is not None: print(f"  Total open issues          : {open_bugs}")

                # Aggregate from /metrics
                try:
                    mr = requests.get(f"{SERVICE_URL}/metrics", timeout=5)
                    if mr.ok:
                        md = mr.json()
                        print(f"  Total fixed by Devin       : {md.get('issues_fixed', 0)}")
                        print(f"  Total cost to date         : ${md.get('total_cost', 0):.2f}")
                except Exception:
                    pass
                print()
                return
        except Exception:
            continue

    print("\nTimed out waiting for fix result.", file=sys.stderr)
    sys.exit(1)



def cmd_metrics(args):
    """Show aggregate metrics across all Devin sessions."""
    resp = requests.get(f"{SERVICE_URL}/metrics", timeout=10)
    resp.raise_for_status()
    d = resp.json()

    def _fmt(hours):
        if hours is None:
            return "n/a"
        h, m = int(hours), int((hours % 1) * 60)
        return f"{h}h {m}m" if h else f"{m}m"

    print(f"\n  {'─'*42}")
    print(f"  Devin Remediation — Aggregate Metrics")
    print(f"  {'─'*42}")
    print(f"  Sessions run     : {d.get('sessions_run', 0)}")
    print(f"  Issues fixed     : {d.get('issues_fixed', 0)}")
    print(f"  Failed           : {d.get('failed', 0)}")
    if d.get("open_bugs_now") is not None:
        print(f"  Total open issues: {d['open_bugs_now']}")
    print(f"  Total cost       : ${d.get('total_cost', 0):.2f}")
    avg = d.get("avg_devin_ttr_hours")
    human_avg = d.get("avg_human_ttr_hours")
    if avg:
        print(f"  Avg Devin TTR    : {_fmt(avg)}")
    if human_avg:
        print(f"  Human avg TTR    : {_fmt(human_avg)}")
    if avg and human_avg:
        print(f"  Overall speedup  : {human_avg/avg:.1f}×")
    print()

    sessions = d.get("sessions", [])
    if sessions:
        print(f"  {'#':<5} {'':2} {'TTR':<8} {'Cost':<10} PR")
        print(f"  {'─'*42}")
        for s in sessions:
            num = f"#{s.get('issue_number', '?')}"
            ok = "✅" if s.get("outcome") == "pr_opened" else "❌"
            ttr_h = s.get("ttr_hours")
            if ttr_h:
                h, m = int(ttr_h), int((ttr_h % 1) * 60)
                ttr_str = f"{h}h {m}m" if h else f"{m}m"
            else:
                ttr_str = "—"
            cost = f"${s.get('cost', 0):.2f}"
            prs = s.get("pr_urls", [])
            pr_str = prs[0].split("/pull/")[-1] if prs else "no PR"
            pr_str = f"PR #{pr_str}" if prs else "no PR"
            print(f"  {num:<5} {ok}  {ttr_str:<8} {cost:<10} {pr_str}")
    print()


def cmd_login(args):
    """Save service credentials to ~/.devin-fix/config.json"""
    print("\n  devin-fix setup\n")
    service_url = input(f"  Service URL [{SERVICE_URL}]: ").strip() or SERVICE_URL
    webhook_secret = input("  Webhook secret: ").strip()
    gh_token = input("  GitHub token (PAT with repo scope): ").strip()
    default_repo = input("  Default repo (owner/name): ").strip()
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps({
        "service_url": service_url,
        "webhook_secret": webhook_secret,
        "gh_token": gh_token,
        "default_repo": default_repo,
    }, indent=2))
    print(f"\n  Config saved to {_CONFIG_PATH}\n")


# ── entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="devin-fix",
        description="Scope and fix GitHub issues using Devin AI",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default: {DEFAULT_REPO})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="Configure service URL, token, and default repo")

    p_issues = sub.add_parser("issues", help="List open issues")
    p_issues.add_argument("--label", default="", help="Filter by label (default: all issues)")

    p_scope = sub.add_parser("scope", help="Scope an issue (confidence score + action plan)")
    p_scope.add_argument("issue_number", type=int)

    p_fix = sub.add_parser("fix", help="Trigger Devin to fix an issue and wait for PR")
    p_fix.add_argument("issue_number", type=int)

    sub.add_parser("metrics", help="Show aggregate metrics across all Devin sessions")

    args = parser.parse_args()
    if not hasattr(args, "repo"):
        args.repo = DEFAULT_REPO

    {"login": cmd_login, "issues": cmd_issues, "scope": cmd_scope, "fix": cmd_fix, "metrics": cmd_metrics}[args.command](args)


if __name__ == "__main__":
    main()
