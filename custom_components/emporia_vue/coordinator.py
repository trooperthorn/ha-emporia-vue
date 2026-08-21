"""Data update coordinators for the Emporia Vue integration."""

import asyncio
import calendar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
import logging
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

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    MAINS_COMBINED_CHANNEL_NUM,
    MAINS_SPLIT_CHANNEL_EXPORT,
    MAINS_SPLIT_CHANNEL_IMPORT,
    MAINS_SPLIT_CHANNELS,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)


@dataclass
class VueRuntimeData:
    """Mutable, config-entry-scoped state for one Emporia Vue account.

    Held on ConfigEntry.runtime_data (never module-level globals or
    hass.data): module globals would leak/collide if this domain ever
    supported more than one config entry, and hass.data duplicates what
    runtime_data already exists for.
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

    coordinator_1min: "VueMinuteCoordinator | None" = None
    coordinator_1mon: "VueMonthCoordinator | None" = None
    coordinator_day_sensor: "VueDayCoordinator | None" = None
    coordinator_device_status: "VueDeviceStatusCoordinator | None" = None

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


class VueMinuteCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for 1-minute power data."""

    def __init__(self, hass: HomeAssistant, runtime: VueRuntimeData) -> None:
        """Initialize the minute coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="sensor",
            update_interval=timedelta(minutes=1),
        )
        self.runtime = runtime

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API endpoint at a 1 minute interval.

        This is the place to pre-process the data to lookup tables so
        entities can quickly look up their data.
        """
        data: dict = await self.runtime.update_sensors([Scale.MINUTE.value])
        if data:
            add_minute_mains_split(data)
            self.runtime.last_minute_data = data
        return data


class VueDayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for daily energy data."""

    def __init__(self, hass: HomeAssistant, runtime: VueRuntimeData) -> None:
        """Initialize the day coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="sensor",
            update_interval=timedelta(minutes=1),
        )
        self.runtime = runtime

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh day totals from the API every 15 minutes, integrating minute data between."""
        runtime = self.runtime
        now: datetime = datetime.now(UTC)
        if not runtime.last_day_update or (now - runtime.last_day_update) > timedelta(
            minutes=15
        ):
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


class VueMonthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for monthly (billing cycle) energy data."""

    def __init__(self, hass: HomeAssistant, runtime: VueRuntimeData) -> None:
        """Initialize the month coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="sensor",
            update_interval=timedelta(minutes=1),
        )
        self.runtime = runtime

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh month totals from the API every 30 minutes, integrating minute data between."""
        runtime = self.runtime
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


class VueDeviceStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for outlet/EV charger device status."""

    def __init__(self, hass: HomeAssistant, vue: PyEmVue) -> None:
        """Initialize the device status coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="device_status",
            update_interval=timedelta(minutes=1),
        )
        self.vue = vue

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch device status (outlets and chargers)."""
        try:
            data: dict[str, Any] = {}
            outlets: list[OutletDevice]
            chargers: list[ChargerDevice]

            outlets, chargers = await self.hass.async_add_executor_job(
                self.vue.get_devices_status
            )

            if outlets:
                for outlet in outlets:
                    data[str(outlet.device_gid)] = outlet
            if chargers:
                for charger in chargers:
                    data[str(charger.device_gid)] = charger
            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Emporia API: {err}") from err
