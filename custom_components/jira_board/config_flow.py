"""Config flow for the Jira Board integration.

Three steps: credentials -> pick which projects to track (multi-select,
populated live from the account's actual Jira projects, not free-typed
keys) -> pick which of those is the default for brand-new cards.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
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
    }
)


def _projects_schema(projects: list[dict[str, str]]) -> vol.Schema:
    options = [
        selector.SelectOptionDict(value=p["key"], label=f"{p['key']} — {p['name']}")
        for p in projects
    ]
    return vol.Schema(
        {
            vol.Required(CONF_PROJECTS): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _default_project_schema(chosen_keys: list[str]) -> vol.Schema:
    options = [selector.SelectOptionDict(value=k, label=k) for k in chosen_keys]
    return vol.Schema(
        {
            vol.Required(CONF_DEFAULT_PROJECT, default=chosen_keys[0]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


def _options_schema(
    all_projects: list[dict[str, str]], current_projects: list[str], current_default: str
) -> vol.Schema:
    project_options = [
        selector.SelectOptionDict(value=p["key"], label=f"{p['key']} — {p['name']}")
        for p in all_projects
    ]
    # Default-project options intentionally stay the *full* project list
    # here, not just the currently-tracked ones: HA's selectors aren't
    # reactive to each other within one step, so there's no way to narrow
    # this dropdown live to whatever's mid-edit in the projects field above
    # it. Picking a default outside the submitted projects is instead
    # rejected server-side (see JiraBoardOptionsFlow.async_step_init).
    return vol.Schema(
        {
            vol.Required(CONF_PROJECTS, default=current_projects): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=project_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DEFAULT_PROJECT, default=current_default
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=project_options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


class JiraBoardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jira Board."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._available_projects: list[dict[str, str]] = []

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
                self._available_projects = await client.list_projects()
            except JiraApiError:
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001 - surfaced to the user as a generic error
                _LOGGER.exception("Unexpected error validating Jira credentials")
                errors["base"] = "cannot_connect"

            if not errors and not self._available_projects:
                errors["base"] = "no_projects_found"

            if not errors:
                await self.async_set_unique_id(
                    f"{user_input[CONF_BASE_URL]}::{user_input[CONF_EMAIL]}"
                )
                self._abort_if_unique_id_configured()
                self._data.update(user_input)
                return await self.async_step_projects()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_projects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_PROJECTS):
                errors["base"] = "no_projects_selected"
            else:
                self._data[CONF_PROJECTS] = user_input[CONF_PROJECTS]
                return await self.async_step_default_project()

        return self.async_show_form(
            step_id="projects",
            data_schema=_projects_schema(self._available_projects),
            errors=errors,
        )

    async def async_step_default_project(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_DEFAULT_PROJECT] = user_input[CONF_DEFAULT_PROJECT]
            self._data.setdefault("scan_interval", DEFAULT_SCAN_INTERVAL_SECONDS)
            return self.async_create_entry(title="Jira Board", data=self._data)

        return self.async_show_form(
            step_id="default_project",
            data_schema=_default_project_schema(self._data[CONF_PROJECTS]),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JiraBoardOptionsFlow()


class JiraBoardOptionsFlow(OptionsFlow):
    """Change which projects are tracked / the default project later,
    without removing and re-adding the whole integration. Writes to the
    config entry's `options`, which take precedence over the original
    `data` from initial setup wherever they're read (see __init__.py)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        session = async_get_clientsession(self.hass)
        client = JiraClient(
            session,
            self.config_entry.data[CONF_BASE_URL],
            self.config_entry.data[CONF_EMAIL],
            self.config_entry.data[CONF_API_TOKEN],
        )
        try:
            all_projects = await client.list_projects()
        except JiraApiError:
            return self.async_abort(reason="cannot_connect")

        current_projects = self.config_entry.options.get(
            CONF_PROJECTS, self.config_entry.data[CONF_PROJECTS]
        )
        current_default = self.config_entry.options.get(
            CONF_DEFAULT_PROJECT, self.config_entry.data[CONF_DEFAULT_PROJECT]
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_PROJECTS):
                errors["base"] = "no_projects_selected"
            elif user_input[CONF_DEFAULT_PROJECT] not in user_input[CONF_PROJECTS]:
                errors["base"] = "default_not_in_projects"
            else:
                return self.async_create_entry(data=user_input)
            current_projects = user_input[CONF_PROJECTS]
            current_default = user_input[CONF_DEFAULT_PROJECT]

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(all_projects, current_projects, current_default),
            errors=errors,
        )
