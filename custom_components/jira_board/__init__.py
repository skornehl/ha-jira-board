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

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JiraClient
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_EMAIL,
    CONF_PROJECTS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)
from .coordinator import JiraBoardCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["todo"]

# Bump on every change to www/jira-board-card.js. Appended as a query
# string on the registered URL purely for cache-busting - browsers treat a
# different URL as a different resource, so this is what actually
# guarantees a client picks up a new card version instead of possibly
# serving a stale cached copy despite cache_headers=False below (that flag
# only affects HA's own response headers, not whatever caching heuristics
# the browser decides to apply on its own).
CARD_VERSION = "3"
CARD_URL_PATH = f"/{DOMAIN}_static/jira-board-card.js"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = JiraClient(
        session,
        entry.data[CONF_BASE_URL],
        entry.data[CONF_EMAIL],
        entry.data[CONF_API_TOKEN],
    )
    coordinator = JiraBoardCoordinator(
        hass,
        client,
        entry.data[CONF_PROJECTS],
        entry.data.get("scan_interval", DEFAULT_SCAN_INTERVAL_SECONDS),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_frontend(hass)
    return True


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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
