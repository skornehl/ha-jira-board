"""Data update coordinator for the Jira Board integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import JiraApiError, JiraClient
from .const import COLUMNS, DOMAIN, DONE_RETENTION_DAYS

_LOGGER = logging.getLogger(__name__)

FIELDS = ["summary", "status", "project", "resolutiondate"]


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
        # issue_key -> column name we just forced it into locally
        self.just_moved: dict[str, str] = {}

    def _jql(self) -> str:
        projects = ", ".join(self.projects)
        return (
            f"project in ({projects}) AND "
            f"(statusCategory != Done OR resolutiondate >= -{DONE_RETENTION_DAYS}d) "
            "ORDER BY updated DESC"
        )

    async def _async_update_data(self) -> dict[str, list[dict]]:
        try:
            issues = await self.client.search_issues(self._jql(), FIELDS)
        except JiraApiError as err:
            raise UpdateFailed(f"Jira search failed: {err}") from err

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
            by_column[column].append(
                {
                    "key": key,
                    "summary": issue["fields"]["summary"],
                    "project": issue["fields"]["project"]["key"],
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
