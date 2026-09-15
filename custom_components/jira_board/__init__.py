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
ATTR_ISSUE_KEY = "issue_key"
ATTR_BOARD_ENTITY_ID = "board_entity_id"
GET_ISSUE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ISSUE_KEY): cv.string,
        vol.Optional(ATTR_BOARD_ENTITY_ID): cv.entity_id,
    }
)

# Bump on every change to www/jira-board-card.js. Appended as a query
# string on the registered URL purely for cache-busting - browsers treat a
# different URL as a different resource, so this is what actually
# guarantees a client picks up a new card version instead of possibly
# serving a stale cached copy despite cache_headers=False below (that flag
# only affects HA's own response headers, not whatever caching heuristics
# the browser decides to apply on its own).
CARD_VERSION = "16"
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
    """Register the `get_issue` service backing the card's detail popup.

    Domain-level (not per-entry/per-entity) and registered once, same
    idempotency reasoning as _async_register_frontend - a second
    account/board must not try to register it twice.
    """
    if hass.services.has_service(DOMAIN, SERVICE_GET_ISSUE):
        return

    async def _async_get_issue(call: ServiceCall) -> ServiceResponse:
        client = _resolve_client(hass, call.data.get(ATTR_BOARD_ENTITY_ID))
        issue_key = call.data[ATTR_ISSUE_KEY]
        try:
            issue = await client.get_issue(issue_key)
        except JiraApiError as err:
            raise HomeAssistantError(f"Could not fetch {issue_key}: {err}") from err
        return format_issue_for_card(issue, client.base_url)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ISSUE,
        _async_get_issue,
        schema=GET_ISSUE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _resolve_client(hass: HomeAssistant, board_entity_id: str | None) -> JiraClient:
    """Pick which configured board's Jira client should serve this call.

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
            return coordinators[entity.config_entry_id].client
    if not coordinators:
        raise HomeAssistantError("No Jira Board configured")
    return next(iter(coordinators.values())).client


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
