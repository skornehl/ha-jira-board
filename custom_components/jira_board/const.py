"""Constants for the Jira Board integration."""

DOMAIN = "jira_board"

CONF_BASE_URL = "base_url"
CONF_EMAIL = "email"
CONF_API_TOKEN = "api_token"
CONF_PROJECTS = "projects"
CONF_DEFAULT_PROJECT = "default_project"

DEFAULT_SCAN_INTERVAL_SECONDS = 120

# Board columns, in display order. Value = Jira status name (must match
# exactly, case-sensitive, as returned by the Jira REST API / used as
# `to.name` on issue transitions). This is the union of statuses actually
# seen across FAM/HA/HUG on 2026-09-10; not every project's workflow
# necessarily offers every column as a valid transition target - moves that
# aren't a valid transition for a given issue fail gracefully and are
# reverted on the next coordinator refresh.
COLUMNS: list[str] = ["To Do", "In Progress", "In Review", "Done"]

# Slug used for each column's todo.* entity_id, e.g. todo.jira_board_to_do
COLUMN_SLUGS: dict[str, str] = {
    "To Do": "to_do",
    "In Progress": "in_progress",
    "In Review": "in_review",
    "Done": "done",
}

ATTR_ISSUE_KEY = "issue_key"
ATTR_PROJECT = "project"

# JQL fragment: only issues that are "live" on the board (not older resolved
# work) - Done issues resolved more than N days ago are excluded so the
# board doesn't accumulate ancient history forever.
DONE_RETENTION_DAYS = 14
