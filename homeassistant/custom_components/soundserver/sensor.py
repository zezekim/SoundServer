"""Sensor exposing the SoundServer's auto-discovered speakers."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DATA_COORDINATOR, DOMAIN


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the speakers sensor (loaded via discovery from __init__)."""
    if discovery_info is None:
        return
    coordinator = hass.data[DOMAIN][DATA_COORDINATOR]
    async_add_entities([SoundServerSpeakersSensor(coordinator)])


class SoundServerSpeakersSensor(CoordinatorEntity, SensorEntity):
    """Number of discovered speakers; the list is in the attributes."""

    _attr_name = "SoundServer Speakers"
    _attr_unique_id = "soundserver_speakers"
    _attr_icon = "mdi:speaker-multiple"
    _attr_native_unit_of_measurement = "speakers"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict:
        speakers = self.coordinator.data or []
        return {
            "speakers": speakers,
            "ids": [s.get("id") for s in speakers],
            "names": [s.get("name") for s in speakers],
        }
