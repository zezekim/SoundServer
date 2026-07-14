"""Config flow — add SoundServer from the Home Assistant UI."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_NAME, DOMAIN


class SoundServerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SoundServer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            try:
                async with asyncio.timeout(10):
                    async with session.get(f"{url}/api/speakers") as resp:
                        ok = resp.status < 400
            except Exception:  # noqa: BLE001 - any failure means "can't connect"
                ok = False

            if ok:
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                    data={CONF_URL: url, CONF_NAME: user_input.get(CONF_NAME, DEFAULT_NAME)},
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default="http://homeassistant.local/sound"): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
