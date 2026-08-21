# emporia_vue Home Assistant Integration

# MODIFIED for Full Home Assistant Energy support. Coding optimization via GEMINI Pro.
Changes:
- EV Charging Slider. It works you can watch it change in the app.
- EV Automation Blueprints!!!
- Properly formatted for Energy Category
- Properly added Balance for overage of non-sensors
- Restart registered all sensors
- aSync calls to pyVue
- 12am does not give you a total of yesterday, blame Solar and HA. HA doesnt want negative values.
- EV is more usable. Dynamic Solar Charging Automation; I want to be able to set the Amp values. My electric monthly bill is less than a McDonalds meal for 2.
  

Reads data from the Emporia Vue energy monitor. Creates a sensor for each device channel showing average usage over each minute.

Note: This project is not associated with or endorsed by Emporia Energy.

Data is pulled from the Emporia API using the [PyEmVue python module](https://github.com/magico13/PyEmVue), also written by me.

![ha_example](images/ha_example.png)

## Installation with HACS

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The simplest way to install this integration is with the Home Assistant Community Store (HACS). This is not (yet) part of the default store and will need to be added as a custom repository.

Setting up a custom repository is done by:

1. Go into HACS from the side bar.
2. Click into Integrations.
3. Click the 3-dot menu in the top right and select `Custom repositories`
4. In the UI that opens, copy and paste the [url for this github repo](https://github.com/magico13/ha-emporia-vue) into the `Add custom repository URL` field.
5. Set the category to `Integration`.
6. Click the `Add` button.
7. Select Emporia Vue from the list and press the download button.
8. Further configuration is done within the Integrations configuration in Home Assistant. You may need to restart home assistant and clear your browser cache before it appears, try ctrl+shift+r if you don't see it in the configuration list.

![hacs1](images/hacs1.PNG)
![hacs2](images/hacs2.PNG)
![hacs3](images/hacs3.PNG)
![hacs4](images/hacs4.PNG)

## Manual Installation

If you don't want to use HACS or just prefer manual installs, you can install this like any other custom component. Just merge the `custom_components` folder with the one in your Home Assistant config folder and you may need to manually install the PyEmVue library.

## Configuration

Configuration is done directly in the Home Assistant UI, no manual config file editing is required.

1. Go into the Home Assistant `Configuration`
2. Select `Integrations`
3. Click the `+` button at the bottom
4. Search for "Emporia Vue" and add it. If you do not see it in the list, ensure that you have installed the integration.
5. In the UI that opens, enter the email and password used for the Emporia App. If your account uses Google/Apple, see the [Google/Apple Accounts](#googleapple-accounts) section below.
6. Done! You should now have a sensor for each "channel".

   

### Configuration parameters

The setup and reconfigure forms expose these options:

| Option | Effect |
|---|---|
| **Power Minute Average Sensor** (`enable_1m`) | Default-enabled state for per-channel 1-minute power (W) sensors. Mains/Grid sensors are always created and enabled regardless of this setting. |
| **Energy Today Sensor** (`enable_1d`) | Default-enabled state for per-channel "today" energy (kWh) sensors. |
| **Energy This Month Sensor** (`enable_1mon`) | Default-enabled state for per-channel "this month" energy (kWh) sensors, reset on your Emporia billing cycle date. |
| **Invert Values for Solar Circuits** (`solar_invert`) | Emporia sometimes reports solar production as a negative number depending on how the CT clamp is installed. Enable this to flip solar channels positive for the Energy Dashboard. |

All three `enable_*` options only change whether a sensor starts out enabled in the entity registry — every sensor is still created and can be manually enabled at any time from **Settings → Devices & Services → Entities** without needing to reconfigure the integration.

The integration's **Configure** button (separate from reconfigure) also exposes:

| Option | Effect |
|---|---|
| **Vehicle SoC sensor** (`vehicle_soc_sensor`) | An existing HA sensor entity reporting your EV's state of charge (%). When set, an "EV Charge Time Needed" sensor is created for each EV charger, estimating hours remaining to reach 100% at the currently configured charging current. |
| **Battery capacity (kWh)** (`battery_capacity_kwh`) | Your vehicle's usable battery capacity, used for the charge-time estimate above. Defaults to 80 kWh. |

### Actions

This integration registers one custom action beyond the standard switch/number entities: **`emporia_vue.set_charger_current`**. It sets both the charging current (in amps) and on/off state for a target EV charger in a single call — see `services.yaml` for the full field list. It's primarily meant for use from automations/blueprints (see the bundled EV charging blueprints under `blueprints/automation/`) rather than everyday manual use, since the **Current Limit** number entity and charger **switch** entity cover the same functionality individually.

### Google/Apple Accounts

If your Emporia account was created via Sign in with Google or Apple, the easiest solution is to **set an Emporia password** using the create account flow on the Emporia website or app using the same email address as you'd use with Google/Apple. Once set, you can log in using the standard email and password method above.

If you are unable to set a password, the integration also supports token-based authentication. To obtain your tokens:

1. Open [web.emporiaenergy.com](https://web.emporiaenergy.com) in a browser and sign in with Google/Apple.
2. Open your browser's Developer Tools (F12) and go to the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox).
3. Under **IndexedDB** → **com.amplify.awsCognitoAuthPlugin** → **default.store**, look for keys ending in `.hostedUi.idToken`, `.hostedUi.accessToken`, and `.hostedUi.refreshToken` - copy the values of all three, making sure to only keep the values within the quotes (should start with `eyJ` or similar)
4. Use those values in the token authentication step of the integration setup.



## 📊 Configuring the Home Assistant Energy Dashboard

To ensure the Home Assistant Energy Dashboard calculates costs, solar offset, and individual device consumption accurately, you must map the specific sensors created by this integration to their correct logical categories in Home Assistant (**Settings** > **Dashboards** > **Energy**).

### ⚡ Electricity Grid
This section tracks the power crossing your physical utility meter. If you have solar panels, you **must** use the split, strictly positive sensors. Do not use the raw "Mains" sensors here, as the Energy Dashboard cannot process negative numbers.

* **Grid Consumption:** Add `Grid Import Energy (1D)`.
  * *Tip:* Check "Use an entity with current price" or "Use a static price" to track your utility costs.
* **Return to Grid:** Add `Grid Export Energy (1D)`.
  * *Tip:* Enter your utility's net-metering buyback rate here to calculate your offset.

### ☀️ Solar Panels
This section tracks total solar production before it is consumed by your house or sent to the grid.

* **Solar Production:** Add the Emporia channel connected to your solar inverter (e.g., `Solar Energy (1D)` or `Channel 4 Energy (1D)`).
  * *Important Note:* Depending on how your CT clamps are physically installed, Emporia might report this as a negative number. If so, ensure the **Solar Invert** option is checked in the integration's configuration settings so Home Assistant receives a positive value.

### 🔋 Home Battery Storage
*Only use this section if you have a physical home battery system (e.g., Tesla Powerwall, Enphase IQ) monitored by CT clamps. Do not put your EV charger here.*

* **Energy going into the battery:** Add the sensor tracking power flowing to the battery circuit.
* **Energy coming out of the battery:** Add the sensor tracking power discharging from the battery to the house.

### 🔌 Individual Devices
This section breaks down where the power consumed by your house is actually going. This is where you map your Emporia branch circuits.

* **Add Devices:** Select your specific branch circuits (e.g., `EV Charger Energy (1D)`, `HVAC Energy (1D)`, `Water Heater Energy (1D)`).
* **The Unmonitored Balance:** You should also add the `Balance Energy (1D)` sensor. This shows up as a distinct slice of your pie chart, showing exactly how much power is being consumed by wall outlets, lights, and appliances not actively monitored by a dedicated CT clamp.

---

### ⚠️ Critical Setup Rules

1. **Always use the `(1D)` sensors:** The Energy Dashboard requires sensors that track total accumulated energy over time (kWh). If you attempt to use the `(1MIN)` power (Watt) sensors, the dashboard will reject them.
2. **Wait 2 Hours:** Home Assistant’s Long-Term Statistics engine only compiles Energy Dashboard data once an hour. After configuring this, the dashboard will remain blank or show incomplete data for up to two hours while the database builds its first baseline.
3. **Do not duplicate Mains:** Never add the raw Mains sensors to the "Individual Devices" list. Home Assistant automatically calculates your total home consumption mathematically (`Grid Import` + `Solar Production` - `Grid Export`). If you add Mains to the device list, your dashboard will double-count your entire house's consumption.

## Automation Blueprints

`custom_components/emporia_vue/blueprints/automation/` ships ready-to-import blueprints. Import via **Settings → Automations & Scenes → Blueprints → Import Blueprint**, pointing at the raw GitHub URL of the file, or copy it into your own `config/blueprints/automation/` folder. None of these require Emporia-specific entities except where noted — they use generic `sensor`/`switch`/`number`/`climate`/`binary_sensor` selectors so they work with whatever integrations you actually have (ELK-M1, Davis, WeatherFlow/Tempest, your EV integration, etc).

### EV charging

* **EV Charging: Solar Excess Only** (`ev_solar_excess_charging.yaml`) — Throttles charging current to roughly match the power currently flowing back to the grid, so the car only ever draws from surplus solar. Pair with this integration's `Grid Export Power (1MIN)` sensor.
* **EV Charging: Ensure Ready by Departure** (`ev_departure_readiness.yaml`) — A safety net for the blueprint above. Estimates whether the car needs more range than it currently has (with a configurable overhead buffer for weather/detours) and whether there's still enough time before your departure to get that charge from solar excess alone; if not, it forces a full-speed charge instead of waiting. Uses this integration's own `EV Charge Time Needed` sensor.
* **EV Charging: Pre-Sunset Ramp Down** (`ev_predictive_solar.yaml`) — Steps charging current down and then off as the sun sets, to avoid pulling expensive evening grid power.
* **EV Charging: Smart Travel Prompt** (`ev_smart_travel.yaml`) — Sends an actionable notification with charging-speed options when the car is plugged in, checking your calendar for upcoming travel.

### HVAC / climate

* **HVAC: Pause When a Window or Door Opens** (`hvac_window_door_pause.yaml`) — Pauses heating/cooling once any monitored door/window contact sensor (ELK-M1 zones, Z-Wave/Zigbee contacts, etc.) has been open past a grace period, and restores it once everything's closed.
* **HVAC: Free Cooling Advisor** (`weather_free_cooling_advisor.yaml`) — Notifies you when outdoor conditions (from a Davis, WeatherFlow/Tempest, or any weather integration) are actually better than indoor conditions while the AC is running, so opening windows could cool the house for free. Advisory only — nothing is switched automatically.

### Other energy blueprints

* **Energy: Circuit Left On Alert** (`circuit_left_on_alert.yaml`) — Notifies you when a monitored circuit (or the `Balance` unmonitored-load sensor) has been drawing power above a threshold for longer than expected, with optional quiet hours for circuits that are supposed to run overnight.

## Removing the integration

1. Go into **Settings** → **Devices & Services** → **Integrations**.
2. Find **Emporia Vue** and click the 3-dot menu, then **Delete**.
3. This removes the config entry, all of its entities and devices, and unregisters the `emporia_vue.set_charger_current` action. It does not affect your Emporia account or hardware in any way — your Vue monitor keeps reporting to the Emporia app as normal.
4. If you added `emporia_vue.set_charger_current` calls to any automations/scripts/blueprints, remove or update those separately; deleting the integration does not clean up references to it elsewhere in your configuration.
5. If installed via HACS and you don't plan to reinstall, you can also remove it from HACS → Integrations → 3-dot menu → Remove.
