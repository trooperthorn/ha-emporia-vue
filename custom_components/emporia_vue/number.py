"""Platform for number integration."""

import logging
from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import VueDevice

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.exceptions import HomeAssistantError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .charger_entity import EmporiaChargerEntity
from .const import DOMAIN, VUE_DATA

_LOGGER: logging.Logger = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    vue: PyEmVue = hass.data[DOMAIN][config_entry.entry_id][VUE_DATA]
    coordinator: DataUpdateCoordinator | None = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator_device_status"
    ]
    device_information: dict[int, VueDevice] = hass.data[DOMAIN][config_entry.entry_id][
        "device_information"
    ]

    if coordinator is None or coordinator.data is None:
        return

    entities = []
    for gid in coordinator.data:
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.ev_charger:
            entities.append(
                EmporiaChargerCurrentNumber(coordinator, vue, device)
            )

    async_add_entities(entities)


class EmporiaChargerCurrentNumber(EmporiaChargerEntity, NumberEntity):  # type: ignore
    """Representation of the Emporia EV Charger current limit."""

    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:current-ac"
    _attr_name = "Current Limit"
    _attr_translation_key = "charge_current_limit"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        device: VueDevice,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(
            coordinator,
            vue,
            device,
            UnitOfElectricCurrent.AMPERE,
            NumberDeviceClass.CURRENT,
        )
        self._attr_native_min_value = 6.0
        self._attr_native_max_value = float(device.ev_charger.max_charging_rate)
        self._attr_native_step = 1.0

        # Optimistic state to prevent the slider from snapping back while
        # waiting for the API/coordinator to catch up.
        self._optimistic_value: float | None = None

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger current limit number.

        Overrides EmporiaChargerEntity.unique_id (which is shared across all
        charger entity types) so this entity has its own distinct ID rather
        than colliding conceptually with the charger switch's unique_id.
        """
        return f"number.emporia_vue.charger_current_{self._device_gid}"

    @property
    def native_value(self) -> float | None:
        """Return the current charging rate."""
        if self._optimistic_value is not None:
            return self._optimistic_value

        data = self.coordinator.data.get(self._device_gid)
        if data:
            return float(data.charging_rate)
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the charger current limit with optimistic UI response."""
        current = int(value)
        current = max(6, min(current, int(self._attr_native_max_value)))

        previous_value = self.native_value

        self._optimistic_value = float(current)
        self.async_write_ha_state()

        try:
            await self.hass.async_add_executor_job(
                self._vue.update_charger,
                self.coordinator.data[self._device_gid],
                self.coordinator.data[self._device_gid].charger_on,
                current,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            self._optimistic_value = previous_value
            self.async_write_ha_state()
            _LOGGER.error("Error updating charger current: %s", err)
            raise HomeAssistantError(
                f"Failed to set Emporia charger current: {err}"
            ) from err
        else:
            # Only clear the optimistic override once the coordinator's data
            # actually agrees with what we set — Emporia's API can be
            # eventually-consistent, so a refresh right after a successful
            # write may still briefly report the old value.
            data = self.coordinator.data.get(self._device_gid)
            if data and float(data.charging_rate) == self._optimistic_value:
                self._optimistic_value = None
            self.async_write_ha_state()
