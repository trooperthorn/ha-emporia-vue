"""Platform for sensor integration."""

from datetime import datetime
import logging
from typing import Any

from pyemvue.device import VueDevice, VueDeviceChannel, ChargerDevice
from pyemvue.enums import Scale

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    MAINS_CHANNEL_NUMS,
    MAINS_COMBINED_CHANNEL_NUM,
    MAINS_SPLIT_CHANNELS,
)

_LOGGER: logging.Logger = logging.getLogger(__name__)

# Emporia's channel_type_gid for solar production channels. Used to make
# sure solar isn't double-subtracted in the Balance calculation (see
# VueBalanceSensor.native_value) — solar already nets out of the Mains
# reading physically, since the Mains CTs sit upstream of the solar
# interconnection point.
SOLAR_CHANNEL_TYPE_GID = 13

# Native Emporia channel labels for a device-side, already-computed
# Import/Export split. Where present, these are preferred over our own
# minute-integrated derivation (see VueMainsSplitSensor).
NATIVE_MAINS_FROM_GRID = "MainsFromGrid"
NATIVE_MAINS_TO_GRID = "MainsToGrid"


def _device_is_true_mains_panel(device: VueDevice) -> bool:
    """Return True only if this device is a genuine Mains/panel device.

    A device qualifies if it has the combined "1,2,3" channel AND at least
    one other real channel alongside it. A device reporting only a bare
    "1,2,3" with nothing else is a single-CT monitor — e.g. a dedicated
    solar production meter reporting its lone clamp reading through the
    same generic aggregate label — not a household panel/grid connection,
    and should not get Balance/Grid Import-Export sensors synthesized
    for it.
    """
    channel_nums = {ch.channel_num for ch in (device.channels or [])}
    if MAINS_COMBINED_CHANNEL_NUM not in channel_nums:
        return False
    other_channels = channel_nums - {MAINS_COMBINED_CHANNEL_NUM} - MAINS_SPLIT_CHANNELS
    return len(other_channels) > 0


def _coordinator_has_native_mains_split(coordinator, device_gid: int) -> bool:
    """Return True if this coordinator's current data includes Emporia's
    own native MainsFromGrid/MainsToGrid channels for this device.

    Checked per-coordinator (i.e. per scale) rather than once globally,
    since Emporia may only surface these channels at some scales (e.g.
    Day/Month) and not others (e.g. Minute) for a given account.
    """
    if not coordinator or not coordinator.data:
        return False
    found = {NATIVE_MAINS_FROM_GRID: False, NATIVE_MAINS_TO_GRID: False}
    for entry in coordinator.data.values():
        if entry.get("device_gid") != device_gid:
            continue
        channel_num = entry.get("channel_num")
        if channel_num in found:
            found[channel_num] = True
    return all(found.values())


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    domain_data = hass.data[DOMAIN][config_entry.entry_id]

    coordinator_1min = domain_data.get("coordinator_1min")
    coordinator_1mon = domain_data.get("coordinator_1mon")
    coordinator_day_sensor = domain_data.get("coordinator_day_sensor")
    coordinator_device_status = domain_data.get("coordinator_device_status")
    device_information: dict[int, VueDevice] = domain_data.get("device_information", {})

    enable_1m = config_entry.data.get(ENABLE_1M, True)
    enable_1d = config_entry.data.get(ENABLE_1D, True)
    enable_1mon = config_entry.data.get(ENABLE_1MON, True)

    all_entities = []

    def add_scale_block(coordinator, scale_enabled: bool) -> None:
        """Create a CurrentVuePowerSensor for every real channel this coordinator has.

        Every channel is always created. scale_enabled only controls the
        default-enabled state in the entity registry, EXCEPT Mains channels,
        which are always force-enabled. Synthetic Mains Import/Export
        entries are skipped here — they're represented by VueMainsSplitSensor
        instead, added below.
        """
        if not coordinator or not coordinator.data:
            return
        for identifier in coordinator.data:
            entry = coordinator.data[identifier]
            channel_num = str(entry.get("channel_num"))
            if channel_num in MAINS_SPLIT_CHANNELS:
                continue
            is_mains = channel_num in MAINS_CHANNEL_NUMS
            all_entities.append(
                CurrentVuePowerSensor(
                    coordinator,
                    identifier,
                    force_enabled=is_mains or scale_enabled,
                )
            )

    # 1. ADD PER-CHANNEL SENSORS FOR ALL THREE SCALES
    # Every channel is always created for Minute, Day, and Month — this
    # includes single-CT monitors (e.g. solar) via their own "1,2,3"
    # channel, native MainsFromGrid/MainsToGrid channels where Emporia
    # provides them, and every ordinary branch circuit.
    add_scale_block(coordinator_1min, enable_1m)
    add_scale_block(coordinator_day_sensor, enable_1d)
    add_scale_block(coordinator_1mon, enable_1mon)

    # 2. ADD BALANCE AND GRID IMPORT/EXPORT SENSORS FOR ALL THREE SCALES
    # Always created, always enabled — independent of ENABLE_1M/1D/1MON —
    # but only for devices that are genuinely a Mains/panel device (see
    # _device_is_true_mains_panel). The derived Grid Import/Export sensor
    # is skipped per-scale wherever that scale already has Emporia's own
    # native MainsFromGrid/MainsToGrid channels, since CurrentVuePowerSensor
    # already creates entities for those (via MAINS_CHANNEL_NUMS) and
    # they're a more authoritative source than our minute-integrated
    # derivation.
    for gid, device in device_information.items():
        if _device_is_true_mains_panel(device):
            for coordinator, scale in (
                (coordinator_1min, "1MIN"),
                (coordinator_day_sensor, "1D"),
                (coordinator_1mon, "1MON"),
            ):
                all_entities.append(VueBalanceSensor(coordinator, device, scale))
                if not _coordinator_has_native_mains_split(coordinator, gid):
                    all_entities.append(
                        VueMainsSplitSensor(coordinator, device, scale, "Import")
                    )
                    all_entities.append(
                        VueMainsSplitSensor(coordinator, device, scale, "Export")
                    )

    # 3. ADD CHARGER STATUS & CHARGE TIME SENSORS
    if coordinator_device_status and coordinator_device_status.data:
        soc_sensor = config_entry.options.get("vehicle_soc_sensor")

        for gid in coordinator_device_status.data:
            if int(gid) in device_information and device_information[int(gid)].ev_charger:
                device_obj = device_information[int(gid)]

                if soc_sensor and isinstance(soc_sensor, str) and soc_sensor.strip():
                    all_entities.append(
                        EmporiaEVChargeTimeNeededSensor(hass, config_entry, device_obj)
                    )

                all_entities.append(
                    EmporiaChargerStatusSensor(coordinator_device_status, device_obj)
                )

    async_add_entities(all_entities)

    # 4. CLEAN UP / DISABLE DEVICES WITH ZERO ACTIVE ENTITIES
    # Built from each entity's own device_info property, which every HA
    # entity has regardless of whether the subclass sets _attr_device_info
    # or overrides the property directly. This works uniformly across
    # every entity class in this file, including devices that only ever
    # get a plain CurrentVuePowerSensor (e.g. single-CT solar monitors).
    #
    # Known limitation: this only sees entities created in THIS platform's
    # async_setup_entry. A device populated entirely by switch.py/number.py
    # (e.g. a smart outlet with no EV charger, which never appears in
    # sensor.py at all) will still be misjudged as inactive here. Fixing
    # that properly means moving this pass to __init__.py, run once after
    # all platforms have finished forwarding entry setup, and building the
    # active set from the entity registry directly instead of one
    # platform's local list.
    active_identifiers: set[tuple[str, str]] = set()
    for entity in all_entities:
        info = entity.device_info
        if info and info.get("identifiers"):
            active_identifiers.update(info["identifiers"])

    device_registry = dr.async_get(hass)

    for dev_entry in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id):
        is_active = any(ident in active_identifiers for ident in dev_entry.identifiers)

        if is_active:
            # Re-enable a device we previously auto-disabled, now that it
            # has active entities again (e.g. a channel reappeared).
            if dev_entry.disabled_by == dr.DeviceEntryDisabler.INTEGRATION:
                device_registry.async_update_device(dev_entry.id, disabled_by=None)
        elif dev_entry.disabled_by is None:
            device_registry.async_update_device(
                dev_entry.id,
                disabled_by=dr.DeviceEntryDisabler.INTEGRATION,
            )


class CurrentVuePowerSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, identifier, force_enabled: bool = True) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._scale: str = coordinator.data[identifier]["scale"]
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]

        # Enabled by default only if this scale/channel is supposed to be on
        # (force_enabled, always True for Mains channels regardless of the
        # user's Minute/Day/Month option) AND we actually have usage data.
        initial_usage = coordinator.data[identifier].get("usage")
        self._attr_entity_registry_enabled_default = (
            force_enabled and initial_usage is not None
        )

        final_channel: VueDeviceChannel | None = None
        if self._device is not None:
            for channel in self._device.channels:
                if channel.channel_num == channel_num:
                    final_channel = channel
                    break
        if final_channel is None:
            _LOGGER.warning(
                "No channel found for device_gid %s and channel_num %s",
                device_gid,
                channel_num,
            )
            raise RuntimeError(
                f"No channel found for device_gid {device_gid} and channel_num {channel_num}"
            )
        self._channel: VueDeviceChannel = final_channel
        self._iskwh = self.scale_is_energy()

        self._attr_has_entity_name = True

        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 3
            self._attr_name = f"Energy {self.scale_readable()}"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            self._attr_name = f"Power {self.scale_readable()}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the sensor."""
        return {
            "channel_num": self._channel.channel_num,
            "channel_name": self._channel.name,
            "device_gid": self._device.device_gid,
            "scale": self._scale,
            "channel_multiplier": self._channel.channel_multiplier,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        if self._channel.channel_num in MAINS_CHANNEL_NUMS:
            # Group Mains/aggregate channels with the parent monitor device,
            # alongside the Balance/Grid Import/Export sensors, instead of
            # creating a separate per-channel device for them.
            return DeviceInfo(
                identifiers={(DOMAIN, str(self._device.device_gid))},
                name=self._device.device_name or f"Emporia Vue {self._device.device_gid}",
                model=self._device.model,
                sw_version=self._device.firmware,
                manufacturer="Emporia",
            )
        device_name = self._channel.name
        if not device_name:
            if self._channel.channel_num.isdigit():
                device_name = (
                    f"{self._device.device_name} Circuit {self._channel.channel_num}"
                )
            else:
                device_name = self._device.device_name
        return DeviceInfo(
            identifiers={
                (DOMAIN, f"{self._device.device_gid}-{self._channel.channel_num}")
            },
            name=device_name,
            model=self._device.model,
            sw_version=self._device.firmware,
            manufacturer="Emporia",
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self._id in self.coordinator.data:
            usage = self.coordinator.data[self._id]["usage"]
            return self.scale_usage(usage) if usage is not None else None
        return None

    @property
    def unique_id(self) -> str:
        """Return the Unique ID for the sensor."""
        if self._scale == Scale.MINUTE.value:
            return (
                "sensor.emporia_vue.instant."
                f"{self._channel.device_gid}-{self._channel.channel_num}"
            )
        return (
            f"sensor.emporia_vue.{self._scale}."
            f"{self._channel.device_gid}-{self._channel.channel_num}"
        )

    @property
    def last_reset(self):
        """Return the time when the sensor was last reset, if any."""
        if self._iskwh and self.coordinator.data and self._id in self.coordinator.data:
            return self.coordinator.data[self._id].get("reset")
        return None

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = 60 * 1000 * usage
        elif self._scale == Scale.SECOND.value:
            usage = 3600 * 1000 * usage
        elif self._scale == Scale.MINUTES_15.value:
            usage = 4 * 1000 * usage
        return usage

    def scale_is_energy(self):
        """Return True if the scale is an energy unit instead of power."""
        return self._scale not in (
            Scale.MINUTE.value,
            Scale.SECOND.value,
            Scale.MINUTES_15.value,
        )

    def scale_readable(self):
        """Return a human readable scale."""
        if self._scale == Scale.MINUTE.value:
            return "Minute Average"
        if self._scale == Scale.DAY.value:
            return "Today"
        if self._scale == Scale.MONTH.value:
            return "This Month"
        return self._scale


def _map_charger_state(status: str | None, message: str | None, fault_text: str | None) -> tuple[str, str]:
    """Map Emporia charger status/message to a human-friendly state and IEC 61851 code."""
    status_lower = (status or "").lower()
    message_lower = (message or "").lower()
    fault = (fault_text or "").strip()

    if fault or "error" in status_lower or "fault" in status_lower or "error" in message_lower or "fault" in message_lower:
        return "Error", "F"
    if status_lower == "charging":
        return "Charging", "C"
    if not status_lower:
        return "Disconnected", "A"
    if status_lower == "devicenotconnected":
        return "Disconnected", "A"
    if status_lower == "standby" and message_lower in ("ready", "off", "self test", "please wait"):
        return "Disconnected", "A"
    if status_lower != "standby":
        _LOGGER.debug(
            "Unmapped charger state: status=%s, message=%s", status, message
        )
    return "Connected", "B"


CHARGER_STATUS_OPTIONS = ["Disconnected", "Connected", "Charging", "Error"]


class EmporiaChargerStatusSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Representation of an Emporia Charger status sensor."""

    def __init__(self, coordinator, device: VueDevice) -> None:
        """Initialize the charger status sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_gid = str(device.device_gid)
        self._attr_has_entity_name = True
        self._attr_name = "Status"
        self._attr_translation_key = "charger_status"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = CHARGER_STATUS_OPTIONS
        self._attr_icon = "mdi:ev-station"

    @property
    def native_value(self) -> str:
        """Return the human-friendly charger status."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if data:
            state, _ = _map_charger_state(data.status, data.message, data.fault_text)
            return state
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return IEC code and raw Emporia values as attributes."""
        data: ChargerDevice | None = self.coordinator.data.get(self._device_gid)
        if data:
            _, iec_code = _map_charger_state(data.status, data.message, data.fault_text)
            return {
                "iec_status": iec_code,
                "raw_status": data.status,
                "raw_message": data.message,
                "fault_text": data.fault_text,
            }
        return {}

    @property
    def unique_id(self) -> str:
        """Unique ID for the charger status sensor."""
        return f"emporia_vue.charger_status_{self._device_gid}"

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


class VueBalanceSensor(CoordinatorEntity, SensorEntity):
    """Representation of a dynamically calculated Unmonitored Balance sensor."""

    def __init__(self, coordinator, device, scale: str) -> None:
        """Initialize the balance sensor."""
        super().__init__(coordinator)
        self._device = device
        self._scale = scale
        self._device_gid = device.device_gid

        # Solar channel_nums for this device, precomputed so native_value
        # doesn't need to re-scan device.channels on every update.
        self._solar_channel_nums = {
            ch.channel_num
            for ch in (device.channels or [])
            if ch.channel_type_gid == SOLAR_CHANNEL_TYPE_GID
        }

        self._attr_has_entity_name = True
        self._attr_unique_id = f"vue_balance_{self._device_gid}_{scale}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self._device_gid))},
            name=self._device.device_name or f"Emporia Vue {self._device_gid}",
            manufacturer="Emporia",
            model=self._device.model,
        )

        self._iskwh = scale not in ["1S", "1MIN"]

        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 3
            self._attr_name = f"Balance Energy ({scale})"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            self._attr_name = f"Balance Power ({scale})"

    @property
    def native_value(self) -> float | None:
        """Calculate the balance dynamically from the coordinator data.

        Mains CTs sit upstream of the solar interconnection point, so the
        Mains reading already reflects (House Consumption - Solar
        Production) — it goes negative when solar exports more than the
        house is using. If solar's channel were folded into branch_usage
        like a normal consumption circuit, its production would be
        subtracted a second time here, driving Balance sharply negative
        (and thus to the 0.0 floor) any time solar output is significant.
        So solar is tracked separately and added back rather than treated
        as a consumption branch:

            Balance = (Mains + Solar Production) - sum(other branches)
        """
        if not self.coordinator.data:
            return None

        mains_usage = 0.0
        branch_usage = 0.0
        solar_usage = 0.0

        for identifier, data in self.coordinator.data.items():
            if data.get("device_gid") != self._device_gid or data.get("scale") != self._scale:
                continue
            channel_num = str(data.get("channel_num"))
            if channel_num in MAINS_SPLIT_CHANNELS:
                # Derived Import/Export entries — not part of this calculation.
                continue

            usage = data.get("usage")
            if usage is None:
                continue

            if channel_num in ("1", "2", "3", "1,2,3"):
                mains_usage += usage
            elif channel_num in self._solar_channel_nums:
                solar_usage += usage
            elif channel_num.isdigit() and int(channel_num) >= 4:
                branch_usage += usage

        balance = (mains_usage + solar_usage) - branch_usage

        # Due to slight analog inaccuracies in CT clamps, the sum of branches
        # can occasionally exceed Mains+Solar by a tiny fraction. The Emporia
        # app floors this at 0, so we do the same to prevent UI artifacts.
        return max(balance, 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return {
            "device_gid": self._device_gid,
            "scale": self._scale,
            "description": (
                "Calculated: (Total Mains + Solar Production) minus "
                "Sum of other Monitored Branch Circuits"
            ),
        }

    @property
    def last_reset(self):
        """Return the time when the sensor was last reset, if any."""
        if not self._iskwh or not self.coordinator.data:
            return None

        for identifier, data in self.coordinator.data.items():
            if data.get("device_gid") == self._device_gid and data.get("scale") == self._scale:
                channel_num = str(data.get("channel_num"))
                if channel_num in ("1", "2", "3", "1,2,3"):
                    return data.get("reset")
        return None


class VueMainsSplitSensor(CoordinatorEntity, SensorEntity):
    """Representation of the derived Grid Import or Export sensor.

    Reads a pre-computed value from coordinator data (see
    add_minute_mains_split / integrate_mains_split in __init__.py), derived
    from the sign of the combined Mains channel at MINUTE resolution. This
    correctly handles solar export: when solar production exceeds house
    load, the Mains CT reads negative for that minute, and that minute's
    magnitude is accumulated into Export rather than Import.

    Only instantiated where the device/scale combination lacks Emporia's
    own native MainsFromGrid/MainsToGrid channels (see
    _coordinator_has_native_mains_split in async_setup_entry) — where those
    exist, they're a more authoritative source and are surfaced instead via
    the ordinary CurrentVuePowerSensor path.
    """

    def __init__(self, coordinator, device: VueDevice, scale: str, direction: str) -> None:
        """Initialize the split mains sensor."""
        super().__init__(coordinator)
        self._device = device
        self._scale = scale
        self._device_gid = device.device_gid
        self._direction = direction

        channel_num = "MainsImport" if direction == "Import" else "MainsExport"
        self._id = f"{self._device_gid}-{channel_num}-{scale}"

        self._attr_has_entity_name = True
        self._attr_unique_id = f"vue_mains_{self._direction.lower()}_{self._device_gid}_{scale}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(self._device_gid))},
            name=self._device.device_name or f"Emporia Vue {self._device_gid}",
            manufacturer="Emporia",
            model=self._device.model,
        )

        self._iskwh = scale not in ["1S", "1MIN"]

        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL
            self._attr_suggested_display_precision = 3
            self._attr_name = f"Grid {self._direction} Energy ({scale})"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            self._attr_name = f"Grid {self._direction} Power ({scale})"

    @property
    def native_value(self) -> float | None:
        """Return the pre-computed Import/Export value for this scale."""
        entry = self.coordinator.data.get(self._id) if self.coordinator.data else None
        if not entry:
            return 0.0 if self._iskwh else None
        return entry.get("usage")

    @property
    def last_reset(self):
        """Return the time when this total was last reset, if any."""
        if not self._iskwh or not self.coordinator.data:
            return None
        entry = self.coordinator.data.get(self._id)
        return entry.get("reset") if entry else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_gid": self._device_gid,
            "scale": self._scale,
            "description": f"Accumulated {self._direction} from the combined Mains channel",
        }


class EmporiaEVChargeTimeNeededSensor(SensorEntity):
    """Representation of calculated EV Charge Time Needed."""

    _attr_native_unit_of_measurement = "h"
    _attr_icon = "mdi:timer-sand"
    _attr_name = "EV Charge Time Needed"
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, charger_device: VueDevice) -> None:
        """Initialize the charge time sensor."""
        self.hass = hass
        self._config_entry = config_entry
        self._charger_device = charger_device
        self._device_gid = charger_device.device_gid

        self._attr_unique_id = f"emporia_vue_ev_charge_time_needed_{self._device_gid}"

        # Matches the device identifier used by EmporiaChargerEntity
        # (switch.py / number.py) so this sensor groups with the charger's
        # switch/status/current-limit entities instead of a separate device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_gid}-1,2,3")},
            name=self._charger_device.device_name or f"Emporia Vue {self._device_gid}",
            manufacturer="Emporia",
            model=self._charger_device.model,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to vehicle battery sensor state changes."""
        await super().async_added_to_hass()

        vehicle_soc_sensor = self._config_entry.options.get("vehicle_soc_sensor")
        if vehicle_soc_sensor:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [vehicle_soc_sensor], self._async_on_soc_update
                )
            )

    async def _async_on_soc_update(self, event) -> None:
        """Handle vehicle SoC updates."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Calculate charge time needed in hours."""
        vehicle_soc_sensor = self._config_entry.options.get("vehicle_soc_sensor")
        battery_capacity = float(self._config_entry.options.get("battery_capacity_kwh", 80.0))

        if not vehicle_soc_sensor:
            return None

        soc_state = self.hass.states.get(vehicle_soc_sensor)
        if not soc_state or soc_state.state in ["unknown", "unavailable"]:
            return None

        try:
            current_soc = float(soc_state.state)
        except ValueError:
            return None

        target_soc = 100.0
        percent_needed = max(target_soc - current_soc, 0.0)
        kwh_needed = (percent_needed / 100.0) * battery_capacity

        amps_entity = f"number.emporia_vue_charger_current_{self._device_gid}"
        amps_state = self.hass.states.get(amps_entity)
        amps = float(amps_state.state) if amps_state and amps_state.state.replace('.', '', 1).isdigit() else 40.0

        charge_rate_kw = (amps * 240.0) / 1000.0

        if charge_rate_kw <= 0:
            return 0.0

        hours_needed = (kwh_needed / charge_rate_kw) * 1.1
        return round(hours_needed, 2)
