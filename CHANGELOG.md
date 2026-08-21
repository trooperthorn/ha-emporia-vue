# Changelog

## Unreleased — Platinum quality-scale rework (session 1)

Session goal: audit the integration against Home Assistant's Platinum
integration quality scale, fix what's fixable, and expand automation
coverage (EV charging, HVAC, weather, general energy). Full rule-by-rule
status lives in `custom_components/emporia_vue/quality_scale.yaml`.

### Fixed

- **Config options `Power Minute Average Sensor` / `Energy Today Sensor` /
  `Energy This Month Sensor` were no-ops.** `sensor.py`'s `add_scale_block`
  hardcoded every sensor to force-enabled regardless of these settings.
  Only Mains/Grid sensors are now force-enabled; everything else respects
  the user's choice.

### Changed — architecture

- Replaced module-level globals (`DEVICE_GIDS`, `DEVICE_INFORMATION`,
  `LAST_MINUTE_DATA`/`LAST_DAY_DATA`/`LAST_MONTH_DATA`, `INVERT_SOLAR`) with
  a `VueRuntimeData` instance scoped to each config entry. Previously this
  only worked because `single_config_entry: true` prevented a second
  account from ever colliding with the shared state.
- Added `coordinator.py`: real `DataUpdateCoordinator` subclasses
  (`VueMinuteCoordinator`, `VueDayCoordinator`, `VueMonthCoordinator`,
  `VueDeviceStatusCoordinator`) instead of raw coordinators built inline in
  `__init__.py`.
- All platforms (`sensor.py`, `switch.py`, `number.py`) now read exclusively
  from `ConfigEntry.runtime_data` — removed the parallel `hass.data[DOMAIN]`
  storage that duplicated the same state.
- Added `diagnostics.py` (redacts auth data from config entry diagnostics,
  reports device/coordinator health).
- Added `PARALLEL_UPDATES`: `0` in `sensor.py` (read-only, coordinator-
  backed), `1` in `switch.py`/`number.py` (issue direct API writes).
- `set_charger_current` service is now unregistered in
  `async_unload_entry` (was previously left registered after unload).
- All user-facing exceptions across `__init__.py`, `switch.py`, `number.py`
  now use `translation_domain`/`translation_key` instead of raw strings or
  a bare re-raised `requests.HTTPError`; added a matching `exceptions`
  section to `strings.json`/`translations/en.json`.
- Removed the unused `VUE_DATA` constant.

### Added

- `quality_scale.yaml` tracking all 36 Bronze/Silver/Gold/Platinum rules
  (done/todo/exempt, with reasoning for each exemption and each
  structurally-blocked Platinum rule).
- `tests/`: config-flow coverage (successful login, invalid auth, cannot
  connect, duplicate account) and unit tests for the pure usage-sign/
  reset-datetime/debounce/mains-split-math helpers in `coordinator.py`,
  using `pytest-homeassistant-custom-component`. *Not executed in the
  authoring sandbox* — installing the HA test harness via pip timed out
  there, so these need a real run (locally or in CI) before being trusted.
- Five new automation blueprints under
  `custom_components/emporia_vue/blueprints/automation/`, documented in
  README:
  - `ev_solar_excess_charging.yaml` — throttle EV charging current to match
    grid export power.
  - `ev_departure_readiness.yaml` — force a full-speed charge only when
    there isn't enough time left before departure to get needed range from
    solar excess alone (uses the integration's own `EV Charge Time Needed`
    sensor and a configurable range-overhead buffer).
  - `hvac_window_door_pause.yaml` — pause climate when any door/window
    contact sensor (ELK-M1 zones, Z-Wave/Zigbee, etc.) is open past a grace
    period, restore once closed.
  - `weather_free_cooling_advisor.yaml` — notify when outdoor conditions
    (Davis, WeatherFlow/Tempest, or any weather integration) beat indoor
    conditions while AC is running; advisory only.
  - `circuit_left_on_alert.yaml` — generic "did I leave something on" alert
    for any power sensor, with optional quiet hours.
- `requirements_test.txt` and `pytest.ini` for running the new test suite.
- README: Configuration Parameters, Actions, Removing the Integration, and
  Automation Blueprints sections — all previously undocumented.

### Downgraded

- `manifest.json`'s `quality_scale` claim: `platinum` → `silver`. The prior
  `platinum` claim was unsubstantiated — there was no `quality_scale.yaml`
  and, on audit, several Gold/Platinum rules weren't met. Silver reflects
  what's actually done as of this session.

### Known gaps (tracked in `quality_scale.yaml`, not addressed this session)

- **Platinum `async-dependency`/`inject-websession`: structurally blocked.**
  `pyemvue`, the required client library, is built on `requests`, not an
  async HTTP client. Every call is wrapped in
  `hass.async_add_executor_job`/`run_in_executor`. Fixing this needs
  `pyemvue` itself to move to `aiohttp`, or this integration to bypass
  `pyemvue` and speak Emporia's Cognito/REST API directly — out of scope
  for this repo alone.
- Gold: `dynamic-devices`, `entity-category`, `repair-issues`, most
  `docs-*` rules, and `entity-translations`/`icon-translations` (partial)
  remain open — not addressed to avoid inventing features/entities beyond
  what was asked.
- Confirmed via research: no Emporia hardware exposes a supported local
  API. A local option exists only via full firmware replacement on Vue 2
  hardware (`emporia-vue-local/esphome`, third-party), which is a different
  integration model entirely and out of scope here.
