"""The Somfy CUL integration."""

from __future__ import annotations

import logging
import os
from typing import Any

import serial
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_BAUD_RATE, CONF_CUL_PATH, DATA_SOMFY_CUL, DOMAIN
from .cul import Cul

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.COVER]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: {
            vol.Optional(CONF_CUL_PATH, default="/dev/ttyAMA0"): cv.string,
            vol.Optional(CONF_BAUD_RATE, default=38400): vol.Coerce(int),
        }
    },
    extra=vol.ALLOW_EXTRA,
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """

    return True, None

    if os.path.exists(data[CONF_CUL_PATH]):
        if os.access(data[CONF_CUL_PATH], os.R_OK | os.W_OK):
            # Return info that you want to store in the config entry.

            try:
                with serial.Serial(
                    data[CONF_CUL_PATH], data[CONF_BAUD_RATE], timeout=1
                ) as ser:
                    return True, None
            except serial.SerialException as e:
                return False, f"Failed to open {data[CONF_CUL_PATH]}: {e}"

        else:
            return (
                False,
                f"Device {data[CONF_CUL_PATH]} exists but does not have read/write permissions.",
            )
    else:
        return False, f"Device {data[CONF_CUL_PATH]} does not exist."


def setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SOMFY CUL component."""

    conf = config.get(DOMAIN, None)
    if not conf:
        _LOGGER.error(
            "SOMFY CUL configuration is missing. Please add a somfy_cul section to your configuration.yaml"
        )
        return False

    cul_path = conf[CONF_CUL_PATH]
    baud_rate = int(conf.get(CONF_BAUD_RATE, 38400))
    cul = None

    # Create API instance
    try:
        cul = Cul(cul_path, baud_rate)
    except ValueError as e:
        _LOGGER.error("Could not connect to CUL")

    # Validate the API connection (and authentication)
    # version = cul.get_cul_version()
    # if not isinstance(version, str) or len(version) < 1:
    #     _LOGGER.error("Could not read CUL version")
    #     return False

    # _LOGGER.info("CUL version %s", version)

    # Store an API object for your platforms to access

    hass.data[DOMAIN] = {DATA_SOMFY_CUL: cul}

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
