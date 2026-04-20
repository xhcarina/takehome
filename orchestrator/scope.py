"""Mode 3 — Scope a GitHub issue: analyze, confidence score, action plan. No code changes."""
import json
import os
import re
import sys

from devin_client import create_session, poll_until_done, extract_acus, get_session_messages
from github_client import post_comment
from store import upsert_scope


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confidence_score", "reasoning", "action_plan"],
    "properties": {
        "confidence_score": {"type": "integer", "minimum": 0, "maximum": 10},
        "reasoning": {"type": "string"},
        "action_plan": {"type": "string"},
    },
}


def _build_prompt(issue_title: str, issue_body: str, repo_url: str) -> str:
    return f"""You are a senior security engineer performing issue triage.

Repository: {repo_url}
Issue title: {issue_title}
Issue description:
{issue_body}

Your task (analysis only — do NOT modify any code, do NOT open a PR, do NOT ask clarifying questions):
1. Clone the repository and locate the code relevant to this issue.
2. Assess how feasible it is to fix this issue programmatically.
3. Submit your findings via the structured output tool with is_final=true. Do NOT include the JSON in a chat message — use only the structured output tool.

The structured output must have exactly these fields:
- "confidence_score": integer 0-10 (10 = highly confident this can be fixed cleanly)
- "reasoning": 1-2 sentences explaining the score
- "action_plan": a numbered list of concrete implementation steps as a single string
"""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of Devin's response."""
    match = re.search(r'\{[\s\S]*\}', text or "")
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def run_from_session(repo: str, issue_number: int, session_id: str, session_url: str) -> dict:
    """Poll an already-created session and return the scope result."""
    print(f"[scope] polling session={session_id}")

    final = poll_until_done(session_id, timeout=1800, nudge=False)
    acus = extract_acus(final)
    status = final.get("status", "unknown")

    # Try structured output first
    structured = final.get("structured_output") or {}

    if not structured:
        msgs = final.get("_fetched_messages") or []
        if not msgs:
            try:
                msgs = get_session_messages(session_id)
            except Exception as e:
                print(f"[scope] live fetch failed: {e}")
        for i, msg in enumerate(reversed(msgs)):
            src = msg.get("source")
            text = msg.get("message", "")
            if src != "devin":
                continue
            candidate = _extract_json(text)
            if candidate.get("confidence_score") is not None:
                structured = candidate
                break
        if not structured:
            print(f"[scope] WARNING: could not extract structured output from any message")

    confidence = structured.get("confidence_score", "n/a")
    reasoning = structured.get("reasoning", "")
    action_plan = structured.get("action_plan", "")

    comment = (
        f"🔍 **Scope Complete** — Confidence: **{confidence}/10**\n\n"
        f"**Reasoning:** {reasoning}\n\n"
        f"**Action Plan:**\n{action_plan}\n\n"
        f"_ACUs used: {acus} · Status: {status} · [Session]({session_url})_"
    )
    post_comment(repo, issue_number, comment)
    print(f"[scope] done. confidence={confidence} acus={acus}")

    return {
        "confidence_score": confidence,
        "reasoning": reasoning,
        "action_plan": action_plan,
        "session_id": session_id,
        "session_url": session_url,
        "acus": acus,
        "session_status": status,
    }


def run(repo: str, issue_number: int, issue_title: str, issue_body: str) -> dict:
    """Create session and scope (used when called directly, not via app.py)."""
    repo_url = f"https://github.com/{repo}"
    print(f"[scope] repo={repo} issue=#{issue_number}")
    session = create_session(
        title=f"Scope #{issue_number}: {issue_title[:80]}",
        prompt=_build_prompt(issue_title, issue_body, repo_url),
        tags=["scope", "triage"],
        max_acu=15,
        structured_output_schema=_SCHEMA,
    )
    session_id = session["session_id"]
    session_url = session.get("url", f"https://app.devin.ai/sessions/{session_id}")
    upsert_scope(repo, issue_number, {"status": "pending", "session_url": session_url})
    return run_from_session(repo, issue_number, session_id, session_url)


if __name__ == "__main__":
    result = run(
        repo=os.environ["REPO"],
        issue_number=int(os.environ["ISSUE_NUMBER"]),
        issue_title=os.environ["ISSUE_TITLE"],
        issue_body=os.environ.get("ISSUE_BODY", ""),
    )
    print(result)
    sys.exit(0)
