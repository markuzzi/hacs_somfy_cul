"""Cover Platform for the Somfy MyLink component."""

import asyncio
import logging
import os
from threading import Timer
import time
from typing import Any, Final

import aiofiles
import voluptuous as vol
import yaml

from homeassistant.components.cover import (
    ATTR_POSITION,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
    CoverState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    ATTR_CURRENT_POS,
    ATTR_DOWN_TIME,
    ATTR_ENC_KEY,
    ATTR_ROLLING_CODE,
    ATTR_UP_TIME,
    CONF_ADDRESS,
    CONF_NAME,
    CONF_REVERSED,
    CONF_TYPE,
    DATA_SOMFY_CUL,
    DOMAIN,
    MANUFACTURER,
    SERVICE_CLOSE,
    SERVICE_OPEN,
    SERVICE_PROG,
    SERVICE_RELOAD,
    SERVICE_STOP,
)
from .cul import Cul


class Command(vol.Enum):
    """Available SOMFY commands."""

    MY: Final = "10"
    STOP: Final = "10"
    OPEN: Final = "20"
    MY_UP: Final = "30"
    CLOSE: Final = "40"
    MY_DOWN: Final = "50"
    UP_DOWN: Final = "60"
    MY_UP_DOWN: Final = "70"
    PROG: Final = "80"
    WIND_SUN: Final = "90"
    WIND_ONLY: Final = "A0"

    POS: Final = "POS"  # custom


CONFIG_FILE = "somfy_cover_state.yaml"

_LOGGER = logging.getLogger(__name__)
_STATE_FILE_LOCK = asyncio.Lock()

PLATFORM_SCHEMA = COVER_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ADDRESS): cv.string,
        vol.Optional(CONF_TYPE, default=CoverDeviceClass.SHADE): cv.string,
        vol.Optional(CONF_REVERSED, default=False): int,
        vol.Optional(ATTR_UP_TIME): vol.Coerce(int),
        vol.Optional(ATTR_DOWN_TIME): vol.Coerce(int),
    }
)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Discover and configure Somfy covers."""
    somfy_cul_data = hass.data.get(DOMAIN, {})
    somfy_cul = somfy_cul_data[DATA_SOMFY_CUL] or None

    if not somfy_cul:
        _LOGGER.warning("SOMFY CUL device is not available")

    cover_config = {
        "name": config.get(CONF_NAME),
        "device_class": config.get(CONF_TYPE, CoverDeviceClass.SHUTTER),
        "address": config.get(CONF_ADDRESS),
        "up_time": config.get(ATTR_UP_TIME),
        "down_time": config.get(ATTR_DOWN_TIME),
        "reverse": config.get(CONF_REVERSED, False),
    }

    cover = SomfyCulShade(hass, somfy_cul, **cover_config)

    _LOGGER.debug(
        "Adding Somfy Cover: %s with address %s",
        cover_config["name"],
        cover_config["address"],
    )

    add_entities([cover])


class SomfyCulShade(RestoreEntity, CoverEntity):
    """Object for controlling a Somfy cover."""

    _attr_should_poll = False
    _attr_assumed_state = True
    _attr_has_entity_name = True
    _attr_name = None
    _attr_unique_id = None

    _enc_key: int = 1
    _rolling_code: int = 0
    _up_time = None
    _down_time = None

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Flag supported features."""
        supported_features = CoverEntityFeature(0)
        if self._up_time is not None and self._down_time is not None:
            supported_features |= (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
                | CoverEntityFeature.SET_POSITION
            )
        else:
            supported_features |= (
                CoverEntityFeature.OPEN
                | CoverEntityFeature.CLOSE
                | CoverEntityFeature.STOP
            )

        return supported_features

    def __init__(
        self,
        hass: HomeAssistant,
        somfy_cul: Cul,
        address: str,
        up_time: int | None = None,
        down_time: int | None = None,
        name="SomfyCover",
        reverse=False,
        device_class=CoverDeviceClass.SHADE,
    ) -> None:
        """Initialize the cover."""
        self._hass = hass
        self._somfy_cul = somfy_cul

        self._attr_name = name
        self._address = address
        self._reverse = reverse
        self._up_time = up_time
        self._down_time = down_time

        self._drv_timer = None
        self._stop_timer = None
        self._cmd_time = 0

        self._attr_unique_id = address
        self._attr_device_class = device_class
        self._attr_is_closed = True
        self._attr_is_opening = False
        self._attr_is_closing = False

        self._attr_extra_state_attributes = {
            "enc_key": self._enc_key,
            "rolling_code": self._rolling_code,
        }

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            manufacturer=MANUFACTURER,
            name=name,
        )

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        cmd = Command.CLOSE if not self._reverse else Command.OPEN
        self.send_command(cmd)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        cmd = Command.CLOSE if self._reverse else Command.OPEN
        self.send_command(cmd)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        cmd = Command.STOP
        self.send_command(cmd)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        if (position := kwargs.get(ATTR_POSITION)) is None:
            _LOGGER.debug("Argument `position` is missing in set_cover_position")
            return

        cmd = Command.POS
        self.send_command(cmd, int(position))

    def send_command(self, cmd: Command, target_pos=None):
        """Send a command to the CUL."""
        try:
            cmd, time_to_stop = self._update_state(cmd, target_pos)
            self._somfy_cul.send_command(self._command_string(cmd))
            if time_to_stop is not None:
                self._stop_timer = Timer(time_to_stop, self._send_stop_command)
                self._stop_timer.start()
        finally:
            self._increase_rolling_code()
            self._async_save_state()

    def _send_stop_command(self):
        try:
            self._somfy_cul.send_command(self._command_string(Command.STOP))
        finally:
            self._increase_rolling_code()
            self._async_save_state()

    async def async_prog_cover(self):
        """Handle the async_prog_cover service."""
        try:
            self._somfy_cul.send_command(self._command_string(Command.PROG))
        finally:
            self._increase_rolling_code()
            self._async_save_state()

    async def async_added_to_hass(self) -> None:
        """Complete the initialization."""
        await super().async_added_to_hass()
        # Restore the last state
        last_state = await self.async_get_last_state()

        if last_state is not None and last_state.state in (
            CoverState.OPEN,
            CoverState.CLOSED,
        ):
            self._attr_is_closed = last_state.state == CoverState.CLOSED

        if not await self._load_state_from_yaml():
            await self._save_state_to_yaml()  # save initial state

        self.platform.async_register_entity_service(
            SERVICE_PROG, {}, "async_prog_cover"
        )
        self.platform.async_register_entity_service(
            SERVICE_OPEN, {}, "async_open_cover"
        )
        self.platform.async_register_entity_service(
            SERVICE_CLOSE, {}, "async_close_cover"
        )
        self.platform.async_register_entity_service(
            SERVICE_STOP, {}, "async_stop_cover"
        )
        self.platform.async_register_entity_service(
            SERVICE_RELOAD, {}, "async_reload_state"
        )

    async def async_reload_state(self, **kwargs: Any) -> None:
        """Reload the state from the YAML file."""
        await self._load_state_from_yaml()

    async def _save_state_to_yaml(self):
        """Save the current state to the file using self.entity_id as the key."""
        async with _STATE_FILE_LOCK:
            state_data = await self._read_state_yaml()

            # Update state for the current address
            state_data[self.entity_id] = self._get_state()

            # Save updated state back to the file
            state_file_path = self._get_state_file_path()
            async with aiofiles.open(
                state_file_path, mode="w", encoding="utf-8"
            ) as file:
                await file.write(yaml.safe_dump(state_data, default_flow_style=False))

    async def _load_state_from_yaml(self):
        """Load the state for self.entity_id from the file, if it exists."""
        state_data = await self._read_state_yaml()

        # Load state for the current address if it exists
        if self.entity_id in state_data:
            self._set_state(state_data[self.entity_id])
        else:
            return False

        return True

    async def _read_state_yaml(self):
        """Load the state from the file, if it exists."""
        state_file_path = self._get_state_file_path()
        state_data = {}
        if os.path.exists(state_file_path):  # noqa: PTH110
            try:
                async with aiofiles.open(state_file_path, encoding="utf-8") as file:
                    try:
                        content = await file.read()
                        state_data = yaml.safe_load(content) or {}
                    except yaml.YAMLError as e:
                        _LOGGER.error("Error reading YAML file: %s", e)
            except FileNotFoundError:
                _LOGGER.debug(
                    "State YAML file not existing: %s. Creating a new one",
                    state_file_path,
                )
        return state_data

    def _get_state_file_path(self):
        """Get the full path to the state file."""
        return self._hass.config.path(CONFIG_FILE)

    def _get_state(self):
        return {
            ATTR_ENC_KEY: self._enc_key,
            ATTR_ROLLING_CODE: self._rolling_code,
            ATTR_CURRENT_POS: self._attr_current_cover_position,
        }

    def _set_state(self, state):
        """Set the object's state from a dictionary."""
        self._enc_key = state.get(ATTR_ENC_KEY, self._enc_key)
        self._rolling_code = state.get(ATTR_ROLLING_CODE, self._rolling_code)
        self._attr_current_cover_position = state.get(
            ATTR_CURRENT_POS, self._attr_current_cover_position
        )
        self._attr_extra_state_attributes = {
            "enc_key": self._enc_key,
            "rolling_code": self._rolling_code,
        }
        self.schedule_update_ha_state(force_refresh=True)

    def _async_save_state(self):
        self._attr_extra_state_attributes = {
            "enc_key": self._enc_key,
            "rolling_code": self._rolling_code,
        }
        self.schedule_update_ha_state()
        self.hass.loop.call_soon_threadsafe(self._async_save_state_task)

    def _async_save_state_task(self):
        self.hass.async_create_task(self._save_state_to_yaml())

    def _increase_rolling_code(self):
        """Increment rolling_code, roll over when crossing the 16 bit boundary.

        Increment enc_key, roll over when crossing the 4 bit boundary.
        """

        self._rolling_code = (self._rolling_code + 1) % 0x10000
        self._enc_key = (self._enc_key + 1) % 0x10
        self._async_save_state()

        _LOGGER.debug(
            "Next rolling code for device %s is %d, encryption key is %d",
            self._address,
            self._rolling_code,
            self._enc_key,
        )

    def _reset_timer(self):
        """Reset timer functions."""
        if self._drv_timer is not None:
            _LOGGER.debug("Resetting timer")
            self._drv_timer.cancel()
            self._drv_timer = None

    def _write_state(self, cmd: Command, position=None):
        if cmd == Command.OPEN:
            self._write_state_open()
        elif cmd == Command.CLOSE:
            self._write_state_closed()
        elif cmd == Command.POS:
            self._write_state_pos(position)
        else:
            _LOGGER.error("Wrong command %s to write the device state", cmd)

    def _write_state_pos(self, position):
        """Timer function called when shutter has been set to position."""
        _LOGGER.debug("Write state position for device: %s", self._attr_name)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = position >= 99
        self._attr_current_cover_position = position
        self._async_save_state()

    def _write_state_open(self):
        """Timer function called when shutter has been opened."""
        _LOGGER.debug("Write state open for device: %s", self._attr_name)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = False
        self._attr_current_cover_position = 100
        self._async_save_state()

    def _write_state_closed(self):
        """Timer function called when shutter has been closed."""
        _LOGGER.debug("Write state closed for device: %s", self._attr_name)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = True
        self._attr_current_cover_position = 0
        self._async_save_state()

    def _write_state_stopped(self, position=0):
        """Timer function called when shutter has been closed."""
        _LOGGER.debug("Write state stopped for device: %s", self._attr_name)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = False
        self._attr_current_cover_position = position
        self._async_save_state()

    def _calculate_position_command(
        self, target_position: int | None = None
    ) -> tuple[Command, float]:
        cur = self._attr_current_cover_position or 0
        target = target_position or 100

        if target > cur:
            cmd = Command.OPEN
            time_per_step = self._up_time / 100
            time_to_stop = time_per_step * (target - cur)
        elif target < cur:
            cmd = Command.CLOSE
            time_per_step = self._down_time / 100
            time_to_stop = time_per_step * (cur - target)
        else:
            _LOGGER.debug("Already at position")
            return None, None

        return cmd, time_to_stop

    def _start_update_state_timer(
        self, cmd: Command, target_position=None
    ) -> tuple[Command, float]:
        """Start the timer when moving the shutter. Resets the time, when cmd is STOP.

        Returns the time after which the cover must be stoppped in case of a target position
        """
        _LOGGER.debug("Starting a timer for device: %s", self._attr_name)
        self._reset_timer()
        self._cmd_time = time.time()

        # This is the time, after which the cover must be stoppped in case of a target position
        time_to_stop = None

        if cmd == Command.POS:
            cmd, time_to_stop = self._calculate_position_command(target_position)
            if cmd is None or time_to_stop is None:
                _LOGGER.error("Position cannot be set")
                return None, None

            self._drv_timer = Timer(
                time_to_stop, self._write_state_pos, args=(target_position,)
            )
            _LOGGER.debug("POS timer with timeout: %s", time_to_stop)

        elif cmd == Command.OPEN:
            self._attr_is_opening = True
            self._attr_is_closing = False
            self._async_save_state()

            timeout = self._up_time
            if self._attr_current_cover_position is not None:
                cur = self._attr_current_cover_position / 100
                timeout = timeout * (1 - cur) + 1
            self._drv_timer = Timer(timeout, self._write_state_open)
            _LOGGER.debug("OPEN timer with timeout: %s", timeout)

        elif cmd == Command.CLOSE:
            self._attr_is_opening = False
            self._attr_is_closing = True
            self._async_save_state()

            timeout = self._down_time
            if self._attr_current_cover_position is not None:
                cur = self._attr_current_cover_position / 100
                timeout = timeout * (cur) + 1
            self._drv_timer = Timer(timeout, self._write_state_closed)
            _LOGGER.debug("CLOSE timer with timeout: %s", timeout)

        else:
            _LOGGER.warning(
                "Timer can only be started for commands OPEN, CLOSE, and POS, but not for %s",
                cmd,
            )
            return None, None

        self._drv_timer.start()

        return cmd, time_to_stop

    def _update_state(
        self, cmd: Command, target_position=None
    ) -> tuple[Command, float]:
        """Calculate position, publish state and position.

        Returns the time after which the cover must be stoppped in case of a target position
        """
        _LOGGER.debug("Updating state for command: %s", cmd)
        _move_time = None
        if cmd == Command.OPEN:
            _move_time = self._up_time
        elif cmd == Command.CLOSE:
            _move_time = self._down_time
        elif cmd == Command.POS:
            _move_time = min(self._up_time or 0, self._down_time or 0)

        _LOGGER.debug("Move time: %s", _move_time)

        time_to_stop = None

        if cmd in (Command.OPEN, Command.CLOSE, Command.POS):
            if _move_time and _move_time > 0:
                cmd, time_to_stop = self._start_update_state_timer(cmd, target_position)
            else:
                self._write_state(cmd)

        elif cmd == Command.STOP:
            current_pos = (
                50
                if not self._attr_current_cover_position
                else self._attr_current_cover_position
            )
            current_time = time.time()

            if self._drv_timer is not None and (
                self._attr_is_closing or self._attr_is_opening
            ):
                self._reset_timer()
                if self._cmd_time > 0:
                    ti = current_time - self._cmd_time
                    dt = self._up_time if self._attr_is_opening else self._down_time
                    current_pos += int(ti / dt * 100) * (
                        1 if self._attr_is_opening else -1
                    )

            # Make sure that pos is in range 0..100
            current_pos = max(min(current_pos, 100), 0)

            # publish stopped state and calculated position
            self._write_state_stopped(current_pos)
            self._cmd_time = 0

        return cmd, time_to_stop

    def _calculate_checksum(self, command_string: str):
        """Calculate checksum for command string.

        From https://pushstack.wordpress.com/somfy-rts-protocol/ :
        The checksum is calculated by doing a XOR of all nibbles of the frame.
        To generate a checksum for a frame set the 'cks' field to 0 before
        calculating the checksum.
        """
        cmd = bytearray(command_string, "utf-8")
        checksum = 0
        for char in cmd:
            checksum = checksum ^ char ^ (char >> 4)
        checksum = checksum & 0xF
        return f"{checksum:01X}"

    def _command_string(self, cmd: Command):
        """Generate command string.

        A Somfy command is a hex string of the following form: KKC0RRRRSSSSSS.

        KK - Encryption key: First byte always 'A', second byte varies
        C - Command (1 = My, 2 = Up, 4 = Down, 8 = Prog)
        0 - Checksum (set to 0 for calculating checksum)
        RRRR - Rolling code
        SSSSSS - Address (= remote channel)
        """
        command_string = (
            f"A{self._enc_key:01X}{cmd.value}{self._rolling_code:04X}{self._address}"
        )

        chksum = self._calculate_checksum(command_string)

        command_string = command_string[:3] + chksum + command_string[4:]
        _LOGGER.debug(
            "Generated string %s from command %s for device %s",
            command_string,
            cmd.value,
            self.name,
        )
        _LOGGER.debug(
            "ENC Key: %s, CMD: %s, CHKSUM: %s, Rolling Code: %s, Address: %s",
            self._enc_key,
            cmd.value,
            chksum,
            self._rolling_code,
            self._address,
        )
        command_string = "Ys" + command_string + "\n"
        return command_string.encode()
