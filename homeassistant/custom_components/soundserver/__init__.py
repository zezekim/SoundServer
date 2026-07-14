"""SoundServer integration for Home Assistant.

Adds `soundserver.play`, `soundserver.speak`, and `soundserver.set_volume`
services that call a networked SoundServer over HTTP
(https://github.com/zezekim/SoundServer).

Configuration (configuration.yaml):

    soundserver:
      url: "http://10.0.14.50/sound"   # base URL of the Sound Server
      default_speaker: "2,0"           # optional; used when a call omits `speaker`

`url` should point at the Sound Server itself — either through the Caddy portal
(`http://<pi>/sound`) or directly (`http://<pi>:5000`).
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "soundserver"
CONF_DEFAULT_SPEAKER = "default_speaker"

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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SoundServer services from configuration.yaml."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]
    base_url = conf[CONF_URL].rstrip("/")
    default_speaker = conf.get(CONF_DEFAULT_SPEAKER)
    session = async_get_clientsession(hass)

    def resolve_speaker(call: ServiceCall) -> str:
        speaker = call.data.get("speaker") or default_speaker
        if not speaker:
            raise HomeAssistantError(
                "No 'speaker' provided and no 'default_speaker' configured for soundserver."
            )
        return speaker

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

    hass.services.async_register(DOMAIN, "play", handle_play, schema=PLAY_SCHEMA)
    hass.services.async_register(DOMAIN, "speak", handle_speak, schema=SPEAK_SCHEMA)
    hass.services.async_register(DOMAIN, "set_volume", handle_set_volume, schema=SET_VOLUME_SCHEMA)

    _LOGGER.info("SoundServer integration ready (url=%s)", base_url)
    return True
