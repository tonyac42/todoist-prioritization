#!/usr/bin/env python3
"""
Todoist Priority Janitor (REST v2 + Sync v9)

Todoist UI terms used below:
- UI Priority 1 is the most urgent.
- In the REST API, priority is 4 (most urgent) -> 1 (least).

Rules:
1) All overdue tasks => UI Priority 1
2) All completed (checked) tasks => clear priority to default (UI P4) but keep labels
3) If NO tasks are currently UI Priority 1, then after 12:05 (America/New_York):
     - for tasks due today only:
       UI P4 -> UI P3, UI P3 -> UI P2, UI P2 -> UI P1
4) Create a UI Priority 1 task if GitHub Actions scheduling may stop soon
   (scheduled workflows can be disabled after long repo inactivity).
"""

import os
import sys
import json
import uuid
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("LOCAL_TZ", "America/New_York"))

# REST v2 for active tasks
TODOIST_REST_BASE = "https://api.todoist.com/rest/v2"
# Sync v9 for completed items + item_update
TODOIST_SYNC_BASE = "https://api.todoist.com/sync/v9"

# Map Todoist UI priorities to API priorities
# UI P1 -> API 4, UI P2 -> API 3, UI P3 -> API 2, UI P4 -> API 1
UI_TO_API = {1: 4, 2: 3, 3: 2, 4: 1}
API_TO_UI = {v: k for k, v in UI_TO_API.items()}


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def rest_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def sync_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded"}


def get_active_tasks(token: str) -> list[dict]:
    r = requests.get(f"{TODOIST_REST_BASE}/tasks", headers=rest_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def update_task_priority_rest(token: str, task_id: str, api_priority: int) -> None:
    r = requests.post(
        f"{TODOIST_REST_BASE}/tasks/{task_id}",
        headers=rest_headers(token),
        data=json.dumps({"priority": int(api_priority)}),
        timeout=30,
    )
    r.raise_for_status()


def create_task_rest(
    token: str,
    content: str,
    api_priority: int,
    due_date: str | None = None,      # YYYY-MM-DD
    project_id: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,  # REST v2 uses label NAMES in "labels"
) -> dict:
    payload: dict = {"content": content, "priority": int(api_priority)}
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
    params = {"since": since_iso, "limit": str(limit), "annotate_items": "true"}
    r = requests.get(
        f"{TODOIST_SYNC_BASE}/completed/get_all",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def sync_item_update_priority(token: str, item_id: str, api_priority: int) -> None:
    cmd_uuid = str(uuid.uuid4())
    commands = [{
        "type": "item_update",
        "uuid": cmd_uuid,
        "args": {"id": str(item_id), "priority": int(api_priority)},
    }]

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

    # Date-only due
    if due.get("date"):
        try:
            d = date.fromisoformat(due["date"])
            return d < today_local
        except ValueError:
            return False

    # Datetime due
    dt_str = due.get("datetime")
    if dt_str:
        try:
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt_utc.astimezone(TZ) < datetime.now(TZ)
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
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        return None

    r = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"Warn: GitHub API failed: {r.status_code} {r.text[:200]}")
        return None

    pushed_at = r.json().get("pushed_at")
    if not pushed_at:
        return None

    dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00")).astimezone(TZ)
    return max((datetime.now(TZ).date() - dt.date()).days, 0)


def maybe_create_github_expiry_warning(todoist_token: str, active_tasks: list[dict]) -> None:
    warn_days = int(os.getenv("GH_WARN_DAYS", "55"))
    disable_days = int(os.getenv("GH_DISABLE_DAYS", "60"))
    inactivity = github_inactivity_days()
    if inactivity is None:
        return
    if not (warn_days <= inactivity < disable_days):
        return

    marker = os.getenv("GH_TASK_MARKER", "[GH-ACTIONS-KEEPALIVE]")
    if any(marker in (t.get("content") or "") for t in active_tasks):
        return

    repo = os.getenv("GITHUB_REPOSITORY", "")
    actions_url = f"https://github.com/{repo}/actions" if repo else "https://github.com"
    today_str = datetime.now(TZ).date().isoformat()

    content = f"{marker} GitHub Actions may stop soon — sign in and run/refresh it"
    description = (
        f"Repo inactivity is ~{inactivity} days.\n\n"
        f"Link: {actions_url}\n\n"
        "Reminder: you may need to sign in and manually run the workflow or push a small commit."
    )

    create_task_rest(
        todoist_token,
        content=content,
        api_priority=UI_TO_API[1],
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

    # Fetch active tasks
    active = get_active_tasks(todoist_token)

    # Rule 1: overdue -> UI P1
    for t in active:
        if is_overdue(t, today_local):
            cur_api = int(t.get("priority") or UI_TO_API[4])
            if cur_api != UI_TO_API[1]:
                update_task_priority_rest(todoist_token, str(t["id"]), UI_TO_API[1])
                print(f"Overdue -> P1: {t['id']} {(t.get('content') or '')[:60]}")

    # Refresh
    active = get_active_tasks(todoist_token)

    # Rule 2: completed -> clear priority (to default UI P4)
    lookback_hours = int(os.getenv("COMPLETED_LOOKBACK_HOURS", "24"))
    since_iso = (now - timedelta(hours=lookback_hours)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    completed_items = sync_completed_get_all(todoist_token, since_iso=since_iso, limit=200)
    for entry in completed_items:
        item_obj = entry.get("item_object") or {}
        item_id = item_obj.get("id") or entry.get("task_id")
        if not item_id:
            continue
        sync_item_update_priority(todoist_token, str(item_id), UI_TO_API[4])

    print(f"Processed completed items (lookback {lookback_hours}h): {len(completed_items)}")

    # Refresh
    active = get_active_tasks(todoist_token)

    # Rule 3: noon escalation if no UI P1 tasks exist
    has_ui_p1 = any(int(t.get("priority") or UI_TO_API[4]) == UI_TO_API[1] for t in active)
    after_1205 = (now.hour > 12) or (now.hour == 12 and now.minute >= 5)

    if (not has_ui_p1) and after_1205:
        for t in active:
            if not is_due_today(t, today_local):
                continue

            api_pri = int(t.get("priority") or UI_TO_API[4])
            ui_pri = API_TO_UI.get(api_pri, 4)

            if ui_pri in (2, 3, 4):
                new_ui = ui_pri - 1
                new_api = UI_TO_API[new_ui]
                if new_api != api_pri:
                    update_task_priority_rest(todoist_token, str(t["id"]), new_api)
                    print(f"Escalated due-today: {t['id']} UI P{ui_pri} -> UI P{new_ui}")

    # Rule 4: GitHub warning task
    maybe_create_github_expiry_warning(todoist_token, active)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        text = ""
        try:
            text = (e.response.text or "")[:800]
        except Exception:
            pass
        print(f"HTTPError: {e} :: {text}", file=sys.stderr)
        raise
