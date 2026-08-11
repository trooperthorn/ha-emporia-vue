"""Platform for sensor integration."""

from datetime import datetime
import logging

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

from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__name__)


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

    _LOGGER.info(domain_data)

    # Global configuration flags
    enable_1m = config_entry.data.get(ENABLE_1M, True)
    enable_1d = config_entry.data.get(ENABLE_1D, True)
    enable_1mon = config_entry.data.get(ENABLE_1MON, True)

    all_entities = []

    # 1. ADD THE 1-MINUTE SENSORS
    if coordinator_1min:
        for _, identifier in enumerate(coordinator_1min.data):
            all_entities.append(CurrentVuePowerSensor(coordinator_1min, identifier))
            
        for gid, device in device_information.items():
            if device.model is not None and "Vue" in device.model:
                all_entities.append(VueBalanceSensor(coordinator_1min, device, "1MIN"))
                all_entities.append(VueMainsSplitSensor(coordinator_1min, device, "1MIN", "Import"))
                all_entities.append(VueMainsSplitSensor(coordinator_1min, device, "1MIN", "Export"))

    # 2. ADD THE 1-MONTH SENSORS (Forcing MainFromGrid / MainToGrid)
    if coordinator_1mon:
        for _, identifier in enumerate(coordinator_1mon.data):
            all_entities.append(CurrentVuePowerSensor(coordinator_1mon, identifier))
            
        for gid, device in device_information.items():
            if device.model is not None and "Vue" in device.model:
                is_main_grid = device.device_name in ["MainFromGrid", "MainToGrid"]
                if enable_1mon or is_main_grid:
                    all_entities.append(VueBalanceSensor(coordinator_1mon, device, "1MON"))

    # 3. ADD THE 1-DAY SENSORS (Forcing MainFromGrid / MainToGrid)
    if coordinator_day_sensor:
        for _, identifier in enumerate(coordinator_day_sensor.data):
            all_entities.append(CurrentVuePowerSensor(coordinator_day_sensor, identifier))
            
        for gid, device in device_information.items():
            if device.model is not None and "Vue" in device.model:
                is_main_grid = device.device_name in ["MainFromGrid", "MainToGrid"]
                if enable_1d or is_main_grid:
                    all_entities.append(VueBalanceSensor(coordinator_day_sensor, device, "1D"))

    # 4. ADD CHARGER STATUS & CHARGE TIME SENSORS
    if coordinator_device_status and coordinator_device_status.data:
        soc_sensor = config_entry.options.get("vehicle_soc_sensor")
        
        for gid in coordinator_device_status.data:
            if int(gid) in device_information and device_information[int(gid)].ev_charger:
                device_obj = device_information[int(gid)]
                
                # Add charge time calculation sensor only if option is configured
                if soc_sensor and isinstance(soc_sensor, str) and soc_sensor.strip():
                    all_entities.append(
                        EmporiaEVChargeTimeNeededSensor(hass, config_entry, device_obj)
                    )
                
                # Add charger status sensor
                all_entities.append(
                    EmporiaChargerStatusSensor(coordinator_device_status, device_obj)
                )

    # Register all collected entities at once
    async_add_entities(all_entities)

    # 5. CLEAN UP / DISABLE DEVICES WITH ZERO ENTITIES
    active_device_gids = {
        getattr(entity, "_device_gid", None) 
        for entity in all_entities 
        if hasattr(entity, "_device_gid") and entity._device_gid is not None
    }

    device_registry = dr.async_get(hass)
    
    for dev_entry in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id):
        dev_gid = None
        for domain_name, identifier in dev_entry.identifiers:
            if domain_name == DOMAIN:
                try:
                    dev_gid = int(identifier)
                except ValueError:
                    pass
        
        # If the device has no active entities created, disable it in the registry
        if dev_gid and dev_gid not in active_device_gids:
            if not dev_entry.disabled_by:
                device_registry.async_update_device(
                    dev_entry.id,
                    disabled_by=dr.DeviceEntryDisabler.INTEGRATION, # <-- Correct
                )

class CurrentVuePowerSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Representation of a Vue Sensor's current power."""

    def __init__(self, coordinator, identifier) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)
        self._id = identifier
        self._scale: str = coordinator.data[identifier]["scale"]
        device_gid: int = coordinator.data[identifier]["device_gid"]
        channel_num: str = coordinator.data[identifier]["channel_num"]
        self._device: VueDevice = coordinator.data[identifier]["info"]
        
        # 1. Disable entity by default if no usage data is returned
        initial_usage = coordinator.data[identifier].get("usage")
        if initial_usage is None:
            self._attr_entity_registry_enabled_default = False
        else:
            self._attr_entity_registry_enabled_default = True

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

        # 2. Add Device Registry Grouping
        # Groups all entities under their parent hardware device in HA
        self._attr_device_info = DeviceInfo(
            identifiers={("emporia_vue", str(device_gid))}, # Use your specific DOMAIN variable here
            name=self._device.device_name or f"Emporia Vue {device_gid}",
            manufacturer="Emporia",
            model=self._device.model,
        )

        self._attr_has_entity_name = True
        
        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            # Changed from TOTAL to TOTAL_INCREASING for accurate Energy Dashboard resets
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING 
            self._attr_suggested_display_precision = 3
            self._attr_name = f"Energy {self.scale_readable()}"
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 1
            self._attr_name = f"Power {self.scale_readable()}"

    # 3. Add Custom Attributes for Scripting and Templating
    @property
    def extra_state_attributes(self) -> dict[str, any]:
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
        device_name = self._channel.name
        if not device_name:
            # An unnamed *numbered* channel is a CT that has not been configured
            # in the Emporia app. Falling back to the monitor's name gives every
            # such channel an identical name, so a Vue with spare channels shows
            # up as a pile of same-named devices (#379, #328).
            # Aggregate channels ("1,2,3", MainsFromGrid, Balance, ...) legitimately
            # represent the monitor itself, so those keep the monitor's name.
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
            return self.coordinator.data[self._id].get("last_reset")
        return None

    def scale_usage(self, usage):
        """Scales the usage to the correct timescale and magnitude."""
        if self._scale == Scale.MINUTE.value:
            usage = 60 * 1000 * usage  # convert from kwh to w rate
        elif self._scale == Scale.SECOND.value:
            usage = 3600 * 1000 * usage  # convert to rate
        elif self._scale == Scale.MINUTES_15.value:
            usage = (
                4 * 1000 * usage
            )  # this might never be used but for safety, convert to rate
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


# Known Emporia charger API responses (from historical data):
#   Status: "Charging", "Standby", "DeviceNotConnected", ""
#   Messages: "Charging", "Ready", "Off", "Self Test", "Offline",
#             "EV is not accepting charge", "Connected to EV",
#             "Please Wait", "Charging Halted", ""

def _map_charger_state(status: str | None, message: str | None, fault_text: str | None) -> tuple[str, str]:
    """Map Emporia charger status/message to a human-friendly state and IEC 61851 code."""
    status_lower = (status or "").lower()
    message_lower = (message or "").lower()
    fault = (fault_text or "").strip()

    # F: Fault condition
    if fault or "error" in status_lower or "fault" in status_lower or "error" in message_lower or "fault" in message_lower:
        return "Error", "F"
    # C: Actively charging
    if status_lower == "charging":
        return "Charging", "C"
    # A: Disconnected -  no EV present or device offline
    if not status_lower:
        return "Disconnected", "A"
    if status_lower == "devicenotconnected":
        return "Disconnected", "A"
    if status_lower == "standby" and message_lower in ("ready", "off", "self test", "please wait"):
        return "Disconnected", "A"
    # B: Connected but not charging (default for unknown/unmapped states)
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

        self._attr_has_entity_name = True
        self._attr_unique_id = f"vue_balance_{self._device_gid}_{scale}"

        # Group this sensor with the physical Vue monitor in the HA UI
        self._attr_device_info = DeviceInfo(
            identifiers={("emporia_vue", str(self._device_gid))},
            name=self._device.device_name or f"Emporia Vue {self._device_gid}",
            manufacturer="Emporia",
            model=self._device.model,
        )

        # Determine if we are measuring Power (1MIN) or Energy (1D/1MON)
        self._iskwh = scale not in ["1S", "1MIN"]

        if self._iskwh:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
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
        """Calculate the balance dynamically from the coordinator data."""
        if not self.coordinator.data:
            return None

        mains_usage = 0.0
        branch_usage = 0.0

        # Scan the coordinator data for THIS specific device and scale
        for identifier, data in self.coordinator.data.items():
            if data.get("device_gid") == self._device_gid and data.get("scale") == self._scale:
                usage = data.get("usage")
                if usage is None:
                    continue

                channel_num = str(data.get("channel_num"))

                # Emporia Gen 2/3 hardware uses 1, 2, 3 (or "1,2,3") for Mains CTs.
                # All branch circuits are channel 4 and above.
                if channel_num in ["1", "2", "3", "1,2,3"]:
                    mains_usage += usage
                elif channel_num.isdigit() and int(channel_num) >= 4:
                    branch_usage += usage

        # Calculate balance. 
        balance = mains_usage - branch_usage

        # Due to slight analog inaccuracies in CT clamps, the sum of branches 
        # can occasionally exceed the mains by a tiny fraction, resulting in a negative number.
        # The Emporia app floors this at 0, so we should do the same to prevent UI artifacts.
        return max(balance, 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, any]:
        """Return the state attributes."""
        return {
            "device_gid": self._device_gid,
            "scale": self._scale,
            "description": "Calculated: Total Mains minus Sum of Branch Circuits",
        }
    @property
    def last_reset(self):
        """Return the time when the sensor was last reset, if any."""
        if not self._iskwh or not self.coordinator.data:
            return None
            
        for identifier, data in self.coordinator.data.items():
            if data.get("device_gid") == self._device_gid and data.get("scale") == self._scale:
                return data.get("last_reset")
        return None


class VueMainsSplitSensor(CoordinatorEntity, SensorEntity):
    """Representation of calculated Grid Import or Export sensors."""

    def __init__(self, coordinator, device, scale: str, direction: str) -> None:
        """Initialize the split mains sensor."""
        super().__init__(coordinator)
        self._device = device
        self._scale = scale
        self._device_gid = device.device_gid
        # Direction is either "Import" or "Export"
        self._direction = direction 

        self._attr_has_entity_name = True
        self._attr_unique_id = f"vue_mains_{self._direction.lower()}_{self._device_gid}_{scale}"

        self._attr_device_info = DeviceInfo(
            identifiers={("emporia_vue", str(self._device_gid))},
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
        """Calculate the Import or Export dynamically."""
        if not self.coordinator.data:
            return None

        mains_usage = 0.0
        mains_found = False

        # Sum up the Mains channels (1, 2, and 3)
        for identifier, data in self.coordinator.data.items():
            if data.get("device_gid") == self._device_gid and data.get("scale") == self._scale:
                usage = data.get("usage")
                if usage is None:
                    continue

                channel_num = str(data.get("channel_num"))
                if channel_num in ["1", "2", "3", "1,2,3"]:
                    mains_usage += usage
                    mains_found = True

        if not mains_found:
            return None

        # Return strictly positive values based on the direction requested
        if self._direction == "Import":
            return mains_usage if mains_usage > 0 else 0.0
        elif self._direction == "Export":
            return abs(mains_usage) if mains_usage < 0 else 0.0

    @property
    def extra_state_attributes(self) -> dict[str, any]:
        return {
            "device_gid": self._device_gid,
            "scale": self._scale,
            "description": f"Calculated: {self._direction} from Mains CTs",
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
        
        self._attr_device_info = DeviceInfo(
            identifiers={("emporia_vue", str(self._device_gid))},
            name=self._charger_device.device_name or f"Emporia Vue {self._device_gid}",
            manufacturer="Emporia",
            model=self._charger_device.model,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to vehicle battery sensor state changes."""
        await super().async_added_to_hass()
        
        vehicle_soc_sensor = self._config_entry.options.get("vehicle_soc_sensor")
        if vehicle_soc_sensor:
            # Re-evaluate calculation whenever the vehicle SoC sensor changes
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

        # 1. Energy Needed
        target_soc = 100.0
        percent_needed = max(target_soc - current_soc, 0.0)
        kwh_needed = (percent_needed / 100.0) * battery_capacity

        # 2. Get current charger rate from HA state (default to 40A @ 240V if missing)
        amps_entity = f"number.emporia_vue_charger_current_{self._device_gid}"
        amps_state = self.hass.states.get(amps_entity)
        amps = float(amps_state.state) if amps_state and amps_state.state.replace('.', '', 1).isdigit() else 40.0

        charge_rate_kw = (amps * 240.0) / 1000.0

        if charge_rate_kw <= 0:
            return 0.0

        # 3. Hours needed (with 10% charging loss buffer)
        hours_needed = (kwh_needed / charge_rate_kw) * 1.1
        return round(hours_needed, 2)
