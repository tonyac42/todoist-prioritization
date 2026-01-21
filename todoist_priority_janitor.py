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


API_BASE = "https://api.todoist.com/api/v1"

# UI -> API mapping (IMPORTANT)
# UI P1 (highest) -> API 4, UI P4 (default) -> API 1
UI_TO_API = {1: 4, 2: 3, 3: 2, 4: 1}
API_TO_UI = {4: 1, 3: 2, 2: 3, 1: 4}


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def get_local_tz() -> dt.tzinfo:
    tz_name = os.getenv("TZ", "America/New_York")
    if ZoneInfo is None:
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
        r = requests.post(
            f"{API_BASE}/tasks/{task_id}",
            headers=self._headers(),
            data=json.dumps({"priority": int(api_priority)}),
            timeout=30,
        )
        r.raise_for_status()

    def create_task(self, content: str, api_priority: int = 1, due_string: Optional[str] = None) -> Dict[str, Any]:
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
    Handles:
      - due["datetime"] = RFC3339
      - due["date"] = YYYY-MM-DD (all-day)
      - due["date"] sometimes contains a datetime (YYYY-MM-DDTHH:MM:SS)
    """
    if not due_obj:
        return None, None

    if due_obj.get("datetime"):
        iso = str(due_obj["datetime"])
        due_dt = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz)
        return due_dt_local, due_dt_local.date()

    raw = due_obj.get("date")
    if not raw:
        return None, None

    raw = str(raw)

    if "T" in raw:
        due_dt = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz) if due_dt.tzinfo else due_dt.replace(tzinfo=tz)
        return due_dt_local, due_dt_local.date()

    d = dt.date.fromisoformat(raw)
    return None, d


def is_due_today(task: Dict[str, Any], today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    _, due_date_local = parse_due_to_local(due, tz)
    return due_date_local == today_local


def is_overdue(task: Dict[str, Any], now_local: dt.datetime, today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    due_dt_local, due_date_local = parse_due_to_local(due, tz)

    if due_date_local is None:
        return False

    # timed: overdue if time passed
    if due_dt_local is not None:
        return due_dt_local < now_local

    # all-day: overdue if before today
    return due_date_local < today_local


def after_1205(now_local: dt.datetime) -> bool:
    return (now_local.hour, now_local.minute) >= (12, 5)


def compress_due_today_priorities_api(due_today: List[Dict[str, Any]]) -> Dict[int, int]:
    """
    Gap-compress among due-today tasks for the set of priorities present (excluding P1).
    Works in API priorities:
      - API 4 = UI P1 (but this step only runs when NO API 4 exists)
      - API 3 = UI P2
      - API 2 = UI P3
      - API 1 = UI P4

    We compress UI levels among {P2,P3,P4} to {P1,P2,P3} as needed.
    """
    # Only consider tasks currently at API 1/2/3 (UI P4/P3/P2)
    present_api_levels = sorted({int(t.get("priority", 1)) for t in due_today if int(t.get("priority", 1)) in (1, 2, 3)})
    if not present_api_levels:
        return {}

    present_ui_levels = sorted({API_TO_UI[a] for a in present_api_levels})  # subset of {2,3,4}
    ui_map = {old_ui: new_ui for new_ui, old_ui in enumerate(present_ui_levels, start=1)}  # compress to {1..N}

    api_map: Dict[int, int] = {}
    for old_ui, new_ui in ui_map.items():
        old_api = UI_TO_API[old_ui]
        new_api = UI_TO_API[new_ui]
        api_map[old_api] = new_api

    return api_map


def github_inactivity_days() -> Optional[int]:
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

    pushed_dt = dt.datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    days = (dt.datetime.now(dt.timezone.utc).date() - pushed_dt.date()).days
    return max(days, 0)


def maybe_create_github_expiry_task(client: TodoistClient, active_tasks: List[Dict[str, Any]]) -> None:
    warn_days = int(os.getenv("GH_WARN_DAYS", "55"))
    disable_days = int(os.getenv("GH_DISABLE_DAYS", "60"))
    inactivity = github_inactivity_days()
    if inactivity is None:
        return
    if not (warn_days <= inactivity < disable_days):
        return

    marker = os.getenv("GH_TASK_MARKER", "[GH-ACTIONS-KEEPALIVE]").strip()
    if any(marker in (t.get("content") or "") for t in active_tasks):
        return

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    actions_url = f"https://github.com/{repo}/actions" if repo else "https://github.com (open your repo → Actions)"

    content = (
        f"{marker} GitHub Actions may stop soon — sign in and run/refresh\n"
        f"Inactivity: ~{inactivity} days\n"
        f"{actions_url}"
    )

    # UI P1 -> API 4
    client.create_task(content=content, api_priority=UI_TO_API[1], due_string="today")


def main() -> int:
    token = os.getenv("TODOIST_TOKEN", "").strip()
    if not token:
        die("Missing TODOIST_TOKEN env var (set it as a GitHub Actions secret).")

    tz = get_local_tz()
    now_local = dt.datetime.now(tz)
    today_local = now_local.date()

    client = TodoistClient(token)

    # Fetch active tasks once
    active = client.get_all_active_tasks()

    # GitHub expiry warning
    maybe_create_github_expiry_task(client, active)

    # Refresh once if we might have created something
    active = client.get_all_active_tasks()

    # ---- Rules:
    # A) Overdue => UI P1 (API 4)
    # B) Checked => default (API 1)
    # C) Anything NOT due today (and NOT overdue) => default (API 1)  <-- your new requirement

    desired: Dict[str, int] = {}

    for t in active:
        task_id = str(t.get("id"))
        cur_api = int(t.get("priority", 1))

        # Checked -> default
        if t.get("checked") is True:
            if cur_api != UI_TO_API[4]:
                desired[task_id] = UI_TO_API[4]
            continue

        # If no due date, treat as "not due today" => clear priority
        if not t.get("due"):
            if cur_api != UI_TO_API[4]:
                desired[task_id] = UI_TO_API[4]
            continue

        # Overdue -> P1
        if is_overdue(t, now_local, today_local, tz):
            if cur_api != UI_TO_API[1]:
                desired[task_id] = UI_TO_API[1]
            continue

        # Not overdue; if not due today => clear priority
        if not is_due_today(t, today_local, tz):
            if cur_api != UI_TO_API[4]:
                desired[task_id] = UI_TO_API[4]
            continue

        # Due today and not overdue: leave priority as-is for now (cascade handles later)

    # Apply updates (only where needed)
    for task_id, new_api in desired.items():
        client.update_task_priority(task_id, new_api)

    # Re-fetch once so cascade sees current truth (especially “no P1 exists?”)
    active = client.get_all_active_tasks()

    # ---- Cascade (after 12:05) only if no UI P1 exists anywhere ----
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
