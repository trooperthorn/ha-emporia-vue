"""The Emporia Vue integration."""

import asyncio
import calendar
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from functools import partial
import logging
import re
from typing import Any

import dateutil.relativedelta
import dateutil.tz
from pyemvue import PyEmVue
from pyemvue.device import (
    ChargerDevice,
    OutletDevice,
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale
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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    MAINS_COMBINED_CHANNEL_NUM,
    MAINS_SPLIT_CHANNEL_EXPORT,
    MAINS_SPLIT_CHANNEL_IMPORT,
    MAINS_SPLIT_CHANNELS,
    SOLAR_INVERT,
    VUE_DATA,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "switch", "number"]
SENSITIVE_CONFIG_KEYS = {
    CONF_PASSWORD,
    CONF_ID_TOKEN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
}

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: CONFIG_FLOW_SCHEMA},
    extra=vol.ALLOW_EXTRA,
)

@dataclass
class VueRuntimeData:
    """Mutable state for a single Emporia Vue config entry.

    Deliberately instance-scoped (one per config entry, held on
    entry.runtime_data) rather than module-level globals: module globals
    would leak/collide if a second config entry were ever added, which is
    also why every helper below that used to read `global DEVICE_GIDS` etc.
    is now a method on this class instead.
    """

    vue: PyEmVue
    device_gids: list[str] = field(default_factory=list)
    device_information: dict[int, VueDevice] = field(default_factory=dict)
    last_minute_data: dict[str, Any] = field(default_factory=dict)
    last_day_data: dict[str, Any] = field(default_factory=dict)
    last_day_update: datetime | None = None
    last_month_data: dict[str, Any] = field(default_factory=dict)
    last_month_update: datetime | None = None
    invert_solar: bool = True

    async def update_sensors(self, scales: list[str]) -> dict:
        """Fetch data from API endpoint."""
        try:
            data: dict = {}
            loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
            for scale in scales:
                utcnow: datetime = datetime.now(UTC)
                usage_dict: dict[int, VueUsageDevice] = await loop.run_in_executor(
                    None, self.vue.get_device_list_usage, self.device_gids, utcnow, scale
                )
                if not usage_dict:
                    _LOGGER.warning(
                        "No channels found during update for scale %s. Retrying", scale
                    )
                    usage_dict = await loop.run_in_executor(
                        None,
                        self.vue.get_device_list_usage,
                        self.device_gids,
                        utcnow,
                        scale,
                    )
                if usage_dict:
                    flattened, data_time = flatten_usage_data(usage_dict, scale)
                    await self.parse_flattened_usage_data(
                        flattened,
                        scale,
                        data,
                        utcnow,
                        data_time,
                    )
                else:
                    raise UpdateFailed(f"No channels found during update for scale {scale}")

            return data
        except Exception as err:
            _LOGGER.error("Error communicating with Emporia API: %s", err)
            raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err

    async def parse_flattened_usage_data(
        self,
        flattened_data: dict[str, VueDeviceChannelUsage],
        scale: str,
        data: dict[str, Any],
        requested_time: datetime,
        data_time: datetime,
    ) -> None:
        """Loop through the device list and find the corresponding update data."""
        unused_data: dict[str, VueDeviceChannelUsage] = flattened_data.copy()
        for gid, info in self.device_information.items():
            local_time: datetime = await change_time_to_local(data_time, info.time_zone)
            requested_time_local: datetime = await change_time_to_local(
                requested_time, info.time_zone
            )
            if abs((local_time - requested_time_local).total_seconds()) > 30:
                _LOGGER.warning(
                    "More than 30 seconds have passed between the requested datetime"
                    " and the returned datetime. Requested: %s Returned: %s",
                    requested_time,
                    data_time,
                )
            for info_channel in info.channels:
                identifier: str = make_channel_id(info_channel, scale)
                channel_num = info_channel.channel_num
                channel: VueDeviceChannelUsage | None = flattened_data.get(identifier)
                if not channel:
                    _LOGGER.info(
                        "Could not find usage info for device %s channel %s",
                        gid,
                        channel_num,
                    )
                unused_data.pop(identifier, None)
                reset_datetime: datetime | None = None

                if scale in [Scale.DAY.value, Scale.MONTH.value]:
                    reset_datetime = determine_reset_datetime(
                        local_time,
                        info.billing_cycle_start_day,
                        scale == Scale.MONTH.value,
                    )

                fixed_usage: float = channel.usage if channel else 0.0
                if fixed_usage is None:
                    fixed_usage = self.handle_none_usage(scale, identifier)
                    _LOGGER.info(
                        "Got None usage for device %s channel %s scale %s and timestamp %s. "
                        "Instead using a value of %s",
                        gid,
                        channel_num,
                        scale,
                        local_time.isoformat(),
                        fixed_usage,
                    )

                bidirectional = "bidirectional" in info_channel.type.lower()
                is_solar = info_channel.channel_type_gid == 13
                fixed_usage = fix_usage_sign(
                    channel_num, fixed_usage, bidirectional, is_solar, self.invert_solar
                )

                data[identifier] = {
                    "device_gid": gid,
                    "channel_num": channel_num,
                    "usage": fixed_usage,
                    "scale": scale,
                    "info": info,
                    "reset": reset_datetime,
                    "timestamp": local_time,
                }
        if unused_data:
            _LOGGER.info(
                "Unused data found during update. Unused data: %s",
                str(unused_data),
            )
            channels_were_added = False
            for channel in unused_data.values():
                channels_were_added |= await self.handle_special_channels_for_device(channel)
            if channels_were_added:
                _LOGGER.info("Rerunning update due to added channels")
                await self.parse_flattened_usage_data(
                    flattened_data, scale, data, requested_time, data_time
                )

    async def handle_special_channels_for_device(self, channel: VueDeviceChannel) -> bool:
        """Handle the special channels for a device, if they exist."""
        if channel.device_gid in self.device_information:
            device_info: VueDevice = self.device_information[channel.device_gid]
            found = False
            channel_123: VueDeviceChannel | None = None
            for device_channel in device_info.channels:
                if device_channel.channel_num == channel.channel_num:
                    found = True
                    break
                if device_channel.channel_num == "1,2,3":
                    channel_123 = device_channel
            if not found:
                _LOGGER.info(
                    "Adding channel for channel %s-%s",
                    channel.device_gid,
                    channel.channel_num,
                )
                multiplier = 1.0
                type_gid = 1
                if channel_123:
                    multiplier = channel_123.channel_multiplier
                    type_gid = channel_123.channel_type_gid

                device_info.channels.append(
                    VueDeviceChannel(
                        gid=channel.device_gid,
                        name=channel.name,
                        channelNum=channel.channel_num,
                        channelMultiplier=multiplier,
                        channelTypeGid=type_gid,
                    )
                )

                return True
        return False

    def handle_none_usage(self, scale: str, identifier: str):
        """Handle the case of the usage being None by using the previous value or zero."""
        if (
            scale is Scale.MINUTE.value
            and identifier in self.last_minute_data
            and "usage" in self.last_minute_data[identifier]
        ):
            return self.last_minute_data[identifier]["usage"]
        if (
            scale is Scale.DAY.value
            and identifier in self.last_day_data
            and "usage" in self.last_day_data[identifier]
        ):
            return self.last_day_data[identifier]["usage"]
        return 0

    async def check_for_midnight(
        self, timestamp: datetime, device_gid: int, day_id: str, data_dict: dict[str, Any]
    ) -> None:
        """If midnight has recently passed, reset data_dict[day_id]'s usage to zero.

        data_dict is passed explicitly so this works for last_day_data as well
        as the derived Mains Import/Export accumulation.
        """
        if device_gid in self.device_information:
            device_info: VueDevice = self.device_information[device_gid]
            local_time: datetime = await change_time_to_local(
                timestamp, device_info.time_zone
            )
            local_midnight: datetime = local_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            last_reset = data_dict[day_id]["reset"]
            if last_reset is None or local_midnight > last_reset:
                _LOGGER.info(
                    "Midnight happened recently for id %s! Timestamp is %s, midnight is %s, "
                    "previous reset was %s",
                    day_id,
                    local_time,
                    local_midnight,
                    last_reset,
                )
                data_dict[day_id]["usage"] = 0
                data_dict[day_id]["reset"] = local_midnight

    async def check_for_new_month(
        self, timestamp: datetime, device_gid: int, month_id: str, data_dict: dict[str, Any]
    ) -> None:
        """If a new billing cycle has started, reset data_dict[month_id]'s usage to zero."""
        if device_gid in self.device_information:
            device_info: VueDevice = self.device_information[device_gid]
            local_time: datetime = await change_time_to_local(
                timestamp, device_info.time_zone
            )
            current_reset: datetime = determine_reset_datetime(
                local_time,
                device_info.billing_cycle_start_day,
                True,
            )
            last_reset = data_dict[month_id]["reset"]
            if last_reset is None or current_reset > last_reset:
                _LOGGER.info(
                    "New billing cycle started for id %s! Timestamp is %s, "
                    "current reset is %s, previous reset was %s",
                    month_id,
                    local_time,
                    current_reset,
                    last_reset,
                )
                data_dict[month_id]["usage"] = 0
                data_dict[month_id]["reset"] = current_reset

    async def integrate_mains_split(
        self,
        device_gid: str,
        minute_entry: dict[str, Any],
        target: dict[str, Any],
        is_month: bool,
    ) -> None:
        """Accumulate one minute's combined-mains usage into Import/Export totals in `target`."""
        usage = minute_entry.get("usage")
        if usage is None:
            return

        scale = Scale.MONTH.value if is_month else Scale.DAY.value
        import_id = f"{device_gid}-{MAINS_SPLIT_CHANNEL_IMPORT}-{scale}"
        export_id = f"{device_gid}-{MAINS_SPLIT_CHANNEL_EXPORT}-{scale}"
        timestamp: datetime = minute_entry["timestamp"]

        for key, channel_num in (
            (import_id, MAINS_SPLIT_CHANNEL_IMPORT),
            (export_id, MAINS_SPLIT_CHANNEL_EXPORT),
        ):
            if key not in target or not target[key]:
                target[key] = {
                    "device_gid": int(device_gid),
                    "channel_num": channel_num,
                    "usage": 0.0,
                    "scale": scale,
                    "info": self.device_information.get(int(device_gid)),
                    "reset": None,
                    "timestamp": timestamp,
                }

        if is_month:
            await self.check_for_new_month(timestamp, int(device_gid), import_id, target)
            await self.check_for_new_month(timestamp, int(device_gid), export_id, target)
        else:
            await self.check_for_midnight(timestamp, int(device_gid), import_id, target)
            await self.check_for_midnight(timestamp, int(device_gid), export_id, target)

        target[import_id]["timestamp"] = timestamp
        target[export_id]["timestamp"] = timestamp

        if usage > 0:
            target[import_id]["usage"] += usage
        elif usage < 0:
            target[export_id]["usage"] += abs(usage)


def redact_config_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config data with sensitive auth values hidden for logging."""
    return {
        key: "***" if key in SENSITIVE_CONFIG_KEYS else value
        for key, value in data.items()
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Emporia Vue component."""
    hass.data.setdefault(DOMAIN, {})
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
            raise ConfigEntryAuthFailed("Failed to login to Emporia Vue")
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # pylint: disable=broad-exception-caught
        _LOGGER.error("Failed to login to Emporia Vue: %s", err)
        raise ConfigEntryAuthFailed("Failed to login to Emporia Vue") from err

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

        async def async_update_data_1min() -> dict:
            """Fetch data from API endpoint at a 1 minute interval.

            This is the place to pre-process the data to lookup tables
            so entities can quickly look up their data.
            """
            data: dict = await runtime.update_sensors([Scale.MINUTE.value])
            if data:
                add_minute_mains_split(data)
                runtime.last_minute_data = data
            return data

        async def async_update_day_sensors() -> dict:
            now: datetime = datetime.now(UTC)
            if not runtime.last_day_update or (
                now - runtime.last_day_update
            ) > timedelta(minutes=15):
                _LOGGER.info("Updating day sensors")
                runtime.last_day_update = now
                updated_day_data = await runtime.update_sensors([Scale.DAY.value])
                apply_api_update_debounce(updated_day_data, runtime.last_day_data, "day")
                # Preserve locally-accumulated Import/Export totals across the
                # API refresh; Emporia's API doesn't provide these directly.
                carry_forward_mains_split(runtime.last_day_data, updated_day_data)
                runtime.last_day_data = updated_day_data
            else:
                _LOGGER.info("Integrating minute data into day sensors")
                if runtime.last_minute_data:
                    for identifier, data in runtime.last_minute_data.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        if channel_gid in MAINS_SPLIT_CHANNELS:
                            # Handled below via integrate_mains_split, sourced
                            # from the combined mains channel directly.
                            continue
                        day_id: str = f"{device_gid}-{channel_gid}-{Scale.DAY.value}"
                        if (
                            data
                            and runtime.last_day_data
                            and day_id in runtime.last_day_data
                            and runtime.last_day_data[day_id]
                            and "usage" in runtime.last_day_data[day_id]
                            and runtime.last_day_data[day_id]["usage"] is not None
                        ):
                            timestamp: datetime = data["timestamp"]
                            await runtime.check_for_midnight(
                                timestamp, int(device_gid), day_id, runtime.last_day_data
                            )
                            runtime.last_day_data[day_id]["usage"] += data["usage"]

                        if channel_gid == MAINS_COMBINED_CHANNEL_NUM:
                            await runtime.integrate_mains_split(
                                device_gid, data, runtime.last_day_data, is_month=False
                            )
            return runtime.last_day_data

        async def async_update_month_sensors() -> dict:
            now: datetime = datetime.now(UTC)
            if not runtime.last_month_update or (
                now - runtime.last_month_update
            ) > timedelta(minutes=30):
                _LOGGER.info("Updating month sensors")
                runtime.last_month_update = now
                updated_month_data = await runtime.update_sensors([Scale.MONTH.value])
                apply_api_update_debounce(
                    updated_month_data,
                    runtime.last_month_data,
                    "month",
                )
                carry_forward_mains_split(runtime.last_month_data, updated_month_data)
                runtime.last_month_data = updated_month_data
            else:
                _LOGGER.info("Integrating minute data into month sensors")
                if runtime.last_minute_data:
                    for identifier, data in runtime.last_minute_data.items():
                        device_gid, channel_gid, _ = identifier.split("-")
                        if channel_gid in MAINS_SPLIT_CHANNELS:
                            continue
                        month_id: str = f"{device_gid}-{channel_gid}-{Scale.MONTH.value}"
                        if (
                            data
                            and runtime.last_month_data
                            and month_id in runtime.last_month_data
                            and runtime.last_month_data[month_id]
                            and "usage" in runtime.last_month_data[month_id]
                            and runtime.last_month_data[month_id]["usage"] is not None
                        ):
                            timestamp: datetime = data["timestamp"]
                            await runtime.check_for_new_month(
                                timestamp, int(device_gid), month_id, runtime.last_month_data
                            )
                            runtime.last_month_data[month_id]["usage"] += data["usage"]

                        if channel_gid == MAINS_COMBINED_CHANNEL_NUM:
                            await runtime.integrate_mains_split(
                                device_gid, data, runtime.last_month_data, is_month=True
                            )
            return runtime.last_month_data

        # All three coordinators are always created and always poll,
        # regardless of ENABLE_1M/1D/1MON. Those options now only control
        # each non-Mains entity's default-enabled state in the entity
        # registry (see sensor.py) — they must never stop Mains/Grid data
        # from being fetched.
        coordinator_1min = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="sensor",
            update_method=async_update_data_1min,
            update_interval=timedelta(minutes=1),
        )
        await coordinator_1min.async_config_entry_first_refresh()
        _LOGGER.debug("1min Update data: %s", coordinator_1min.data)

        coordinator_1mon = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="sensor",
            update_method=async_update_month_sensors,
            update_interval=timedelta(minutes=1),
        )
        await coordinator_1mon.async_config_entry_first_refresh()
        _LOGGER.debug("1mon Update data: %s", coordinator_1mon.data)

        coordinator_day_sensor = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="sensor",
            update_method=async_update_day_sensors,
            update_interval=timedelta(minutes=1),
        )
        await coordinator_day_sensor.async_config_entry_first_refresh()

        has_controllable_devices = any(
            device.outlet or device.ev_charger
            for device in runtime.device_information.values()
        )

        async def async_update_device_status() -> dict[str, Any]:
            """Fetch device status (outlets and chargers)."""
            try:
                data: dict[str, Any] = {}
                outlets: list[OutletDevice]
                chargers: list[ChargerDevice]

                outlets, chargers = await hass.async_add_executor_job(vue.get_devices_status)

                if outlets:
                    for outlet in outlets:
                        data[str(outlet.device_gid)] = outlet
                if chargers:
                    for charger in chargers:
                        data[str(charger.device_gid)] = charger
                return data
            except Exception as err:
                raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err

        coordinator_device_status = None
        if has_controllable_devices:
            coordinator_device_status = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name="device_status",
                update_method=async_update_device_status,
                update_interval=timedelta(minutes=1),
            )
            await coordinator_device_status.async_config_entry_first_refresh()

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
                raise HomeAssistantError("Target device or Entity required.")

            unique_entity_id: str = charger_entity.unique_id
            gid_match: re.Match[str] | None = re.search(r"\d+", unique_entity_id)
            if not gid_match:
                raise HomeAssistantError(
                    f"Could not find device gid from unique id {unique_entity_id}"
                )

            charger_gid = int(gid_match.group(0))
            if (
                charger_gid not in runtime.device_information
                or not runtime.device_information[charger_gid].ev_charger
            ):
                raise HomeAssistantError(
                    "Set Charging Current called on invalid device with entity id"
                    f" {charger_entity.entity_id} (unique id {unique_entity_id})"
                )

            state = hass.states.get(charger_entity.entity_id)
            _LOGGER.info("State is %s", str(state))
            if not state:
                raise HomeAssistantError(
                    f"Could not find state for entity {charger_entity.entity_id}"
                )
            charger_info: VueDevice = runtime.device_information[charger_gid]
            if charger_info.ev_charger is None:
                raise HomeAssistantError(
                    f"Could not find charger info for device {charger_gid}"
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
                raise

        hass.services.async_register(
            DOMAIN, "set_charger_current", handle_set_charger_current
        )

    except Exception as err:
        _LOGGER.warning("Exception while setting up Emporia Vue. Will retry. %s", err)
        raise ConfigEntryNotReady(
            f"Exception while setting up Emporia Vue. Will retry. {err}"
        ) from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        VUE_DATA: vue,
        "coordinator_1min": coordinator_1min,
        "coordinator_1mon": coordinator_1mon,
        "coordinator_day_sensor": coordinator_day_sensor,
        "coordinator_device_status": coordinator_device_status,
        "device_information": runtime.device_information,
    }

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as err:
        _LOGGER.warning("Error setting up platforms: %s", err)
        raise ConfigEntryNotReady(f"Error setting up platforms: {err}") from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


def flatten_usage_data(
    usage_devices: dict[int, VueUsageDevice],
    scale: str,
) -> tuple[dict[str, VueDeviceChannelUsage], datetime]:
    """Flattens the raw usage data into a dictionary of channel ids and usage info."""
    flattened: dict[str, VueDeviceChannelUsage] = {}
    data_time: datetime = datetime.now(UTC)
    for usage in usage_devices.values():
        data_time = usage.timestamp or data_time
        if usage.channels:
            for channel in usage.channels.values():
                identifier: str = make_channel_id(channel, scale)
                flattened[identifier] = channel
                if channel.nested_devices:
                    nested_flattened, _ = flatten_usage_data(
                        channel.nested_devices, scale
                    )
                    flattened.update(nested_flattened)
    return (flattened, data_time)


def make_channel_id(channel: VueDeviceChannel, scale: str) -> str:
    """Format the channel id for a channel and scale."""
    return f"{channel.device_gid}-{channel.channel_num}-{scale}"


def fix_usage_sign(
    channel_num: str,
    usage: float,
    bidirectional: bool,
    is_solar: bool,
    invert_solar: bool,
) -> float:
    """If the channel is not '1,2,3' or 'Balance' we need it to be positive.

    (see https://github.com/magico13/ha-emporia-vue/issues/57)
    """
    if is_solar:
        if usage and invert_solar:
            return -1 * usage
        return usage

    if usage and not bidirectional and channel_num not in ["1,2,3", "Balance"]:
        return abs(usage)
    return usage


async def change_time_to_local(time: datetime, tz_string: str) -> datetime:
    """Change the datetime to the provided timezone, if not already."""
    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    tz_info: tzinfo | None = await loop.run_in_executor(
        None, dateutil.tz.gettz, tz_string
    )
    if not time.tzinfo or time.tzinfo.utcoffset(time) is None:
        time = time.replace(tzinfo=UTC)
    return time.astimezone(tz_info)


def determine_reset_datetime(
    local_time: datetime, monthly_cycle_start: int, is_month: bool
) -> datetime:
    """Determine the last reset datetime (aware) based on the passed time and cycle start date."""
    reset_datetime: datetime = local_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if is_month:
        last_day_this_month = calendar.monthrange(
            reset_datetime.year, reset_datetime.month
        )[1]
        target_day_this_month = min(monthly_cycle_start, last_day_this_month)
        candidate_this_month = reset_datetime.replace(day=target_day_this_month)

        if local_time >= candidate_this_month:
            reset_datetime = candidate_this_month
        else:
            previous_month = reset_datetime - dateutil.relativedelta.relativedelta(
                months=1
            )
            last_day_previous_month = calendar.monthrange(
                previous_month.year, previous_month.month
            )[1]
            target_day_previous_month = min(
                monthly_cycle_start, last_day_previous_month
            )
            reset_datetime = previous_month.replace(day=target_day_previous_month)
    return reset_datetime


def apply_api_update_debounce(
    updated_data: dict[str, Any],
    existing_data: dict[str, Any],
    scale_name: str,
) -> None:
    """Prevent API reset lag from inflating totals shortly after local reset time."""
    if not updated_data or not existing_data:
        return

    for identifier, updated in updated_data.items():
        if identifier not in existing_data or not updated:
            continue

        existing = existing_data[identifier]
        if not existing:
            continue

        updated_usage = updated.get("usage")
        existing_usage = existing.get("usage")
        reset_datetime = updated.get("reset")
        timestamp = updated.get("timestamp")

        if (
            updated_usage is None
            or existing_usage is None
            or reset_datetime is None
            or timestamp is None
        ):
            continue

        if is_in_reset_debounce_window(
            timestamp,
            reset_datetime,
            scale_name,
        ):
            bounded_usage = min(updated_usage, existing_usage)
            if bounded_usage != updated_usage:
                _LOGGER.info(
                    "Debouncing %s API reset lag for %s: keeping %.6f instead of %.6f",
                    scale_name,
                    identifier,
                    bounded_usage,
                    updated_usage,
                )
                updated["usage"] = bounded_usage


def is_in_reset_debounce_window(
    local_time: datetime,
    reset_datetime: datetime,
    scale_name: str,
    debounce_minutes: int = 30,
) -> bool:
    """Return true when local_time is in the reset debounce window for the scale."""
    if scale_name == "month" and local_time.date() != reset_datetime.date():
        return False

    elapsed = local_time - reset_datetime
    return timedelta(0) <= elapsed < timedelta(minutes=debounce_minutes)


# --- Mains Import/Export split (see sensor.py: VueMainsSplitSensor) -----------
#
# Emporia's API does not provide a native gross Import/Export split for most
# accounts/hardware. We derive it ourselves from the combined "1,2,3" mains
# channel:
#   - At MINUTE scale, a single instant only ever flows one direction, so the
#     sign of that minute's power value is a reliable, correct split.
#   - At DAY/MONTH scale, taking the sign of the *period's net total* is NOT
#     valid (a day with both import and export periods nets out to a
#     misleading single number). Instead we accumulate minute-by-minute,
#     adding each minute's usage to the Import or Export running total based
#     on that minute's sign, and reset the totals at midnight/billing-cycle
#     start the same way last_day_data/last_month_data already do.
#
# If your account/hardware exposes native "MainsFromGrid"/"MainsToGrid"
# channels (check the "Unused data found during update" log line), those are
# a more authoritative source and this derived-split logic can be retired in
# favor of just reading those channels directly as regular per-channel
# sensors.


def add_minute_mains_split(data: dict[str, Any]) -> None:
    """Add synthetic Import/Export power entries derived from the combined mains channel.

    Mutates `data` in place. Safe to call unconditionally; only affects
    devices that have a "1,2,3" combined mains channel entry.
    """
    for identifier in list(data.keys()):
        parts = identifier.split("-")
        if len(parts) != 3:
            continue
        device_gid, channel_num, scale = parts
        if channel_num != MAINS_COMBINED_CHANNEL_NUM:
            continue
        entry = data[identifier]
        usage = entry.get("usage")
        if usage is None:
            continue
        import_usage = usage if usage > 0 else 0.0
        export_usage = abs(usage) if usage < 0 else 0.0
        data[f"{device_gid}-{MAINS_SPLIT_CHANNEL_IMPORT}-{scale}"] = {
            **entry,
            "channel_num": MAINS_SPLIT_CHANNEL_IMPORT,
            "usage": import_usage,
        }
        data[f"{device_gid}-{MAINS_SPLIT_CHANNEL_EXPORT}-{scale}"] = {
            **entry,
            "channel_num": MAINS_SPLIT_CHANNEL_EXPORT,
            "usage": export_usage,
        }


def carry_forward_mains_split(old_data: dict[str, Any], new_data: dict[str, Any]) -> None:
    """Copy derived Mains Import/Export entries from old_data into new_data.

    Called after a full API refresh, since the API response never contains
    these synthetic entries and would otherwise wipe out the running totals.
    """
    if not old_data:
        return
    for key, value in old_data.items():
        parts = key.split("-")
        if len(parts) != 3:
            continue
        if parts[1] in MAINS_SPLIT_CHANNELS and key not in new_data:
            new_data[key] = value
