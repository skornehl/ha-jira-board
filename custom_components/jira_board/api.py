"""Minimal async Jira Cloud REST API client used by the Jira Board integration.

Deliberately hand-rolled instead of pulling in a third-party Jira SDK: we
only need a handful of endpoints (search, transitions, issue create), and
Jira Cloud's REST v3 API is simple enough that a small wrapper keeps the
integration dependency-free.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

TIMEOUT = ClientTimeout(total=20)


class JiraApiError(Exception):
    """Raised on any non-2xx response from the Jira REST API."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Jira API error {status}: {message}")
        self.status = status


class JiraClient:
    """Thin async wrapper around the Jira Cloud REST API (v3)."""

    def __init__(
        self, session: ClientSession, base_url: str, email: str, api_token: str
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self._base_url}{path}"
        async with self._session.request(
            method, url, headers=self._headers, json=json, timeout=TIMEOUT
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise JiraApiError(resp.status, body[:500])
            if resp.status == 204 or resp.content_length == 0:
                return None
            return await resp.json()

    async def test_auth(self) -> dict[str, Any]:
        """Verify credentials, return the authenticated user's profile."""
        return await self._request("GET", "/rest/api/3/myself")

    async def list_projects(self) -> list[dict[str, str]]:
        """List projects visible to this account, for the config flow's picker.

        A single page (up to 200) is plenty for how this integration is
        actually used - personal/small-team Jira sites, not an enterprise
        instance with hundreds of projects - so pagination isn't
        implemented; `isLast` is checked only to log if it was truncated.
        """
        data = await self._request(
            "GET", "/rest/api/3/project/search?maxResults=200&orderBy=key"
        )
        if not data.get("isLast", True):
            _LOGGER.warning(
                "More than %d projects visible - only the first page is offered "
                "in the setup dialog",
                len(data.get("values", [])),
            )
        return [
            {"key": p["key"], "name": p["name"]} for p in data.get("values", [])
        ]

    async def search_issues(
        self, jql: str, fields: list[str], max_results: int = 200
    ) -> list[dict[str, Any]]:
        """Run a JQL search, return the raw list of issue dicts."""
        payload = {"jql": jql, "maxResults": max_results, "fields": fields}
        data = await self._request("POST", "/rest/api/3/search/jql", json=payload)
        return data.get("issues", [])

    async def get_transitions(self, issue_key: str) -> list[dict[str, Any]]:
        """List the transitions currently available for an issue."""
        data = await self._request(
            "GET", f"/rest/api/3/issue/{issue_key}/transitions"
        )
        return data.get("transitions", [])

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        """Move an issue through the given transition ID."""
        await self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/transitions",
            json={"transition": {"id": transition_id}},
        )

    async def transition_to_status(self, issue_key: str, status_name: str) -> bool:
        """Move an issue to the given status by name.

        Looks up the issue's currently valid transitions and picks the one
        whose target matches `status_name`. Transition IDs are per-project
        (sometimes per-issue-type) and not stable across projects, so this
        must always be resolved dynamically rather than hardcoded.

        Returns True if a matching transition was found and applied, False
        if the target status isn't a valid transition from the issue's
        current state (e.g. workflow doesn't allow skipping a column) -
        callers should treat False as "move rejected, revert the UI".
        """
        transitions = await self.get_transitions(issue_key)
        for t in transitions:
            if t["to"]["name"] == status_name:
                await self.transition_issue(issue_key, t["id"])
                return True
        _LOGGER.warning(
            "No transition to status '%s' available for %s (workflow may not "
            "allow this move)",
            status_name,
            issue_key,
        )
        return False

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        epic_key: str | None = None,
    ) -> str:
        """Create a new issue, return its key (e.g. 'HA-50').

        `epic_key`, if given, links it as the new issue's parent Epic in
        one call - team-managed Jira projects use the plain `parent` field
        for this (same field a subtask uses for its parent issue), see the
        matching comment in coordinator.py.
        """
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": issue_type},
            }
        }
        if epic_key:
            payload["fields"]["parent"] = {"key": epic_key}
        data = await self._request("POST", "/rest/api/3/issue", json=payload)
        return data["key"]
