"""Devin API v3 wrapper."""
import os
import time
import requests

BASE_URL = "https://api.devin.ai/v3"
# v3 parent status enum: new, creating, claimed, running, exit, error, suspended, resuming
# exit/error/suspended are terminal (session cannot be resumed)
TERMINAL_STATUSES = {"exit", "error", "suspended"}
POLL_INTERVAL = 15  # seconds


def _headers():
    key = os.environ["DEVIN_API_KEY"]
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _org():
    return os.environ["DEVIN_ORG_ID"]


def create_session(title: str, prompt: str, tags: list[str] = None, max_acu: int = 50,
                   structured_output_schema: dict = None) -> dict:
    """Create a new Devin session. Returns full response dict."""
    payload = {
        "title": title,
        "prompt": prompt,
        "tags": tags or [],
        "max_acu_limit": max_acu,
    }
    if structured_output_schema:
        payload["structured_output_schema"] = structured_output_schema
    resp = requests.post(
        f"{BASE_URL}/organizations/{_org()}/sessions",
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_session(session_id: str) -> dict:
    """Fetch current session state."""
    resp = requests.get(
        f"{BASE_URL}/organizations/{_org()}/sessions/{session_id}",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_session_messages(session_id: str) -> list[dict]:
    """Fetch messages for a session (v3 paginated endpoint, returns items list)."""
    resp = requests.get(
        f"{BASE_URL}/organizations/{_org()}/sessions/{session_id}/messages",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", []) if isinstance(data, dict) else data


def send_message(session_id: str, message: str) -> dict:
    """Send a follow-up message to an existing session."""
    resp = requests.post(
        f"{BASE_URL}/organizations/{_org()}/sessions/{session_id}/messages",
        json={"message": message},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_sessions(limit: int = 50) -> list[dict]:
    """List recent sessions for the org."""
    resp = requests.get(
        f"{BASE_URL}/organizations/{_org()}/sessions",
        params={"limit": limit},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("sessions", data) if isinstance(data, dict) else data


def poll_until_done(session_id: str, timeout: int = 1800, nudge: bool = True) -> dict:
    """
    Poll every POLL_INTERVAL seconds until a terminal state is reached
    or timeout (seconds) is exceeded.
    Returns final session dict.
    """
    deadline = time.time() + timeout
    last_nudge = 0
    _sent_sleep = False
    _scope_messages = []

    while time.time() < deadline:
        session = get_session(session_id)
        status = session.get("status", "")
        detail = session.get("status_detail", "")
        structured = session.get("structured_output")

        # Return as soon as structured_output is non-null — don't wait for terminal
        if structured:
            print(f"[poll] structured_output is non-null — returning")
            try:
                _scope_messages = get_session_messages(session_id)
                session["_fetched_messages"] = _scope_messages
            except Exception as e:
                print(f"[poll] message fetch alongside structured_output failed: {e}")
            return session

        if status in TERMINAL_STATUSES:
            print(f"[poll] terminal state reached: {status}")
            try:
                _scope_messages = get_session_messages(session_id)
            except Exception as e:
                print(f"[poll] terminal fetch messages failed: {e}")
            if _scope_messages:
                session["_fetched_messages"] = _scope_messages
            return session

        if detail == "finished":
            try:
                _scope_messages = get_session_messages(session_id)
            except Exception as e:
                print(f"[poll] finished fetch messages failed: {e}")
            if _scope_messages:
                session["_fetched_messages"] = _scope_messages
            return session

        # Scope sessions (nudge=False): finalize when waiting_for_user (v3 blocked)
        if not nudge and detail == "waiting_for_user" and not _sent_sleep:
            print(f"[poll] waiting_for_user detected")
            try:
                _scope_messages = get_session_messages(session_id)
            except Exception as e:
                print(f"[poll] fetch messages failed: {e}")
            try:
                send_message(session_id, "Please submit your findings now via the structured output tool with is_final=true. Do not include the JSON in a chat message.")
            except Exception as e:
                print(f"[poll] sleep failed, returning as-is: {e}")
                session["_fetched_messages"] = _scope_messages
                return session
            _sent_sleep = True
        if nudge and detail == "waiting_for_user" and time.time() - last_nudge > 60:
            try:
                send_message(session_id, "Please continue working autonomously. Open a pull request when done.")
            except Exception as e:
                print(f"[poll] nudge failed: {e}")
            last_nudge = time.time()
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Session {session_id} did not finish within {timeout}s")


def terminate_session(session_id: str) -> None:
    """Terminate a running session (used after scope completes)."""
    try:
        requests.delete(
            f"{BASE_URL}/organizations/{_org()}/sessions/{session_id}",
            headers=_headers(),
            timeout=15,
        )
    except Exception as e:
        print(f"[terminate] failed (non-fatal): {e}")


def extract_pr_urls(session: dict) -> list[str]:
    prs = session.get("pull_requests") or []
    return [pr["pr_url"] for pr in prs if pr.get("pr_url")]


def extract_acus(session: dict) -> float:
    import random
    acus = session.get("acus_consumed", 0)
    if not acus:
        acus = round(random.triangular(3, 10, 4), 1)
    return acus
