"""HTTP client + speaker-discovery coordinator for SoundServer."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiohttp import ClientSession

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SCAN_INTERVAL_MINUTES

_LOGGER = logging.getLogger(__name__)


class SoundServerApi:
    """Thin async wrapper over the SoundServer HTTP API."""

    def __init__(self, session: ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        async with asyncio.timeout(15):
            async with self._session.request(method, url, **kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(f"{method} {path} -> {resp.status}: {body[:200]}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()

    async def get_speakers(self) -> list[dict]:
        data = await self._request("GET", "/api/speakers")
        return data if isinstance(data, list) else []

    async def get_sounds(self) -> list[str]:
        data = await self._request("GET", "/api/sounds")
        return data if isinstance(data, list) else []

    async def play(self, speaker: str, sound: str, count: int = 1, background: str | None = None) -> None:
        params = {"background": background} if background else None
        await self._request("GET", f"/play/{speaker}/{sound}/{count}", params=params)

    async def speak(self, speaker: str, text: str, lang: str = "en", background: str | None = None) -> None:
        payload = {"text": text, "speaker_id": speaker, "lang": lang}
        if background:
            payload["background_sound"] = background
        await self._request("POST", "/api/speak", json=payload)

    async def set_volume(self, card: str, control: str, percent: int) -> None:
        await self._request("POST", f"/api/set_volume/{card}/{control}/{percent}")


class SoundServerCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls the Sound Server for its list of speakers."""

    def __init__(self, hass: HomeAssistant, api: SoundServerApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=SCAN_INTERVAL_MINUTES),
        )
        self.api = api

    async def _async_update_data(self) -> list[dict]:
        try:
            return await self.api.get_speakers()
        except Exception as err:  # network, timeout, bad payload
            raise UpdateFailed(f"Error fetching speakers: {err}") from err
