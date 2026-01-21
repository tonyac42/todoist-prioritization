#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


# ===== Todoist API base (this is what you got green with) =====
API_BASE = "https://api.todoist.com/api/v1"

# ===== Priority mapping =====
# Todoist UI: P1 (highest) ... P4 (lowest/default)
# Todoist API (what we must send): 4 (highest) ... 1 (lowest/default)
UI_TO_API = {1: 4, 2: 3, 3: 2, 4: 1}
API_TO_UI = {4: 1, 3: 2, 2: 3, 1: 4}


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def get_local_tz() -> dt.tzinfo:
    tz_name = os.getenv("TZ", "America/New_York")
    if ZoneInfo is None:
        # fallback if zoneinfo isn't available
        return dt.timezone(dt.timedelta(hours=-5))
    return ZoneInfo(tz_name)


class TodoistClient:
    def __init__(self, token: str):
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get_all_active_tasks(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        GET /api/v1/tasks (cursor pagination)
        """
        tasks: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            r = requests.get(f"{API_BASE}/tasks", headers=self._headers(), params=params, timeout=30)
            r.raise_for_status()
            payload = r.json()

            tasks.extend(payload.get("results", []))
            cursor = payload.get("next_cursor")
            if not cursor:
                break

        return tasks

    def update_task_priority(self, task_id: str, api_priority: int) -> None:
        """
        POST /api/v1/tasks/{task_id}
        """
        r = requests.post(
            f"{API_BASE}/tasks/{task_id}",
            headers=self._headers(),
            data=json.dumps({"priority": int(api_priority)}),
            timeout=30,
        )
        r.raise_for_status()

    def create_task(self, content: str, api_priority: int = 1, due_string: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v1/tasks
        """
        body: Dict[str, Any] = {"content": content, "priority": int(api_priority)}
        if due_string:
            body["due_string"] = due_string

        r = requests.post(
            f"{API_BASE}/tasks",
            headers=self._headers(),
            data=json.dumps(body),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


def parse_due_to_local(due_obj: Dict[str, Any], tz: dt.tzinfo) -> Tuple[Optional[dt.datetime], Optional[dt.date]]:
    """
    Todoist due object variants we handle:
      - {"date": "YYYY-MM-DD"} (all-day)
      - {"datetime": "YYYY-MM-DDTHH:MM:SSZ"} (timed)
      - sometimes weirdly: {"date": "YYYY-MM-DDTHH:MM:SS"} (datetime stuffed in date field)
    Returns (due_dt_local_or_None, due_date_local_or_None)
    """
    if not due_obj:
        return None, None

    # Prefer explicit datetime
    if due_obj.get("datetime"):
        iso = str(due_obj["datetime"])
        due_dt = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz)
        return due_dt_local, due_dt_local.date()

    raw = due_obj.get("date")
    if not raw:
        return None, None

    raw = str(raw)

    # If "date" actually contains a datetime
    if "T" in raw:
        due_dt = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz) if due_dt.tzinfo else due_dt.replace(tzinfo=tz)
        return due_dt_local, due_dt_local.date()

    # True date-only
    d = dt.date.fromisoformat(raw)
    return None, d


def is_overdue(task: Dict[str, Any], now_local: dt.datetime, today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    due_dt_local, due_date_local = parse_due_to_local(due, tz)

    if due_date_local is None:
        return False

    # If timed, overdue if datetime has passed
    if due_dt_local is not None:
        return due_dt_local < now_local

    # If date-only, overdue if date is before today
    return due_date_local < today_local


def is_due_today(task: Dict[str, Any], today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    _, due_date_local = parse_due_to_local(due, tz)
    return due_date_local == today_local


def after_1205(now_local: dt.datetime) -> bool:
    return (now_local.hour, now_local.minute) >= (12, 5)


def compress_due_today_priorities_api(tasks_due_today: List[Dict[str, Any]]) -> Dict[int, int]:
    """
    Implements your cascading rule exactly by "gap-compressing" the UI priorities among tasks due today,
    but ONLY for UI P2/P3/P4 (since we only run this step when there are no P1 tasks currently).

    Example:
      Due today contains UI {P3,P4}  -> becomes UI {P1,P2}
      Due today contains UI {P4}     -> becomes UI {P1}
      Due today contains UI {P2,P4}  -> becomes UI {P1,P2}  (P4 jumps to P2 because no P3)
    Returns mapping in API priority numbers: old_api_priority -> new_api_priority
    """
    # Consider only tasks currently at API 1/2/3 (UI P4/P3/P2). Ignore API 4 (UI P1).
    present_api_levels = sorted({t.get("priority") for t in tasks_due_today if t.get("priority") in (1, 2, 3)})

    # Convert to UI for clarity, then compress UI values down to 1..N (no gaps)
    present_ui_levels = sorted({API_TO_UI[a] for a in present_api_levels})  # values in {2,3,4}
    ui_map = {old_ui: new_ui for new_ui, old_ui in enumerate(present_ui_levels, start=1)}  # compress to {1..N}

    # Convert UI mapping back to API priorities
    api_map: Dict[int, int] = {}
    for old_ui, new_ui in ui_map.items():
        old_api = UI_TO_API[old_ui]
        new_api = UI_TO_API[new_ui]
        api_map[old_api] = new_api

    return api_map


def github_inactivity_days() -> Optional[int]:
    """
    Uses GitHub API to get repo pushed_at and compute inactivity days.
    Only works in GitHub Actions where GITHUB_REPOSITORY and GITHUB_TOKEN exist.
    """
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return None

    r = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    if r.status_code >= 400:
        return None

    pushed_at = r.json().get("pushed_at")
    if not pushed_at:
        return None

    # pushed_at is ISO8601 UTC
    pushed_dt = dt.datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    days = (dt.datetime.now(dt.timezone.utc).date() - pushed_dt.date()).days
    return max(days, 0)


def maybe_create_github_expiry_task(client: TodoistClient, active_tasks: List[Dict[str, Any]]) -> None:
    """
    Creates a UI P1 task (API priority 4) when repo inactivity is in [warn_days, disable_days),
    with link + sign-in reminder. Creates at most one such task at a time (marker-based).
    """
    warn_days = int(os.getenv("GH_WARN_DAYS", "55"))
    disable_days = int(os.getenv("GH_DISABLE_DAYS", "60"))
    inactivity = github_inactivity_days()
    if inactivity is None:
        return
    if not (warn_days <= inactivity < disable_days):
        return

    marker = os.getenv("GH_TASK_MARKER", "[GH-ACTIONS-KEEPALIVE]").strip()
    already = any(marker in (t.get("content") or "") for t in active_tasks)
    if already:
        return

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    actions_url = f"https://github.com/{repo}/actions" if repo else "https://github.com (open your repo → Actions)"

    content = (
        f"{marker} GitHub Actions may stop soon — sign in and run/refresh\n"
        f"Inactivity: ~{inactivity} days\n"
        f"{actions_url}"
    )

    # Create as UI P1 => API 4
    client.create_task(content=content, api_priority=UI_TO_API[1], due_string="today")


def main() -> int:
    token = os.getenv("TODOIST_TOKEN", "").strip()
    if not token:
        die("Missing TODOIST_TOKEN env var (set it as a GitHub Actions secret).")

    tz = get_local_tz()
    now_local = dt.datetime.now(tz)
    today_local = now_local.date()

    client = TodoistClient(token)

    # Load tasks
    active = client.get_all_active_tasks()

    # GitHub expiry warning (optional but you asked for it)
    maybe_create_github_expiry_task(client, active)

    # Refresh tasks in case we created one
    active = client.get_all_active_tasks()

    # ---- Rule 1: Overdue tasks => UI P1 (API priority 4) ----
    # ---- Rule 2: Checked tasks => clear priority to default (UI P4 = API 1), keep labels ----
    # We'll compute desired priorities and apply only if changes are needed.
    desired: Dict[str, int] = {}

    for t in active:
        task_id = str(t.get("id"))
        cur_api_pri = int(t.get("priority", 1))

        # Rule 2: checked -> default
        if t.get("checked") is True:
            if cur_api_pri != UI_TO_API[4]:
                desired[task_id] = UI_TO_API[4]
            continue

        # Rule 1: overdue -> P1
        if t.get("due") and is_overdue(t, now_local, today_local, tz):
            if cur_api_pri != UI_TO_API[1]:
                desired[task_id] = UI_TO_API[1]

    # Apply overdue/checked changes
    for task_id, new_api_pri in desired.items():
        client.update_task_priority(task_id, new_api_pri)

    # Refresh after updates so Rule 3 sees current reality
    active = client.get_all_active_tasks()

    # ---- Rule 3: Cascading compression (due today only, after 12:05, only if no UI P1 tasks exist) ----
    any_ui_p1_exists = any(int(t.get("priority", 1)) == UI_TO_API[1] for t in active)

    if (not any_ui_p1_exists) and after_1205(now_local):
        due_today = [
            t for t in active
            if (t.get("due") and is_due_today(t, today_local, tz) and not (t.get("checked") is True))
        ]

        mapping = compress_due_today_priorities_api(due_today)
        if mapping:
            for t in due_today:
                old_api = int(t.get("priority", 1))
                if old_api in mapping:
                    new_api = mapping[old_api]
                    if new_api != old_api:
                        client.update_task_priority(str(t["id"]), new_api)

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
