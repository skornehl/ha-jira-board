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

    @property
    def base_url(self) -> str:
        # Exposed so callers (the get_issue service handler) can build a
        # "view in Jira" link without duplicating the site URL elsewhere.
        return self._base_url

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

    async def list_assignable_users(self, project_key: str) -> list[dict[str, Any]]:
        """List users assignable to issues in `project_key`, for the
        details popup's edit-mode assignee dropdown. Capped at Jira's
        default maxResults (50) - plenty for how this board is actually
        used (a personal/family site, not an enterprise instance with
        hundreds of members)."""
        data = await self._request(
            "GET", f"/rest/api/3/user/assignable/search?project={project_key}"
        )
        return [
            {
                "account_id": u["accountId"],
                "name": u.get("displayName"),
                "avatar_url": (u.get("avatarUrls") or {}).get("48x48"),
            }
            for u in data
        ]

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch one issue's full details, for the card's click-to-detail popup.

        Only the fields the popup actually shows are requested (cheap,
        avoids ever pulling e.g. attachments by accident). `comment` is
        included so the popup can show existing comments, not just let
        you add new ones blind - Jira's default page size for the
        embedded `comment` field (not the dedicated, paginated /comment
        endpoint) is plenty for how this board is actually used.
        `expand=renderedFields` gets `description` (and each comment's
        body) back as ready-to-display HTML instead of Jira's Atlassian
        Document Format (ADF) JSON tree - avoids needing an ADF renderer
        client-side.
        """
        fields = ",".join(
            [
                "summary",
                "description",
                "status",
                "issuetype",
                "project",
                "assignee",
                "reporter",
                "priority",
                "labels",
                "duedate",
                "created",
                "updated",
                "comment",
            ]
        )
        return await self._request(
            "GET",
            f"/rest/api/3/issue/{issue_key}?fields={fields}&expand=renderedFields",
        )

    async def list_priorities(self) -> list[dict[str, str]]:
        """List every priority configured on this Jira site, for the
        details popup's edit-mode priority dropdown (not per-project - a
        plain GET returning the whole array, no pagination)."""
        data = await self._request("GET", "/rest/api/3/priority")
        return [{"name": p["name"], "icon_url": p.get("iconUrl")} for p in data]

    async def fetch_asset_data_uri(self, url: str) -> str:
        """Fetch a small public static asset (a priority's iconUrl) and
        return it as a `data:` URI, so the frontend can embed it directly
        instead of the browser hotlinking the URL itself.

        Hotlinking turned out unreliable in practice: this server-side
        request to the exact same URL always succeeds (verified directly,
        no auth needed - these assets are public), but a real browser
        loading it cross-origin was observed to silently fail (no error
        surfaced anywhere reachable, just an <img> that never renders,
        removed by its own onerror handler) - something about the
        client's own network path (ad-blocker, extension, DNS/firewall
        filtering) blocking it, not anything this integration can fix
        from the frontend side. Fetching it here, where it demonstrably
        works, sidesteps the problem entirely. Deliberately doesn't send
        this client's auth headers - the URL isn't guaranteed to be the
        same host as `base_url` in general, and these assets don't need
        auth anyway.
        """
        async with self._session.request("GET", url, timeout=TIMEOUT) as resp:
            if resp.status >= 400:
                raise JiraApiError(resp.status, (await resp.text())[:200])
            content_type = resp.headers.get("Content-Type", "image/svg+xml").split(";")[0]
            data = await resp.read()
        return f"data:{content_type};base64,{base64.b64encode(data).decode()}"

    async def update_issue(
        self,
        issue_key: str,
        summary: str,
        description: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        assignee_account_id: str | None = None,
        labels: list[str] | None = None,
    ) -> None:
        """Overwrite an issue's summary and (optionally) description,
        priority, due date, assignee, and/or labels.

        `summary` is always sent - the popup's edit form always shows it,
        so there's never a reason to omit it. `description` is only
        touched when the caller actually passed one: the popup only
        includes it in the request when its textarea was actually edited
        (see jira-board-card.js), precisely to avoid this round-trip
        silently flattening the original description's rich formatting
        (bold, links, lists, ...) into plain paragraphs just because the
        user only meant to fix a typo in the summary - see _text_to_adf.

        `priority`, `due_date`, `assignee_account_id` and `labels` carry
        no such round-trip risk (all plain scalars/lists, never lossy) so
        the popup always sends its current form values for these
        regardless of whether they changed. `due_date`/`assignee_
        account_id` are `""` to clear them, or omitted/None to leave them
        untouched; `labels` is `[]` to clear all of them (replaces the
        full list, doesn't merge with what's already there), or
        omitted/None to leave them untouched.
        """
        fields: dict[str, Any] = {"summary": summary}
        if description is not None:
            fields["description"] = _text_to_adf(description) if description else None
        if priority is not None:
            fields["priority"] = {"name": priority}
        if due_date is not None:
            fields["duedate"] = due_date or None
        if assignee_account_id is not None:
            fields["assignee"] = {"accountId": assignee_account_id or None}
        if labels is not None:
            fields["labels"] = labels
        await self._request("PUT", f"/rest/api/3/issue/{issue_key}", json={"fields": fields})

    async def add_comment(self, issue_key: str, text: str) -> None:
        """Post a plain-text comment - no rich-text editor is offered for
        this, so `text` always goes through the same minimal ADF wrapping
        as a plain-text description edit."""
        await self._request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/comment",
            json={"body": _text_to_adf(text)},
        )


def _text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text in a minimal Atlassian Document Format (ADF) doc -
    one paragraph per line. Jira Cloud's *write* API requires
    description/comment bodies in ADF rather than plain text (the reverse
    of the read side, where expand=renderedFields hands back plain HTML
    instead) - this card offers no rich-text editing, so a paragraph-per-
    line structure is deliberately the full extent of it, just enough for
    Jira to accept the write and show something reasonable afterwards.
    """
    lines = text.split("\n")
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            if line
            else {"type": "paragraph"}
            for line in lines
        ],
    }


def format_issue_for_card(issue: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Flatten a raw `get_issue` response into the plain shape the card's
    detail popup renders directly, keeping Jira's actual response shape
    (nested `fields`/`renderedFields`, People objects, ...) out of the
    frontend - a standalone function (not a JiraClient method) since it's
    pure data reshaping with no API call of its own, used by the
    `get_issue` service handler in __init__.py.
    """

    def _person(person: dict[str, Any] | None) -> dict[str, Any] | None:
        if not person:
            return None
        return {
            "name": person.get("displayName"),
            "avatar": (person.get("avatarUrls") or {}).get("48x48"),
            # Only actually needed for assignee (pre-selecting the edit
            # popup's dropdown) - included for reporter too since it's
            # the exact same shape either way and there's no reason to
            # special-case it out.
            "account_id": person.get("accountId"),
        }

    fields = issue.get("fields", {})
    rendered = issue.get("renderedFields", {})
    priority = fields.get("priority")

    # Comments come back twice in parallel - fields.comment.comments has
    # the raw ADF body, renderedFields.comment.comments has the same list
    # pre-rendered to HTML (same relationship as description/
    # description_html above). Matched up by id since that's the only
    # thing guaranteed to line up between the two lists.
    raw_comments = (fields.get("comment") or {}).get("comments", [])
    rendered_by_id = {
        c.get("id"): c.get("body")
        for c in (rendered.get("comment") or {}).get("comments", [])
    }
    comments = [
        {
            "author": (c.get("author") or {}).get("displayName"),
            "created": c.get("created"),
            "body_html": rendered_by_id.get(c.get("id")) or "",
        }
        for c in raw_comments
    ]

    return {
        "key": issue["key"],
        "url": f"{base_url}/browse/{issue['key']}",
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "project": (fields.get("project") or {}).get("key"),
        "assignee": _person(fields.get("assignee")),
        "reporter": _person(fields.get("reporter")),
        "priority": priority.get("name") if priority else None,
        "priority_icon": priority.get("iconUrl") if priority else None,
        "labels": fields.get("labels") or [],
        "due_date": fields.get("duedate"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "description_html": rendered.get("description") or "",
        "comments": comments,
    }
