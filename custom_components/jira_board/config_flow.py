"""Config flow for the Jira Board integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import JiraApiError, JiraClient
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_DEFAULT_PROJECT,
    CONF_EMAIL,
    CONF_PROJECTS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default="https://kornehl.atlassian.net"): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_API_TOKEN): str,
        vol.Required(CONF_PROJECTS, default="FAM,HA,HUG"): str,
        vol.Required(CONF_DEFAULT_PROJECT, default="HA"): str,
    }
)


class JiraBoardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jira Board."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = JiraClient(
                session,
                user_input[CONF_BASE_URL],
                user_input[CONF_EMAIL],
                user_input[CONF_API_TOKEN],
            )
            try:
                await client.test_auth()
            except JiraApiError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - surfaced to the user as a generic error
                _LOGGER.exception("Unexpected error validating Jira credentials")
                errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_BASE_URL]}::{user_input[CONF_EMAIL]}"
                )
                self._abort_if_unique_id_configured()
                data = dict(user_input)
                data[CONF_PROJECTS] = [
                    p.strip().upper() for p in data[CONF_PROJECTS].split(",") if p.strip()
                ]
                data.setdefault("scan_interval", DEFAULT_SCAN_INTERVAL_SECONDS)
                return self.async_create_entry(title="Jira Board", data=data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
