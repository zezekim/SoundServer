"""Each SoundServer speaker as a media_player entity."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN
from .coordinator import SoundServerCoordinator

SUPPORT = (
    MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.VOLUME_SET
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create a media_player per discovered speaker; add new ones as they appear."""
    coordinator: SoundServerCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def _add_new_speakers() -> None:
        new = []
        for speaker in coordinator.data or []:
            sid = speaker.get("id")
            if sid and sid not in known:
                known.add(sid)
                new.append(SoundServerSpeaker(coordinator, entry, sid, speaker.get("name", sid)))
        if new:
            async_add_entities(new)

    _add_new_speakers()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_speakers))

    # Entity services: `soundserver.play` / `soundserver.speak` with a speaker target.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "play",
        {
            vol.Required("sound"): cv.string,
            vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional("background"): cv.string,
        },
        "async_play_sound",
    )
    platform.async_register_entity_service(
        "speak",
        {
            vol.Required("text"): cv.string,
            vol.Optional("lang", default="en"): cv.string,
            vol.Optional("background"): cv.string,
        },
        "async_speak",
    )


class SoundServerSpeaker(CoordinatorEntity[SoundServerCoordinator], MediaPlayerEntity):
    """A single SoundServer speaker."""

    _attr_has_entity_name = False
    _attr_supported_features = SUPPORT
    _attr_media_content_type = MediaType.MUSIC

    def __init__(
        self,
        coordinator: SoundServerCoordinator,
        entry: ConfigEntry,
        speaker_id: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._speaker_id = speaker_id
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}-{speaker_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or DEFAULT_NAME,
            manufacturer="SoundServer",
        )
        self._attr_state = MediaPlayerState.IDLE

    @property
    def _api(self):
        return self.coordinator.api

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and any(
            s.get("id") == self._speaker_id for s in (self.coordinator.data or [])
        )

    # --- native media_player actions ---
    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        """Play a library sound (media_id is the .wav filename from the browser)."""
        await self._api.play(self._speaker_id, media_id)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume; the card index is the part before the comma in the speaker id."""
        card = self._speaker_id.split(",")[0]
        await self._api.set_volume(card, "Speaker", round(volume * 100))
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_browse_media(
        self, media_content_type: str | None = None, media_content_id: str | None = None
    ) -> BrowseMedia:
        """Expose the sound library so it's pickable from the media browser."""
        sounds = await self._api.get_sounds()
        children = [
            BrowseMedia(
                title=sound,
                media_class=MediaClass.MUSIC,
                media_content_type=MediaType.MUSIC,
                media_content_id=sound,
                can_play=True,
                can_expand=False,
            )
            for sound in sounds
        ]
        return BrowseMedia(
            title="SoundServer library",
            media_class=MediaClass.DIRECTORY,
            media_content_type="library",
            media_content_id="root",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.MUSIC,
        )

    # --- custom entity services ---
    async def async_play_sound(self, sound: str, count: int = 1, background: str | None = None) -> None:
        await self._api.play(self._speaker_id, sound, count=count, background=background)

    async def async_speak(self, text: str, lang: str = "en", background: str | None = None) -> None:
        await self._api.speak(self._speaker_id, text, lang=lang, background=background)
