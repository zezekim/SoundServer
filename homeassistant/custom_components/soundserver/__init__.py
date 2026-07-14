"""SoundServer integration for Home Assistant.

Adds `soundserver.play`, `soundserver.speak`, `soundserver.set_volume`, and
`soundserver.refresh_speakers` services that call a networked SoundServer over
HTTP (https://github.com/zezekim/SoundServer).

Speakers are auto-discovered from the server's `/api/speakers` endpoint, so a
service call may reference a speaker by its **friendly name** (e.g.
"Outdoor (Dev 0)") or its raw id (e.g. "2,0"). The discovered list is also
published as a `sensor.soundserver_speakers` entity.

Configuration (configuration.yaml):

    soundserver:
      url: "http://10.0.14.50/sound"   # base URL of the Sound Server
      default_speaker: "Outdoor"       # optional; name or id used when omitted
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

DOMAIN = "soundserver"
CONF_DEFAULT_SPEAKER = "default_speaker"
DATA_COORDINATOR = "coordinator"
DATA_DEFAULT_SPEAKER = "default_speaker"
PLATFORMS = [Platform.SENSOR]
SCAN_INTERVAL = timedelta(minutes=5)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_URL): cv.url,
                vol.Optional(CONF_DEFAULT_SPEAKER): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PLAY_SCHEMA = vol.Schema(
    {
        vol.Required("sound"): cv.string,
        vol.Optional("speaker"): cv.string,
        vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        vol.Optional("background"): cv.string,
    }
)

SPEAK_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("speaker"): cv.string,
        vol.Optional("lang", default="en"): cv.string,
        vol.Optional("background"): cv.string,
    }
)

SET_VOLUME_SCHEMA = vol.Schema(
    {
        vol.Required("card"): cv.string,
        vol.Optional("control", default="Speaker"): cv.string,
        vol.Required("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)


class SoundServerCoordinator(DataUpdateCoordinator):
    """Polls the Sound Server for its list of speakers."""

    def __init__(self, hass: HomeAssistant, session, base_url: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.session = session
        self.base_url = base_url

    async def _async_update_data(self):
        url = f"{self.base_url}/api/speakers"
        try:
            async with asyncio.timeout(10):
                async with self.session.get(url) as resp:
                    if resp.status >= 400:
                        raise UpdateFailed(f"{resp.status} fetching {url}")
                    data = await resp.json()
        except UpdateFailed:
            raise
        except Exception as err:  # network error, timeout, bad JSON
            raise UpdateFailed(f"Error fetching speakers from {url}: {err}") from err
        # Expected: [{"id": "2,0", "name": "Outdoor (Dev 0)"}, ...]
        return data if isinstance(data, list) else []

    def resolve(self, value: str) -> str:
        """Resolve a speaker name or id to the speaker id the API expects."""
        speakers = self.data or []
        if any(value == s.get("id") for s in speakers):
            return value
        low = str(value).strip().lower()
        for s in speakers:
            if str(s.get("name", "")).strip().lower() == low:
                return s.get("id")
        # Unknown to us — pass through so raw ids still work even if discovery failed.
        return value


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SoundServer services from configuration.yaml."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]
    base_url = conf[CONF_URL].rstrip("/")
    default_speaker = conf.get(CONF_DEFAULT_SPEAKER)
    session = async_get_clientsession(hass)

    coordinator = SoundServerCoordinator(hass, session, base_url)
    await coordinator.async_refresh()  # best-effort first discovery (won't abort setup)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_COORDINATOR] = coordinator
    hass.data[DOMAIN][DATA_DEFAULT_SPEAKER] = default_speaker

    def resolve_speaker(call: ServiceCall) -> str:
        value = call.data.get("speaker") or default_speaker
        if not value:
            raise HomeAssistantError(
                "No 'speaker' provided and no 'default_speaker' configured for soundserver."
            )
        return coordinator.resolve(value)

    async def request(method: str, path: str, **kwargs) -> None:
        url = f"{base_url}{path}"
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise HomeAssistantError(
                        f"SoundServer {method} {path} failed ({resp.status}): {body[:200]}"
                    )
        except HomeAssistantError:
            raise
        except Exception as err:  # network error, timeout, etc.
            raise HomeAssistantError(f"SoundServer request to {url} failed: {err}") from err

    async def handle_play(call: ServiceCall) -> None:
        speaker = resolve_speaker(call)
        path = f"/play/{speaker}/{call.data['sound']}/{call.data['count']}"
        params = {}
        if call.data.get("background"):
            params["background"] = call.data["background"]
        await request("GET", path, params=params)

    async def handle_speak(call: ServiceCall) -> None:
        speaker = resolve_speaker(call)
        payload = {
            "text": call.data["text"],
            "speaker_id": speaker,
            "lang": call.data["lang"],
        }
        if call.data.get("background"):
            payload["background_sound"] = call.data["background"]
        await request("POST", "/api/speak", json=payload)

    async def handle_set_volume(call: ServiceCall) -> None:
        path = f"/api/set_volume/{call.data['card']}/{call.data['control']}/{call.data['volume']}"
        await request("POST", path)

    async def handle_refresh_speakers(call: ServiceCall) -> None:
        await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "play", handle_play, schema=PLAY_SCHEMA)
    hass.services.async_register(DOMAIN, "speak", handle_speak, schema=SPEAK_SCHEMA)
    hass.services.async_register(DOMAIN, "set_volume", handle_set_volume, schema=SET_VOLUME_SCHEMA)
    hass.services.async_register(DOMAIN, "refresh_speakers", handle_refresh_speakers, schema=vol.Schema({}))

    # Publish the discovered speakers as a sensor entity.
    await discovery.async_load_platform(hass, Platform.SENSOR, DOMAIN, {}, config)

    _LOGGER.info(
        "SoundServer ready (url=%s, %d speakers discovered)",
        base_url,
        len(coordinator.data or []),
    )
    return True
