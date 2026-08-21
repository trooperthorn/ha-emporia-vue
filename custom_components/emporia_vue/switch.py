"""Platform for switch integration."""

import logging
from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import VueDevice
from requests import exceptions

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .charger_entity import EmporiaChargerEntity
from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__name__)

# These entities issue direct writes to the Emporia API (outlet/charger
# on-off). Limit to one in-flight write at a time so two near-simultaneous
# toggles can't race each other against the API.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    runtime = config_entry.runtime_data
    vue: PyEmVue = runtime.vue
    coordinator: DataUpdateCoordinator | None = runtime.coordinator_device_status
    device_information: dict[int, VueDevice] = runtime.device_information

    if coordinator is None or coordinator.data is None:
        return

    switches = []
    for gid in coordinator.data:
        int_gid = int(gid)
        if int_gid not in device_information:
            continue
        device = device_information[int_gid]
        if device.outlet:
            switches.append(EmporiaOutletSwitch(coordinator, vue, gid, device))
        elif device.ev_charger:
            switches.append(
                EmporiaChargerSwitch(
                    coordinator,
                    vue,
                    device,
                    None,
                    SwitchDeviceClass.OUTLET,
                )
            )

    async_add_entities(switches)


class EmporiaOutletSwitch(CoordinatorEntity, SwitchEntity):  # type: ignore
    """Representation of an Emporia Smart Outlet state."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        vue: PyEmVue,
        gid: str,
        device: VueDevice,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._vue = vue
        self._device_gid = gid
        self._device: VueDevice = device
        self._attr_has_entity_name = True
        self._attr_name = None
        self._attr_device_class = SwitchDeviceClass.OUTLET

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.hass.async_add_executor_job(
            self._vue.update_outlet, self.coordinator.data[self._device_gid], True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.hass.async_add_executor_job(
            self._vue.update_outlet,
            self.coordinator.data[self._device_gid],
            False,
        )
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_gid}-1,2,3")},
            name=self._device.device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].outlet_on

    @property
    def unique_id(self) -> str:
        """Unique ID for the switch."""
        return f"switch.emporia_vue.{self._device_gid}"


class EmporiaChargerSwitch(EmporiaChargerEntity, SwitchEntity):  # type: ignore
    """Representation of an Emporia Charger switch state."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the charger on."""
        await self._update_switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the charger off."""
        await self._update_switch(False)

    @property
    def is_on(self) -> bool:
        """Return the state of the switch."""
        return self.coordinator.data[self._device_gid].charger_on

    async def _update_switch(self, on: bool) -> None:
        """Update the switch, preserving the currently-set charging current."""
        charger = self.coordinator.data[self._device_gid]
        try:
            await self.hass.async_add_executor_job(
                self._vue.update_charger,
                charger,
                on,
                charger.charging_rate,
            )
        except exceptions.HTTPError as err:
            _LOGGER.error(
                "Error updating charger status: %s \nResponse body: %s",
                err,
                err.response.text,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="charger_update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
