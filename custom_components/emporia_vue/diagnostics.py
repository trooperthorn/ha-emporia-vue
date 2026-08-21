"""Diagnostics support for Emporia Vue."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import EmporiaVueConfigEntry, redact_config_data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EmporiaVueConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data

    devices: list[dict[str, Any]] = []
    for gid, device in runtime.device_information.items():
        devices.append(
            {
                "device_gid": gid,
                "model": device.model,
                "firmware": device.firmware,
                "outlet": device.outlet is not None,
                "ev_charger": device.ev_charger is not None,
                "channel_count": len(device.channels or []),
                "channels": [
                    {
                        "channel_num": channel.channel_num,
                        "name": channel.name,
                        "channel_type_gid": channel.channel_type_gid,
                        "channel_multiplier": channel.channel_multiplier,
                    }
                    for channel in (device.channels or [])
                ],
            }
        )

    def _coordinator_summary(coordinator) -> dict[str, Any] | None:
        if coordinator is None:
            return None
        return {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "data_point_count": len(coordinator.data) if coordinator.data else 0,
        }

    return {
        "entry_data": redact_config_data(entry.data),
        "entry_options": dict(entry.options),
        "device_count": len(runtime.device_information),
        "devices": devices,
        "coordinators": {
            "1min": _coordinator_summary(runtime.coordinator_1min),
            "day": _coordinator_summary(runtime.coordinator_day_sensor),
            "1mon": _coordinator_summary(runtime.coordinator_1mon),
            "device_status": _coordinator_summary(runtime.coordinator_device_status),
        },
    }
