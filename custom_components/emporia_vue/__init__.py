"""The Emporia Vue integration."""

import asyncio
from collections.abc import Mapping
from functools import partial
import logging
import re
from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import ChargerDevice, VueDevice
import requests
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    AUTH_METHOD,
    AUTH_METHOD_EMAIL_PASSWORD,
    AUTH_METHOD_TOKENS,
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONFIG_FLOW_SCHEMA,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    SOLAR_INVERT,
)
from .coordinator import (
    VueDayCoordinator,
    VueDeviceStatusCoordinator,
    VueMinuteCoordinator,
    VueMonthCoordinator,
    VueRuntimeData,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "switch", "number"]
SENSITIVE_CONFIG_KEYS = {
    CONF_PASSWORD,
    CONF_ID_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
}
SET_CHARGER_CURRENT_SERVICE = "set_charger_current"

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: CONFIG_FLOW_SCHEMA},
    extra=vol.ALLOW_EXTRA,
)

EmporiaVueConfigEntry = ConfigEntry[VueRuntimeData]


def redact_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data with sensitive auth values hidden for logging."""
    return {
        key: "***" if key in SENSITIVE_CONFIG_KEYS else value
        for key, value in data.items()
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Emporia Vue component."""
    conf = config.get(DOMAIN)
    if not conf:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                CONF_EMAIL: conf[CONF_EMAIL],
                CONF_PASSWORD: conf[CONF_PASSWORD],
                ENABLE_1M: conf[ENABLE_1M],
                ENABLE_1D: conf[ENABLE_1D],
                ENABLE_1MON: conf[ENABLE_1MON],
                SOLAR_INVERT: conf[SOLAR_INVERT],
            },
        )
    )
    return True


async def async_login_vue(
    loop: asyncio.AbstractEventLoop,
    vue: PyEmVue,
    entry_data: Mapping[str, Any],
) -> bool:
    """Log in to Emporia using the configured authentication method."""
    auth_method = entry_data.get(AUTH_METHOD, AUTH_METHOD_EMAIL_PASSWORD)
    if auth_method == AUTH_METHOD_TOKENS:
        return await loop.run_in_executor(
            None,
            partial(
                vue.login,
                id_token=entry_data[CONF_ID_TOKEN],
                access_token=entry_data[CONF_ACCESS_TOKEN],
                refresh_token=entry_data[CONF_REFRESH_TOKEN],
            ),
        )

    email: str = entry_data[CONF_EMAIL]
    password: str = entry_data[CONF_PASSWORD]
    if email.startswith("vue_simulator@"):
        host = email.split("@")[1]
        return await loop.run_in_executor(None, vue.login_simulator, host)
    return await loop.run_in_executor(
        None,
        partial(vue.login, username=email, password=password),
    )


async def async_setup_entry(hass: HomeAssistant, entry: EmporiaVueConfigEntry) -> bool:
    """Set up Emporia Vue from a config entry."""
    entry_data = entry.data
    _LOGGER.debug(
        "Setting up Emporia Vue with entry data: %s",
        redact_config_data(entry_data),
    )
    vue = PyEmVue()
    runtime = VueRuntimeData(vue=vue, invert_solar=entry_data.get(SOLAR_INVERT, True))
    entry.runtime_data = runtime

    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    try:
        result: bool = await async_login_vue(loop, vue, entry_data)
        if not result:
            _LOGGER.error("Failed to login to Emporia Vue")
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            )
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # pylint: disable=broad-exception-caught
        _LOGGER.error("Failed to login to Emporia Vue: %s", err)
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err

    if entry_data.get(AUTH_METHOD) == AUTH_METHOD_TOKENS and vue.auth and vue.auth.tokens:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ID_TOKEN: vue.auth.tokens["id_token"],
                CONF_ACCESS_TOKEN: vue.auth.tokens["access_token"],
                CONF_REFRESH_TOKEN: vue.auth.tokens["refresh_token"],
            },
        )

        def _token_updater(tokens: dict[str, Any]) -> None:
            """Persist tokens refreshed mid-session back to the config entry."""
            hass.loop.call_soon_threadsafe(
                lambda: hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_ID_TOKEN: tokens["id_token"],
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    },
                )
            )

        vue.auth.token_updater = _token_updater

    try:
        devices: list[VueDevice] = await loop.run_in_executor(None, vue.get_devices)
        for device in devices:
            if str(device.device_gid) not in runtime.device_gids:
                runtime.device_gids.append(str(device.device_gid))
                _LOGGER.info("Adding gid %s to device_gids list", device.device_gid)
                runtime.device_information[device.device_gid] = device
            else:
                runtime.device_information[device.device_gid].channels += device.channels

        total_channels = 0
        for device in runtime.device_information.values():
            total_channels += len(device.channels)
        _LOGGER.info(
            "Found %s Emporia devices with %s total channels",
            len(runtime.device_information.keys()),
            total_channels,
        )

        runtime.coordinator_1min = VueMinuteCoordinator(hass, runtime)
        await runtime.coordinator_1min.async_config_entry_first_refresh()
        _LOGGER.debug("1min Update data: %s", runtime.coordinator_1min.data)

        runtime.coordinator_1mon = VueMonthCoordinator(hass, runtime)
        await runtime.coordinator_1mon.async_config_entry_first_refresh()
        _LOGGER.debug("1mon Update data: %s", runtime.coordinator_1mon.data)

        runtime.coordinator_day_sensor = VueDayCoordinator(hass, runtime)
        await runtime.coordinator_day_sensor.async_config_entry_first_refresh()

        has_controllable_devices = any(
            device.outlet or device.ev_charger
            for device in runtime.device_information.values()
        )

        if has_controllable_devices:
            runtime.coordinator_device_status = VueDeviceStatusCoordinator(hass, vue)
            await runtime.coordinator_device_status.async_config_entry_first_refresh()

        async def handle_set_charger_current(call) -> None:
            """Handle setting the EV Charger current."""
            _LOGGER.debug(
                "executing set_charger_current: %s %s",
                str(call.service),
                str(call.data),
            )
            current = call.data.get("current")
            current = int(current)
            device_id: str | list[str] | None = call.data.get("device_id", None)
            entity_id: str | list[str] | None = call.data.get("entity_id", None)

            if isinstance(device_id, str):
                device_id = [device_id]
            if isinstance(entity_id, str):
                entity_id = [entity_id]

            charger_entity: er.RegistryEntry | None = None
            entity_registry: er.EntityRegistry = er.async_get(hass)
            if device_id:
                entities: list[er.RegistryEntry] = er.async_entries_for_device(
                    entity_registry, device_id[0]
                )
                for entity in entities:
                    _LOGGER.info("Entity is %s", str(entity))
                    if entity.entity_id.startswith("switch"):
                        charger_entity = entity
                        break
                if not charger_entity and entities:
                    charger_entity = entities[0]
            elif entity_id:
                charger_entity = entity_registry.async_get(entity_id[0])
            if not charger_entity:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="target_required"
                )

            unique_entity_id: str = charger_entity.unique_id
            gid_match: re.Match[str] | None = re.search(r"\d+", unique_entity_id)
            if not gid_match:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="charger_gid_not_found",
                    translation_placeholders={"unique_id": unique_entity_id},
                )

            charger_gid = int(gid_match.group(0))
            if (
                charger_gid not in runtime.device_information
                or not runtime.device_information[charger_gid].ev_charger
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_charger_target",
                    translation_placeholders={
                        "entity_id": charger_entity.entity_id,
                        "unique_id": unique_entity_id,
                    },
                )

            state = hass.states.get(charger_entity.entity_id)
            _LOGGER.info("State is %s", str(state))
            if not state:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="charger_state_not_found",
                    translation_placeholders={"entity_id": charger_entity.entity_id},
                )
            charger_info: VueDevice = runtime.device_information[charger_gid]
            if charger_info.ev_charger is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="charger_info_not_found",
                    translation_placeholders={"charger_gid": str(charger_gid)},
                )
            current: int = max(6, current)
            current = min(current, charger_info.ev_charger.max_charging_rate)
            _LOGGER.info(
                "Setting charger %s to current of %d amps", charger_gid, current
            )

            try:
                updated_charger: ChargerDevice = await loop.run_in_executor(
                    None,
                    vue.update_charger,
                    charger_info.ev_charger,
                    state.state == "on",
                    current,
                )
                runtime.device_information[charger_gid].ev_charger = updated_charger
                state: State | None = hass.states.get(charger_entity.entity_id)
                if state:
                    new_state: str = "on" if updated_charger.charger_on else "off"
                    new_attributes: dict = state.attributes.copy()
                    new_attributes["charging_rate"] = updated_charger.charging_rate
                    hass.states.async_set(
                        charger_entity.entity_id, new_state, new_attributes
                    )

            except requests.exceptions.HTTPError as err:
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

        hass.services.async_register(
            DOMAIN, SET_CHARGER_CURRENT_SERVICE, handle_set_charger_current
        )

    except Exception as err:
        _LOGGER.warning("Exception while setting up Emporia Vue. Will retry. %s", err)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as err:
        _LOGGER.warning("Error setting up platforms: %s", err)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="platform_setup_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EmporiaVueConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok: bool = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.services.async_remove(DOMAIN, SET_CHARGER_CURRENT_SERVICE)

    return unload_ok
