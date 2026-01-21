# Todoist Priority Janitor

Automates Todoist priorities via GitHub Actions.

## What it does (Todoist UI terms)
1. Overdue tasks => Priority 1
2. Completed/checked tasks => clear priority (back to default) but keep labels
3. If there are NO Priority 1 tasks, then after 12:05 (America/New_York):
   - For tasks due today only: P4->P3, P3->P2, P2->P1
4. Creates a Priority 1 warning task if GitHub scheduled workflows may stop soon
   (scheduled workflows may be disabled after 60 days of repo inactivity in public repos).

## Important note about priorities
Todoist REST API uses: 1=normal ... 4=urgent.
Todoist UI uses P1 as most urgent.
This script maps UI P1 -> API priority 4.

## Setup
1. Create a new GitHub repo and add these files.
2. In Todoist: Settings -> Integrations -> Developer -> copy your API token.
3. In GitHub repo settings -> Secrets and variables -> Actions -> New repository secret:
   - TODOIST_TOKEN = your Todoist API token
   - (optional) TODOIST_PROJECT_ID = project id to place the GitHub warning task (otherwise Inbox)
4. Go to Actions tab and enable workflows.
5. Run the workflow once manually (workflow_dispatch) to confirm it works.

## Optional knobs (env vars)
- COMPLETED_LOOKBACK_HOURS (default 24)
- GH_WARN_DAYS (default 55)
- GH_DISABLE_DAYS (default 60)
- LOCAL_TZ (default America/New_York)
