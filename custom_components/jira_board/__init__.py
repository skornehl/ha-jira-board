"""The Jira Board integration - a live, drag-and-drop Kanban view of Jira issues.

Each board column (To Do / In Progress / In Review / Done) is exposed as its
own `todo.*` entity. Moving a card between columns in a compatible frontend
(e.g. the Hakanban Lovelace card, HACS: neilellis/hakanban) transitions the
underlying Jira issue via the REST API - this is a genuine two-way sync, not
a one-off import.
"""
from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JiraApiError, JiraClient, format_issue_for_card
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_DEFAULT_PROJECT,
    CONF_EMAIL,
    CONF_PROJECTS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)
from .coordinator import JiraBoardCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["todo"]

SERVICE_GET_ISSUE = "get_issue"
SERVICE_UPDATE_ISSUE = "update_issue"
SERVICE_ADD_COMMENT = "add_comment"
ATTR_ISSUE_KEY = "issue_key"
ATTR_BOARD_ENTITY_ID = "board_entity_id"
ATTR_SUMMARY = "summary"
ATTR_DESCRIPTION = "description"
ATTR_COMMENT = "comment"
ATTR_PRIORITY = "priority"
ATTR_DUE_DATE = "due_date"
ATTR_ASSIGNEE_ACCOUNT_ID = "assignee_account_id"
ATTR_LABELS = "labels"
GET_ISSUE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ISSUE_KEY): cv.string,
        vol.Optional(ATTR_BOARD_ENTITY_ID): cv.entity_id,
    }
)
UPDATE_ISSUE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ISSUE_KEY): cv.string,
        vol.Optional(ATTR_BOARD_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_SUMMARY): cv.string,
        # Optional and distinct from "" on purpose: omitted means "leave
        # the description untouched", "" means "clear it" - see
        # JiraClient.update_issue's docstring for why the card only ever
        # sends this when its textarea was actually edited.
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        # Unlike description, always sent by the card (never lossy, so
        # there's no "only if changed" guard) - see JiraClient.
        # update_issue's docstring.
        vol.Optional(ATTR_PRIORITY): cv.string,
        vol.Optional(ATTR_DUE_DATE): cv.string,
        vol.Optional(ATTR_ASSIGNEE_ACCOUNT_ID): cv.string,
        vol.Optional(ATTR_LABELS): [cv.string],
    }
)
ADD_COMMENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ISSUE_KEY): cv.string,
        vol.Optional(ATTR_BOARD_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_COMMENT): cv.string,
    }
)

# Bump on every change to www/jira-board-card.js. Appended as a query
# string on the registered URL purely for cache-busting - browsers treat a
# different URL as a different resource, so this is what actually
# guarantees a client picks up a new card version instead of possibly
# serving a stale cached copy despite cache_headers=False below (that flag
# only affects HA's own response headers, not whatever caching heuristics
# the browser decides to apply on its own).
CARD_VERSION = "24"
CARD_URL_PATH = f"/{DOMAIN}_static/jira-board-card.js"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # `options` (set later via the options flow - including a rotated API
    # token) take precedence over the original `data` from initial setup -
    # this is the one place that resolution happens; the coordinator/todo
    # platform read the already-resolved values from the coordinator, not
    # the entry, from here on.
    session = async_get_clientsession(hass)
    client = JiraClient(
        session,
        entry.options.get(CONF_BASE_URL, entry.data[CONF_BASE_URL]),
        entry.options.get(CONF_EMAIL, entry.data[CONF_EMAIL]),
        entry.options.get(CONF_API_TOKEN, entry.data[CONF_API_TOKEN]),
    )
    coordinator = JiraBoardCoordinator(
        hass,
        client,
        entry.options.get(CONF_PROJECTS, entry.data[CONF_PROJECTS]),
        entry.options.get(CONF_DEFAULT_PROJECT, entry.data[CONF_DEFAULT_PROJECT]),
        entry.data.get("scan_interval", DEFAULT_SCAN_INTERVAL_SECONDS),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_frontend(hass)
    await _async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed (tracked projects / default project) - reload so the
    coordinator picks up the new values immediately instead of waiting for
    the next HA restart."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled jira-board-card.js and auto-load it everywhere.

    Registered once per HA run (idempotent - `add_extra_js_url` de-dupes by
    URL), not per config entry, so a second account/board doesn't try to
    double-register the static path.
    """
    if hass.data.get(f"{DOMAIN}_frontend_registered"):
        return
    hass.data[f"{DOMAIN}_frontend_registered"] = True

    www_dir = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_PATH, str(www_dir / "jira-board-card.js"), cache_headers=False)]
    )
    add_extra_js_url(hass, f"{CARD_URL_PATH}?v={CARD_VERSION}")


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register the services backing the card's detail popup (fetching,
    editing, and commenting on a single issue).

    Domain-level (not per-entry/per-entity) and registered once, same
    idempotency reasoning as _async_register_frontend - a second
    account/board must not try to register them twice.
    """
    if hass.services.has_service(DOMAIN, SERVICE_GET_ISSUE):
        return

    async def _async_get_issue(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_BOARD_ENTITY_ID))
        issue_key = call.data[ATTR_ISSUE_KEY]
        try:
            issue = await coordinator.client.get_issue(issue_key)
        except JiraApiError as err:
            raise HomeAssistantError(f"Could not fetch {issue_key}: {err}") from err
        result = format_issue_for_card(issue, coordinator.client.base_url)
        # Swap the hotlinked priority icon URL for the coordinator's
        # already-embedded data: URI when available - see coordinator.py's
        # _embed_asset docstring for why the board's cards don't hotlink
        # it either.
        for p in coordinator.all_priorities:
            if p["name"] == result.get("priority") and p.get("icon_data"):
                result["priority_icon"] = p["icon_data"]
                break
        # Same embed-not-hotlink treatment for assignee/reporter avatars -
        # format_issue_for_card's _person() hands back the raw hotlinked
        # avatarUrls straight from Jira, since it has no access to the
        # coordinator's asset cache (pure data reshaping, no API calls of
        # its own) - swap them here instead, same as priority_icon above.
        for person_key in ("assignee", "reporter"):
            person = result.get(person_key)
            if person and person.get("avatar"):
                person["avatar"] = await coordinator._embed_asset(person["avatar"])
        # Fetched fresh per popup-open rather than cached on the
        # coordinator like all_epics/all_priorities: unlike those, "who
        # can be assigned" is per-project, not board-wide, and only ever
        # needed while the edit form's assignee dropdown is actually open
        # - not worth carrying on every poll for something used this
        # rarely.
        try:
            assignable = await coordinator.client.list_assignable_users(result["project"])
            result["assignable_users"] = [
                {
                    "account_id": u["account_id"],
                    "name": u["name"],
                    # Same embed-not-hotlink treatment as everything else
                    # image-shaped (coordinator.py's _embed_asset).
                    "avatar": await coordinator._embed_asset(u["avatar_url"]),
                }
                for u in assignable
            ]
        except JiraApiError as err:
            _LOGGER.warning(
                "Could not fetch assignable users for %s: %s - edit popup's assignee "
                "dropdown will just show the issue's current assignee",
                result["project"],
                err,
            )
            result["assignable_users"] = []
        return result

    async def _async_update_issue(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_BOARD_ENTITY_ID))
        issue_key = call.data[ATTR_ISSUE_KEY]
        try:
            await coordinator.client.update_issue(
                issue_key,
                call.data[ATTR_SUMMARY],
                description=call.data.get(ATTR_DESCRIPTION),
                priority=call.data.get(ATTR_PRIORITY),
                due_date=call.data.get(ATTR_DUE_DATE),
                assignee_account_id=call.data.get(ATTR_ASSIGNEE_ACCOUNT_ID),
                labels=call.data.get(ATTR_LABELS),
            )
        except JiraApiError as err:
            raise HomeAssistantError(f"Could not update {issue_key}: {err}") from err
        # The card shows "KEY  summary" straight from the coordinator's
        # cached data - without this, a renamed issue would only catch up
        # on the board itself after the next scan_interval poll, even
        # though the popup that just saved it already shows the new text.
        await coordinator.async_request_refresh()

    async def _async_add_comment(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_BOARD_ENTITY_ID))
        issue_key = call.data[ATTR_ISSUE_KEY]
        try:
            await coordinator.client.add_comment(issue_key, call.data[ATTR_COMMENT])
        except JiraApiError as err:
            raise HomeAssistantError(f"Could not comment on {issue_key}: {err}") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ISSUE,
        _async_get_issue,
        schema=GET_ISSUE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_ISSUE, _async_update_issue, schema=UPDATE_ISSUE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_COMMENT, _async_add_comment, schema=ADD_COMMENT_SCHEMA
    )


def _resolve_coordinator(
    hass: HomeAssistant, board_entity_id: str | None
) -> JiraBoardCoordinator:
    """Pick which configured board should serve this call.

    The card always passes its own column entity's ID along, so a second
    board (second config entry, e.g. a different Jira site - see the
    "single site per entry" limitation in the README) routes correctly
    instead of silently hitting whichever entry happens to be first. Falls
    back to the only/first configured board if no entity_id is given
    (covers the common single-board case, and any caller that omits it).
    """
    coordinators = hass.data.get(DOMAIN, {})
    if board_entity_id:
        entity = er.async_get(hass).async_get(board_entity_id)
        if entity and entity.config_entry_id in coordinators:
            return coordinators[entity.config_entry_id]
    if not coordinators:
        raise HomeAssistantError("No Jira Board configured")
    return next(iter(coordinators.values()))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
