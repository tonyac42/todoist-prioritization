#!/usr/bin/env python3
"""
Todoist Priority Janitor

Rules implemented (matching your phrasing in Todoist UI terms):
1) All overdue tasks -> Priority 1
2) All checked/completed tasks -> clear priority (set to default) but keep labels
3) If NO tasks are currently Priority 1, then after 12:05 (America/New_York):
     - for tasks due today only:
       P4 -> P3, P3 -> P2, P2 -> P1
4) Create a Priority 1 task when GitHub scheduled hosting is about to "expire"
   (scheduled workflows can be auto-disabled after 60 days of inactivity in public repos).
"""

import os
import sys
import json
import uuid
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("LOCAL_TZ", "America/New_York"))

# Todoist REST v1 (active tasks)
TODOIST_REST_BASE = "https://api.todoist.com/rest/v1"
# Todoist Sync v9 (completed tasks + item_update)
TODOIST_SYNC_BASE = "https://api.todoist.com/sync/v9"

# ---- Priority mapping (Todoist UI -> API) ----
# UI P1 (red) == API 4 (urgent)
# UI P2 == API 3
# UI P3 == API 2
# UI P4 (default) == API 1
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
    # REST: Get active tasks
    r = requests.get(f"{TODOIST_REST_BASE}/tasks", headers=rest_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def update_task_priority_rest(token: str, task_id: int, api_priority: int) -> None:
    # REST: Update task
    r = requests.post(
        f"{TODOIST_REST_BASE}/tasks/{task_id}",
        headers=rest_headers(token),
        data=json.dumps({"priority": api_priority}),
        timeout=30,
    )
    r.raise_for_status()


def create_task_rest(token: str, content: str, api_priority: int, due_date: str | None = None,
                     project_id: str | None = None, description: str | None = None,
                     labels: list[int] | None = None) -> dict:
    payload: dict = {"content": content, "priority": api_priority}
    if due_date:
        payload["due_date"] = due_date  # YYYY-MM-DD
    if project_id:
        payload["project_id"] = project_id
    if description:
        payload["description"] = description
    if labels:
        payload["label_ids"] = labels

    r = requests.post(
        f"{TODOIST_REST_BASE}/tasks",
        headers=rest_headers(token),
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sync_completed_get_all(token: str, since_iso: str, limit: int = 200) -> list[dict]:
    # Sync: Get completed items
    # docs: /completed/get_all supports since/until and annotate_items=true for full item object :contentReference[oaicite:3]{index=3}
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
    # Sync: item_update command :contentReference[oaicite:4]{index=4}
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
        # Not fatal; completed items sometimes can be tricky depending on account/state.
        print(f"Warn: item_update not ok for item {item_id}: {status}")


def is_overdue(task: dict, today_local: date) -> bool:
    due = task.get("due")
    if not due:
        return False

    # Full-day due date
    if "date" in due and due["date"]:
        try:
            d = date.fromisoformat(due["date"])
            return d < today_local
        except ValueError:
            return False

    # Datetime due (RFC3339 in UTC); compare in local tz
    dt_str = due.get("datetime")
    if dt_str:
        try:
            # Example: 2016-09-01T09:00:00Z
            dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(TZ)
            return dt_local.date() < today_local or (dt_local.date() == today_local and dt_local < datetime.now(TZ))
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
    Scheduled workflows in public repos may be disabled after 60 days inactivity :contentReference[oaicite:5]{index=5}
    """
    repo = os.getenv("GITHUB_REPOSITORY")
    token = os.getenv("GITHUB_TOKEN")
    if not repo or not token:
        return None

    url = f"https://api.github.com/repos/{repo}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=30)
    if r.status_code >= 400:
        print(f"Warn: GitHub API repo fetch failed: {r.status_code} {r.text[:200]}")
        return None

    data = r.json()
    pushed_at = data.get("pushed_at")
    if not pushed_at:
        return None

    # pushed_at is ISO8601 UTC
    dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00")).astimezone(TZ)
    days = (datetime.now(TZ).date() - dt.date()).days
    return max(days, 0)


def maybe_create_github_expiry_warning(todoist_token: str, active_tasks: list[dict]) -> None:
    warn_days = int(os.getenv("GH_WARN_DAYS", "55"))
    disable_days = int(os.getenv("GH_DISABLE_DAYS", "60"))
    inactivity = github_inactivity_days()
    if inactivity is None:
        return

    # Only warn in the window [warn_days, disable_days)
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

    # Create as Todoist UI P1 => API priority 4
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
    today_str = today_local.isoformat()

    active = get_active_tasks(todoist_token)

    # ---- Rule 1: Overdue -> UI P1 (API 4) ----
    for t in active:
        if is_overdue(t, today_local):
            if t.get("priority") != UI_TO_API[1]:
                update_task_priority_rest(todoist_token, t["id"], UI_TO_API[1])
                print(f"Overdue -> P1: {t['id']} {t.get('content','')[:60]}")

    # Refresh after changes (cheap + keeps logic simple)
    active = get_active_tasks(todoist_token)

    # ---- Rule 2: Checked/completed tasks -> clear priority (API 1) ----
    # We can only fetch completed tasks via Sync API :contentReference[oaicite:6]{index=6}
    lookback_hours = int(os.getenv("COMPLETED_LOOKBACK_HOURS", "24"))
    since_iso = (now - timedelta(hours=lookback_hours)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    completed_items = sync_completed_get_all(todoist_token, since_iso=since_iso, limit=200)
    for entry in completed_items:
        item_obj = entry.get("item_object") or {}
        item_id = item_obj.get("id") or entry.get("task_id")
        if not item_id:
            continue
        # Clear priority => default => API 1 (UI P4)
        # Try it; if API says no, we just warn.
        sync_item_update_priority(todoist_token, str(item_id), UI_TO_API[4])

    if completed_items:
        print(f"Processed completed items (lookback {lookback_hours}h): {len(completed_items)}")
    else:
        print(f"No completed items found in last {lookback_hours}h.")

    # Refresh active again
    active = get_active_tasks(todoist_token)

    # ---- Rule 3: Noon escalation (only if there are NO UI P1 tasks) ----
    # "currently priority 1" in your wording == UI P1 == API 4
    has_ui_p1 = any(t.get("priority") == UI_TO_API[1] for t in active)

    after_1205 = (now.hour > 12) or (now.hour == 12 and now.minute >= 5)
    if (not has_ui_p1) and after_1205:
        for t in active:
            if not is_due_today(t, today_local):
                continue

            api_pri = int(t.get("priority") or UI_TO_API[4])
            ui_pri = API_TO_UI.get(api_pri, 4)

            # Only bump P2/P3/P4 (UI) -> up one notch
            if ui_pri in (2, 3, 4):
                new_ui = ui_pri - 1  # 4->3, 3->2, 2->1
                new_api = UI_TO_API[new_ui]
                if new_api != api_pri:
                    update_task_priority_rest(todoist_token, t["id"], new_api)
                    print(f"Escalated due-today: {t['id']} UI P{ui_pri} -> UI P{new_ui}")

    # ---- Rule 4: GitHub “about to expire” warning ----
    maybe_create_github_expiry_warning(todoist_token, active)

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTPError: {e} :: {getattr(e.response,'text','')[:500]}", file=sys.stderr)
        raise
