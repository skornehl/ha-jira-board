"""Data update coordinator for the Jira Board integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import JiraApiError, JiraClient
from .const import COLUMNS, DOMAIN, DONE_RETENTION_DAYS

_LOGGER = logging.getLogger(__name__)

FIELDS = ["summary", "status", "project", "resolutiondate", "parent", "priority", "duedate"]
EPIC_FIELDS = ["summary"]


class JiraBoardCoordinator(DataUpdateCoordinator[dict[str, list[dict]]]):
    """Polls Jira and exposes issues grouped by board column (status name).

    `data` is a dict: {column_name: [{"key": ..., "summary": ..., "project": ...}, ...]}

    Also tracks `just_moved`: a short-lived set of issue keys that a local
    (HA-side) drag just transitioned in Jira, so a stale/lagging search
    result on the very next poll doesn't visibly snap the card back before
    Jira's index catches up.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: JiraClient,
        projects: list[str],
        default_project: str,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.projects = projects
        # Single source of truth for which project a brand-new card lands
        # in - todo.py reads it from here rather than the config entry
        # directly, so it stays correct whether it came from the entry's
        # `data` (initial setup) or `options` (changed later via the
        # options flow) without duplicating that resolution logic.
        self.default_project = default_project
        # issue_key -> column name we just forced it into locally
        self.just_moved: dict[str, str] = {}
        # Epics for the configured projects, *including* ones with zero
        # cards currently on the board - by_column/todo_items only ever
        # sees an Epic if some issue's `parent` points at it, so a
        # freshly-created (or fully-filtered-out) Epic would otherwise
        # never get a lane in "Group by Epic" view. Fetched separately
        # since search/jql has no "give me every parent of these issues,
        # even absent ones" mode. [{"key": ..., "name": ..., "project": ...}]
        self.all_epics: list[dict] = []

    def _jql(self) -> str:
        # issuetype != Epic: Epics themselves have no `parent`, so without
        # this they'd show up as ordinary cards in the "Kein Epic" lane -
        # on top of, not instead of, their own proper lane from
        # all_epics/_epic_jql below. Found 2026-09-15, the same day the
        # empty-lane fix made it obvious (an Epic previously just quietly
        # sat uncategorized; now it visibly duplicated itself).
        projects = ", ".join(self.projects)
        return (
            f"project in ({projects}) AND issuetype != Epic AND "
            f"(statusCategory != Done OR resolutiondate >= -{DONE_RETENTION_DAYS}d) "
            "ORDER BY updated DESC"
        )

    def _epic_jql(self) -> str:
        projects = ", ".join(self.projects)
        # Same Done-retention window as regular issues (see _jql) so a
        # long-closed Epic doesn't linger as a permanent empty lane.
        return (
            f"project in ({projects}) AND issuetype = Epic AND "
            f"(statusCategory != Done OR resolutiondate >= -{DONE_RETENTION_DAYS}d) "
            "ORDER BY key ASC"
        )

    async def _async_update_data(self) -> dict[str, list[dict]]:
        try:
            issues = await self.client.search_issues(self._jql(), FIELDS)
        except JiraApiError as err:
            raise UpdateFailed(f"Jira search failed: {err}") from err

        try:
            epic_issues = await self.client.search_issues(self._epic_jql(), EPIC_FIELDS)
            self.all_epics = [
                {
                    "key": e["key"],
                    "name": e["fields"]["summary"],
                    "project": e["key"].split("-")[0],
                }
                for e in epic_issues
            ]
        except JiraApiError as err:
            # Not fatal for the board itself (columns/cards still work) -
            # "Group by Epic" just temporarily falls back to only the
            # epics inferable from items on screen, same as before this
            # feature existed.
            _LOGGER.warning("Fetching Epics failed, keeping previous list: %s", err)

        by_column: dict[str, list[dict]] = {c: [] for c in COLUMNS}
        seen: set[str] = set()
        for issue in issues:
            key = issue["key"]
            seen.add(key)
            status = issue["fields"]["status"]["name"]
            # Honor a just-applied local move even if Jira's search index is
            # a beat behind and still reports the old status.
            column = self.just_moved.get(key, status)
            if column not in by_column:
                # Unmapped status (workflow has a column we don't know
                # about) - park it in the first column rather than drop it
                # silently, so it's still visible and actionable.
                _LOGGER.debug(
                    "Issue %s has unmapped status '%s', showing under '%s'",
                    key,
                    status,
                    COLUMNS[0],
                )
                column = COLUMNS[0]
            epic_key = None
            epic_name = None
            parent = issue["fields"].get("parent")
            # Team-managed projects link an Epic via the plain `parent`
            # field (same field a subtask uses for its parent issue) - only
            # treat it as an epic if that's actually what's on the other
            # end, not e.g. a subtask's parent Story.
            if parent and parent["fields"]["issuetype"]["name"] == "Epic":
                epic_key = parent["key"]
                epic_name = parent["fields"]["summary"]

            priority = issue["fields"].get("priority")

            by_column[column].append(
                {
                    "key": key,
                    "summary": issue["fields"]["summary"],
                    "project": issue["fields"]["project"]["key"],
                    "epic_key": epic_key,
                    "epic_name": epic_name,
                    "priority": priority.get("name") if priority else None,
                    "priority_icon": priority.get("iconUrl") if priority else None,
                    # Plain "YYYY-MM-DD", no time component - Jira's duedate
                    # field is a date, not a datetime.
                    "due_date": issue["fields"].get("duedate"),
                }
            )

        # Clear just_moved entries once Jira's own data confirms the move
        # (or the issue vanished, e.g. deleted upstream).
        for key in list(self.just_moved):
            if key not in seen or self.just_moved[key] == self._status_of(issues, key):
                self.just_moved.pop(key, None)

        return by_column

    @staticmethod
    def _status_of(issues: list[dict], key: str) -> str | None:
        for issue in issues:
            if issue["key"] == key:
                return issue["fields"]["status"]["name"]
        return None

    def note_local_move(self, issue_key: str, column: str) -> None:
        """Record that we just moved `issue_key` to `column` locally."""
        self.just_moved[issue_key] = column
