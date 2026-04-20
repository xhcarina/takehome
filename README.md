---
title: Devin Remediation Service
emoji: 🔒
colorFrom: red
colorTo: red
sdk: docker
pinned: true
---

## Problem

Engineering teams accumulate bug backlogs faster than they can fix them. Triaging an issue, scoping the fix, implementing it, and opening a PR takes hours of developer time — even for straightforward bugs. This project automates that loop: a labeled GitHub issue becomes a merged PR with zero manual coding, and every run is tracked with cost and time-to-resolution metrics so you can measure the improvement.

---

## Design Decisions

```
Flow A — Automatic (label-based)
────────────────────────────────
User adds label "devin-fix" to any issue
        │
        ▼
GitHub App webhook → POST /remediate → HF Space
        │
        ▼
Devin fixes the issue and opens a PR
Metrics posted as GitHub comment (TTR, cost, speedup)


Flow B — Manual CLI (two-step)
───────────────────────────────
python cli.py scope <N>       # Devin analyzes issue → confidence score + action plan
python cli.py fix <N>         # Devin implements the fix using the scoped action plan

```

**Hosted on HuggingFace** — evaluators need only a CLI + 3 env vars. No local infra, no Devin API access required.

**`gh_token` passthrough** — teams pass their own GitHub token per request, so multiple teams share one hosted service without sharing credentials.

**Nudge on completion** — if Devin finishes a fix without opening a PR, it gets one follow-up message prompting it to open one before the session closes.

**GitHub-backed persistence** — sessions and scope results are stored as JSON files committed to this repo (`data/sessions.json`, `data/scopes.json`). Data survives HuggingFace Space restarts and terminal closes. Pending fix sessions are automatically resumed on startup so no metrics are lost.

## Quick Start (evaluators)

The service is already running at `https://xhcarina-devintakehome.hf.space`.

### Flow A — GitHub App (label-based, fully automatic)

1. Install the GitHub App on your repo: [Install Devin Remediation App](https://github.com/apps/devintakehome/installations/new)
2. Add the label `devin-fix` to any issue
3. The webhook fires automatically → Devin fixes it → PR opened → metrics posted as a comment

No CLI or credentials needed.

### Flow B — Manual CLI (two-step)

```bash
# 1. Get the CLI
git clone https://github.com/xhcarina/takehome.git
cd takehome
pip install requests

# 2. Login (saves config locally — only needed once)
python cli.py login
#   Service URL:   https://xhcarina-devintakehome.hf.space
#   Webhook secret: devin-secret-2026
#   GitHub token:  <your github token>
#   Target repo:   xhcarina/superset

# 3. Use it
python cli.py issues           # list open issues + any existing confidence scores
python cli.py scope 1          # analyze issue #1 — takes ~10-30 min
python cli.py fix 1            # fix issue #1 (uses scoped action plan automatically)
python cli.py metrics          # view aggregate metrics
```

---

## CLI Reference for Flow B

| Command | Description |
|---------|-------------|
| `python cli.py issues` | List open issues with confidence scores |
| `python cli.py scope <N>` | Analyze issue — prints confidence score + action plan, persists result |
| `python cli.py fix <N>` | Trigger Devin to fix (automatically uses prior scope if available) |
| `python cli.py metrics` | Show aggregate metrics across all sessions |

---

## Observability

After each fix, a metrics comment is posted to the GitHub issue:

```
✅ Devin opened a PR.
https://github.com/owner/repo/pull/42

| ACUs consumed            | 38      |
| Estimated cost           | $85.50  |
| Time-to-resolution       | 52m     |
| Human avg TTR            | 6h 14m  |
| Speedup                  | 7.2×    |
| Open bugs (now)          | 8       |
| Total fixed by Devin     | 3       |
| Total cost to date       | $210.00 |
```

**TTR methodology:** issue `created_at` → PR `created_at`, for both Devin and historical human-resolved issues. Human baseline is pulled from `apache/superset` closed issues (thousands of data points).

---

## Demo Issues

Seven issues created in [`xhcarina/superset`](https://github.com/xhcarina/superset/issues), each sourced from a real open bug in `apache/superset`:

| # | Title | Upstream |
|---|-------|----------|
| [#1](https://github.com/xhcarina/superset/issues/1) | Infinite chart reload in nested tabs | [apache/superset #39439](https://github.com/apache/superset/issues/39439) |
| [#2](https://github.com/xhcarina/superset/issues/2) | Delete chart doesn't refresh home page | [apache/superset #39428](https://github.com/apache/superset/issues/39428) |
| [#3](https://github.com/xhcarina/superset/issues/3) | Legend margin broken on radar charts | [apache/superset #39424](https://github.com/apache/superset/issues/39424) |
| [#4](https://github.com/xhcarina/superset/issues/4) | Logo double-prefixed in subdirectory deploy | [apache/superset #39432](https://github.com/apache/superset/issues/39432) |
| [#5](https://github.com/xhcarina/superset/issues/5) | Delete doesn't refresh Recent Activity | [apache/superset #39435](https://github.com/apache/superset/issues/39435) |
| [#6](https://github.com/xhcarina/superset/issues/6) | Permission box too narrow (CSS) | [apache/superset #39339](https://github.com/apache/superset/issues/39339) |
| [#7](https://github.com/xhcarina/superset/issues/7) | Embedded dashboard filters don't load | [apache/superset #39419](https://github.com/apache/superset/issues/39419) |

## Repo Structure

```
cli.py                        # CLI client (Flow B)
orchestrator/
  app.py                      # FastAPI: /remediate /scope /scope/result /issues /metrics /health /webhook
  scope.py                    # Devin analysis → confidence score + action plan (structured output)
  remediate.py                # Devin fix → PR nudge → metrics comment
  devin_client.py             # Devin API v3 wrapper
  github_client.py            # GitHub REST API wrapper
  metrics.py                  # TTR, cost, speedup computation
  store.py                    # GitHub-backed JSON store (data/sessions.json, data/scopes.json)
data/
  sessions.json               # Fix session history and metrics
  scopes.json                 # Scope results (confidence scores, action plans)
Dockerfile                    # Port 7860 for HuggingFace Spaces
requirements.txt
```
