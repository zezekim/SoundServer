"""SoundServer integration for Home Assistant.

Set up from the UI (Settings → Devices & Services → Add Integration → SoundServer).
Each discovered speaker becomes a `media_player` entity you can target from
dropdowns, with the sound library available through the media browser.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .coordinator import SoundServerApi, SoundServerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SoundServer from a config entry."""
    session = async_get_clientsession(hass)
    api = SoundServerApi(session, entry.data[CONF_URL])
    coordinator = SoundServerCoordinator(hass, api)

    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady(f"Cannot reach SoundServer at {api.base_url}")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Domain-level service to re-discover speakers on demand (refreshes every entry).
    if not hass.services.has_service(DOMAIN, "refresh_speakers"):
        async def _refresh_speakers(_call: ServiceCall) -> None:
            for coord in hass.data[DOMAIN].values():
                await coord.async_request_refresh()

        hass.services.async_register(DOMAIN, "refresh_speakers", _refresh_speakers)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "refresh_speakers")
    return unloaded
