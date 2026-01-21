#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
import uuid
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


API_BASE = "https://api.todoist.com/api/v1"


@dataclass
class TodoistClient:
    token: str

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get_all_active_tasks(self, limit: int = 200) -> List[Dict[str, Any]]:
        """
        GET /api/v1/tasks (paginated; cursor-based)
        Returns a flat list of active tasks.
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

            # API returns: { "results": [...], "next_cursor": "..." }
            page = payload.get("results", [])
            tasks.extend(page)

            cursor = payload.get("next_cursor")
            if not cursor:
                break

        return tasks

    def update_task_priority(self, task_id: str, priority: int) -> None:
        """
        POST /api/v1/tasks/{task_id} with {"priority": N}
        """
        if priority < 1 or priority > 4:
            raise ValueError("priority must be between 1 and 4")

        r = requests.post(
            f"{API_BASE}/tasks/{task_id}",
            headers=self._headers(),
            data=json.dumps({"priority": priority}),
            timeout=30,
        )
        r.raise_for_status()

    def create_task(self, content: str, priority: int = 4, due_string: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v1/tasks
        """
        body: Dict[str, Any] = {"content": content, "priority": priority}
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


def parse_due_to_local(due_obj: dict, tz: dt.tzinfo):
    """
    Handles Todoist oddities:
    - due["date"] may be YYYY-MM-DD OR full datetime
    - due["datetime"] may or may not exist
    """
    if not due_obj:
        return None, None

    # Prefer explicit datetime if present
    if due_obj.get("datetime"):
        iso = due_obj["datetime"]
        due_dt = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz)
        return due_dt_local, due_dt_local.date()

    raw = due_obj.get("date")
    if not raw:
        return None, None

    # If date field actually contains a datetime
    if "T" in raw:
        due_dt = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        due_dt_local = due_dt.astimezone(tz)
        return due_dt_local, due_dt_local.date()

    # True date-only
    d = dt.date.fromisoformat(raw)
    return None, d



def is_overdue(task: Dict[str, Any], now_local: dt.datetime, today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    due_dt_local, due_date_local = parse_due_to_local(due, tz)

    if due_date_local is None:
        return False

    if due_dt_local is not None:
        return due_dt_local < now_local
    else:
        return due_date_local < today_local


def is_due_today(task: Dict[str, Any], today_local: dt.date, tz: dt.tzinfo) -> bool:
    due = task.get("due") or {}
    _, due_date_local = parse_due_to_local(due, tz)
    return (due_date_local == today_local)


def get_local_tz() -> dt.tzinfo:
    """
    Uses TZ env var if set (recommended), else falls back to America/New_York.
    """
    tz_name = os.getenv("TZ", "America/New_York")
    if ZoneInfo is None:
        # Worst-case fallback: naive local time
        return dt.timezone(dt.timedelta(hours=-5))
    return ZoneInfo(tz_name)


def after_1205(now_local: dt.datetime) -> bool:
    return (now_local.hour, now_local.minute) >= (12, 5)


def cascade_priorities_for_today(tasks_due_today: List[Dict[str, Any]]) -> Dict[int, int]:
    """
    Compress priority levels for tasks due today (only when there are no P1 tasks globally).
    Example sets:
      {2} -> {1}
      {3} -> {1}
      {4} -> {1}
      {2,4} -> {1,2}   (so P4 -> P2)
      {3,4} -> {1,2}
      {2,3,4} -> {1,2,3}
    Returns a mapping old_priority -> new_priority
    """
    levels = sorted({t.get("priority") for t in tasks_due_today if t.get("priority") in (2, 3, 4)})
    mapping: Dict[int, int] = {}
    for i, lvl in enumerate(levels, start=1):
        mapping[lvl] = i  # compress to 1..N
    return mapping


def ensure_monthly_github_keepalive_task(client: TodoistClient, active_tasks: List[Dict[str, Any]], today_local: dt.date) -> None:
    """
    Creates a P1 task on the 1st of each month if one doesn't already exist today.
    This is your “sign in / keep Actions alive” reminder.
    """
    if today_local.day != 1:
        return

    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        actions_url = f"https://github.com/{repo}/actions"
    else:
        actions_url = "https://github.com/ (open your repo → Actions)"

    marker = "Todoist Priority Janitor — GitHub Actions keepalive"
    already = any(
        (marker in (t.get("content") or "")) and (t.get("priority") == 1) and (t.get("due") and (t["due"].get("date") == today_local.isoformat()))
        for t in active_tasks
    )
    if already:
        return

    content = (
        f"{marker}\n"
        f"Sign in and re-run workflow if needed.\n"
        f"{actions_url}"
    )
    # due_string "today" is fine; Todoist will interpret in account timezone
    client.create_task(content=content, priority=1, due_string="today")


def main() -> int:
    token = os.getenv("TODOIST_TOKEN", "").strip()
    if not token:
        print("ERROR: Missing TODOIST_TOKEN env var (set it as a GitHub Actions secret).")
        return 2

    tz = get_local_tz()
    now_local = dt.datetime.now(tz)
    today_local = now_local.date()

    client = TodoistClient(token=token)

    # 1) Fetch tasks
    active = client.get_all_active_tasks()

    # 4) Monthly reminder task (do this early so it can participate in “P1 exists?”)
    ensure_monthly_github_keepalive_task(client, active, today_local)
    # refresh list in case we just created one
    active = client.get_all_active_tasks()

    # 2) Overdue => P1
    updates: List[Tuple[str, int, int]] = []  # (task_id, old, new)

    for t in active:
        if not (t.get("due")):
            continue
        if is_overdue(t, now_local, today_local, tz):
            if t.get("priority") != 1:
                updates.append((t["id"], t.get("priority", 4), 1))

    # 3) Checked => clear priority (P4), labels untouched
    for t in active:
        if t.get("checked") is True:
            if t.get("priority") != 4:
                updates.append((t["id"], t.get("priority", 4), 4))

    # Apply overdue/checked updates first
    if updates:
        # de-dup: last write wins per task_id
        last: Dict[str, Tuple[int, int]] = {}
        for task_id, oldp, newp in updates:
            last[task_id] = (oldp, newp)

        for task_id, (_oldp, newp) in last.items():
            client.update_task_priority(task_id, newp)

        # refresh after updates
        active = client.get_all_active_tasks()

    # 4) Cascading: only if NO P1 tasks exist, and only after 12:05 local, and only for due-today tasks
    any_p1 = any((t.get("priority") == 1) for t in active)
    if (not any_p1) and after_1205(now_local):
        due_today = [t for t in active if (t.get("due") and is_due_today(t, today_local, tz) and not (t.get("checked") is True))]
        mapping = cascade_priorities_for_today(due_today)

        if mapping:
            for t in due_today:
                oldp = t.get("priority")
                if oldp in mapping:
                    newp = mapping[oldp]
                    if newp != oldp:
                        client.update_task_priority(t["id"], newp)

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
