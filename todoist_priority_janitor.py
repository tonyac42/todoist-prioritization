#!/usr/bin/env python3
"""
Todoist Priority Janitor (REST v2 + Sync v9)

Rules implemented (Todoist UI terms):
1) All overdue tasks -> Priority 1 (UI P1)
2) All checked/completed tasks -> clear priority (set to default) but keep labels
3) If NO tasks are currently UI Priority 1, then after 12:05 (America/New_York):
     - for tasks due today only:
       UI P4 -> UI P3, UI P3 -> UI P2, UI P2 -> UI P1
4) Create a UI Priority 1 warning task when GitHub scheduled hosting is about to "expire"
   (scheduled workflows can be auto-disabled after ~60 days of repo inactivity in public repos).
"""

import os
import sys
import json
import uuid
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("LOCAL_TZ", "America/New_York"))

# Todoist REST v2 (active tasks)
# Docs: https://api.todoist.com/rest/v2/tasks :contentReference[oaicite:4]{index=4}
TODOIST_REST_BASE = "https://api.todoist.com/rest/v2"

# Todoist Sync v9 (completed tasks + item_update)
TODOIST_SYNC_BASE = "https://api.todoist.com/sync/v9"

# ---- Priority mapping (Todoist UI -> Todoist REST API) ----
# REST API priority: 4=highest (urgent), 1=lowest (normal)
# Todoist UI: P1=highest, P4=default/lowest
UI_TO_API = {1: 4, 2: 3, 3: 2, 4: 1}
API_TO_UI = {v: k for k, v in UI_TO_API.items()}


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def rest_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def sync_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def get_active_tasks(token: str) -> list[dict]:
    # REST v2: Get active tasks :contentReference[oaicite:5]{index=5}
    r = requests.get(f"{TODOIST_REST_BASE}/tasks", headers=rest_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def update_task_priority_rest(token: str, task_id: str, api_priority: int) -> None:
    # REST v2: Update a task (POST /tasks/{id}) :contentReference[oaicite:6]{index=6}
    r = requests.post(
        f"{TODOIST_REST_BASE}/tasks/{task_id}",
        headers=rest_headers(token),
        data=json.dumps({"priority": api_priority}),
        timeout=30,
    )
    r.raise_for_status()
    # v2 docs commonly show 204 No Content for updates; we don't need to parse the body. :contentReference[oaicite:7]{index=7}


def create_task_rest(
    token: str,
    content: str,
    api_priority: int,
    due_date: str | None = None,          # YYYY-MM-DD
    project_id: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,      # REST v2 uses label NAMES in "labels" :contentReference[oaicite:8]{index=8}
) -> dict:
    payload: dict = {"content": content, "priority": api_priority}
    if due_date:
        payload["due_date"] = due_date
    if project_id:
        payload["project_id"] = project_id
    if description:
        payload["description"] = description
    if labels:
        payload["labels"] = labels

    r = requests.post(
        f"{TODOIST_REST_BASE}/tasks",
        headers=rest_headers(token),
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sync_completed_get_all(token: str, since_iso: str, limit: int = 200) -> list[dict]:
    # Sync API v9: Get completed items (annotate_items=true gives item_object)
    params = {
        "since": since_iso,
        "limit": str(limit),
        "annotate_items": "true",
    }
    r = requests.get(
        f"{TODOIST_SYNC_BASE}/completed/get_all",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("items", [])


def sync_item_update_priority(token: str, item_id: str, api_priority: int) -> None:
    # Sync API v9: item_update command (sets priority) :contentReference[oaicite:9]{index=9}
    cmd_uuid = str(uuid.uuid4())
    commands = [
        {
            "type": "item_update",
            "uuid": cmd_uuid,
            "args": {
                "id": str(item_id),
                "priority": int(api_priority),
            },
        }
    ]
    r = requests.post(
        f"{TODOIST_SYNC_BASE}/sync",
        headers=sync_headers(token),
        data={"commands": json.dumps(commands)},
        timeout=30,
    )
    r.raise_for_status()
    out = r.json()
    status = out.get("sync_status", {}).get(cmd_uuid)
    if status != "ok":
        print(f"Warn: item_update not ok for item {item_id}: {status}")


def is_overdue(task: dict, today_local: date) -> bool:
    due = task.get("due")
    if not due:
        return False

    # Full-day due date
    if due.get("date"):
        try:
            d = date.fromisoformat(due["date"])
            return d < today_local
        except ValueError:
            return False

    # Datetime due (RFC3339 in UTC); compare in local tz
    dt_str = due.get("datetime")
    if dt_str:
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(TZ)
            return dt_local < datetime.now(TZ)
        except Exception:
            return False

    return False


def is_due_today(task: dict, today_local: date) -> bool:
    due = task.get("due")
    if not due:
        return False

    if due.get("date"):
        try:
            return date.fromisoformat(due["date"]) == today_local
        except ValueError:
            return False

    dt_str = due.get("datetime")
    if dt_str:
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt_utc.astimezone(TZ).date() == today_local
        except Exception:
            return False

    return False


def github_inactivity_days() -> int | None:
    """
    If running in GitHub Actions, use GitHub API to get repo pushed_at and compute inactivity days.
    """
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        return None

    url = f"https://api.github.com/repos/{repo}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"Warn: GitHub API repo fetch failed: {r.status_code} {r.text[:200]}")
        return None

    data = r.json()
    pushed_at = data.get("pushed_at")
    if not pushed_at:
        return None

    dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00")).astimezone(TZ)
    days = (datetime.now(TZ).date() - dt.date()).days
    return max(days, 0)


def maybe_create_github_expiry_warning(todoist_token: str, active_tasks: list[dict]) -> None:
    warn_days = int(os.getenv("GH_WARN_DAYS", "55"))
    disable_days = int(os.getenv("GH_DISABLE_DAYS", "60"))
    inactivity = github_inactivity_days()
    if inactivity is None:
        return

    if not (warn_days <= inactivity < disable_days):
        return

    marker = os.getenv("GH_TASK_MARKER", "[GH-ACTIONS-KEEPALIVE]")
    already = any(marker in (t.get("content") or "") for t in active_tasks)
    if already:
        return

    repo = os.getenv("GITHUB_REPOSITORY", "")
    actions_url = f"https://github.com/{repo}/actions" if repo else "https://github.com"
    today_str = datetime.now(TZ).date().isoformat()

    content = f"{marker} GitHub Actions may stop soon — sign in and run/refresh it"
    description = (
        f"Repo inactivity is ~{inactivity} days.\n\n"
        f"Link: {actions_url}\n\n"
        "Reminder: you may need to sign in to GitHub and manually run the workflow or push a small commit."
    )

    create_task_rest(
        todoist_token,
        content=content,
        api_priority=UI_TO_API[1],  # UI P1 -> API 4
        due_date=today_str,
        project_id=os.getenv("TODOIST_PROJECT_ID") or None,
        description=description,
    )
    print(f"Created GitHub expiry warning task (inactivity={inactivity} days).")


def main() -> None:
    todoist_token = os.getenv("TODOIST_TOKEN")
    if not todoist_token:
        die("Missing TODOIST_TOKEN secret/env var")

    now = datetime.now(TZ)
    today_local = now.date()

    active = get_active_tasks(todoist_token)

    # ---- Rule 1: Overdue -> UI P1 (API 4) ----
    for t in active:
        if is_overdue(t, today_local):
