"""Constants for the Emporia Vue integration."""

import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

DOMAIN = "emporia_vue"
AUTH_METHOD = "auth_method"
AUTH_METHOD_EMAIL_PASSWORD = "email_password"
AUTH_METHOD_TOKENS = "tokens"
CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
ENABLE_1S = "enable_1s"
ENABLE_1M = "enable_1m"
ENABLE_1D = "enable_1d"
ENABLE_1MON = "enable_1mon"
SOLAR_INVERT = "solar_invert"
CUSTOMER_GID = "customer_gid"
CONFIG_TITLE = "title"

# Channel numbers that represent the physical Mains/Grid CTs on a Vue unit,
# rather than a monitored branch circuit. Per-channel sensors for these are
# always created and always default-enabled regardless of ENABLE_1M/1D/1MON.
MAINS_CHANNEL_NUMS = frozenset(
    {"1", "2", "3", "1,2,3", "Balance", "MainsFromGrid", "MainsToGrid"}
)

# The single combined 3-phase mains channel used to derive the Grid
# Import/Export split (see VueMainsSplitSensor in sensor.py).
MAINS_COMBINED_CHANNEL_NUM = "1,2,3"

# Synthetic channel_num labels used internally for the derived Import/Export
# entries injected into coordinator data. These are not real Emporia channels
# and must be excluded from the generic per-channel sensor loop in sensor.py.
MAINS_SPLIT_CHANNEL_IMPORT = "MainsImport"
MAINS_SPLIT_CHANNEL_EXPORT = "MainsExport"
MAINS_SPLIT_CHANNELS = frozenset({MAINS_SPLIT_CHANNEL_IMPORT, MAINS_SPLIT_CHANNEL_EXPORT})

AUTH_METHOD_SCHEMA = vol.Schema(
    {
        vol.Required(AUTH_METHOD, default=AUTH_METHOD_EMAIL_PASSWORD): vol.In(
            {
                AUTH_METHOD_EMAIL_PASSWORD: "Emporia email and password",
                AUTH_METHOD_TOKENS: "Emporia tokens (Google/SSO accounts)",
            }
        ),
    }
)

CONFIG_OPTIONS_SCHEMA = {
    vol.Optional(ENABLE_1M, default=True): cv.boolean,
    vol.Optional(ENABLE_1D, default=True): cv.boolean,
    vol.Optional(ENABLE_1MON, default=True): cv.boolean,
    vol.Optional(SOLAR_INVERT, default=True): cv.boolean,
}

CONFIG_FLOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        **CONFIG_OPTIONS_SCHEMA,
    }
)

TOKEN_CONFIG_FLOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID_TOKEN): cv.string,
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_REFRESH_TOKEN): cv.string,
        **CONFIG_OPTIONS_SCHEMA,
    }
)
