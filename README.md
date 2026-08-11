# emporia_vue Home Assistant Integration

# MODIFIED for Full Home Assistant Energy support. Coding optimization via GEMINI Pro.
Changes:
- Properly formatted for Energy Category
- Properly added Balance for overage of non-sensors
- Restart registered all sensors
- aSync calls to pyVue
- 12am does not give you a total of yesterday, super annoying with Solar.
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
