"""Data update coordinator for the Jira Board integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import JiraApiError, JiraClient
from .const import COLUMNS, DOMAIN, DONE_RETENTION_DAYS

_LOGGER = logging.getLogger(__name__)

FIELDS = [
    "summary",
    "status",
    "project",
    "resolutiondate",
    "parent",
    "priority",
    "duedate",
    "assignee",
    "labels",
]
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
        # This Jira site's configured priorities, for the details popup's
        # edit-mode priority dropdown - site-wide and effectively static,
        # but fetched on the same poll as everything else rather than
        # once at startup, same reasoning as all_epics: simplest way to
        # keep it correct without a separate refresh path, and cheap
        # enough (one tiny request) not to matter.
        # [{"name": ..., "icon_url": ..., "icon_data": ...}]
        self.all_priorities: list[dict] = []
        # url -> already-fetched data: URI - see _embed_asset. Shared by
        # priority icons and assignee avatars; kept across polls rather
        # than re-fetched every scan_interval since both are effectively
        # static per URL (a priority's icon never changes; a person's
        # avatar essentially never does either) - each distinct asset is
        # only ever actually fetched once for the coordinator's lifetime.
        self._asset_cache: dict[str, str] = {}

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

        try:
            self.all_priorities = await self._embed_priority_icons(
                await self.client.list_priorities()
            )
        except JiraApiError as err:
            # Same non-fatal handling as Epics above - the edit popup's
            # priority dropdown just temporarily falls back to whatever
            # it fetched last time (or the issue's own current value).
            _LOGGER.warning("Fetching priorities failed, keeping previous list: %s", err)
        priority_icon_by_name = {
            p["name"]: p.get("icon_data") or p.get("icon_url") for p in self.all_priorities
        }

        # Pre-embed each *distinct* assignee avatar once (not per-issue -
        # a handful of people can easily be assigned dozens of issues
        # between them), same batching idea as priorities above.
        avatar_urls = {
            ((issue["fields"].get("assignee") or {}).get("avatarUrls") or {}).get("48x48")
            for issue in issues
        }
        avatar_urls.discard(None)
        avatar_data_by_url = {url: await self._embed_asset(url) for url in avatar_urls}

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
            priority_name = priority.get("name") if priority else None
            assignee = issue["fields"].get("assignee")
            assignee_avatar_url = (
                (assignee.get("avatarUrls") or {}).get("48x48") if assignee else None
            )

            by_column[column].append(
                {
                    "key": key,
                    "summary": issue["fields"]["summary"],
                    "project": issue["fields"]["project"]["key"],
                    "epic_key": epic_key,
                    "epic_name": epic_name,
                    "priority": priority_name,
                    # Embedded data: URI (falls back to the plain hotlink
                    # URL if embedding it failed) - see _embed_asset for
                    # why this isn't simply priority["iconUrl"] anymore.
                    "priority_icon": priority_icon_by_name.get(priority_name),
                    # Plain "YYYY-MM-DD", no time component - Jira's duedate
                    # field is a date, not a datetime.
                    "due_date": issue["fields"].get("duedate"),
                    "assignee_name": assignee.get("displayName") if assignee else None,
                    "assignee_avatar": avatar_data_by_url.get(assignee_avatar_url),
                    "labels": issue["fields"].get("labels") or [],
                }
            )

        # Clear just_moved entries once Jira's own data confirms the move
        # (or the issue vanished, e.g. deleted upstream).
        for key in list(self.just_moved):
            if key not in seen or self.just_moved[key] == self._status_of(issues, key):
                self.just_moved.pop(key, None)

        return by_column

    async def _embed_asset(self, url: str | None) -> str | None:
        """Fetch `url` (a priority icon, an assignee's avatar, ...) once
        and return it as a `data:` URI the frontend can embed directly,
        instead of the browser hotlinking it.

        Hotlinking these turned out unreliable in the wild: this
        integration's own network path always succeeds, but a real
        browser loading the exact same URL cross-origin was observed to
        silently fail (no error surfaced anywhere reachable - the image
        just never rendered, removed by its own onerror handler) -
        something about that client's own network path (ad-blocker,
        browser extension, DNS/firewall filtering) blocking it, not
        anything fixable from the frontend side. Embedding it here, where
        fetching it demonstrably works, sidesteps the problem entirely.
        Falls back to the original URL if embedding it fails (e.g. this
        specific asset 404s) - not worse than the old hotlinking behavior
        in that case, just doesn't fix it either.
        """
        if not url:
            return None
        if url not in self._asset_cache:
            try:
                self._asset_cache[url] = await self.client.fetch_asset_data_uri(url)
            except JiraApiError as err:
                _LOGGER.debug("Could not embed asset %s, falling back to hotlink: %s", url, err)
                return url
        return self._asset_cache[url]

    async def _embed_priority_icons(self, priorities: list[dict]) -> list[dict]:
        """Add `icon_data` (see _embed_asset) to each priority."""
        for p in priorities:
            if p.get("icon_url"):
                p["icon_data"] = await self._embed_asset(p["icon_url"])
        return priorities

    @staticmethod
    def _status_of(issues: list[dict], key: str) -> str | None:
        for issue in issues:
            if issue["key"] == key:
                return issue["fields"]["status"]["name"]
        return None

    def note_local_move(self, issue_key: str, column: str) -> None:
        """Record that we just moved `issue_key` to `column` locally."""
        self.just_moved[issue_key] = column
