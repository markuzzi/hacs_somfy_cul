"""Connect to CUL Device."""

import logging
import os
import sys

import serial

_LOGGER = logging.getLogger(__name__)


class Cul:
    """Helper class to encapsulate serial communication with CUL device."""

    def __init__(self, serial_port, baud_rate=115200, test=False) -> None:
        """Create instance with a given serial port."""

        self.exit_loop = False
        self.serial = None

        if test:
            self.serial = sys.stderr
            self.test = True
        else:
            self.test = False
            if not os.path.exists(serial_port):  # noqa: PTH110
                raise ValueError(f"cannot find CUL device {serial_port}")
            try:
                self.serial = serial.Serial(
                    port=serial_port, baudrate=baud_rate, timeout=1
                )
            except serial.SerialException as e:
                _LOGGER.error("Could not open CUL device: %s", e)

    def get_cul_version(self):
        """Get CUL version."""
        self.serial.write("V\n")
        self.serial.flush()
        return self.serial.readline()

    def send_command(self, command_string):
        """Send command string to serial port with CUL device."""
        if self.test:
            _LOGGER.info(command_string.decode())
        elif self.serial:
            try:
                self.serial.write(command_string)
                self.serial.flush()
            except serial.SerialException as e:
                _LOGGER.error(
                    "Could not send command %s to CUL device: %s", command_string, e
                )
                return False
        else:
            _LOGGER.error(
                "Could not send command %s. SOMFY CUL is not available", command_string
            )
            return False

        return True

    def listen(self, callback):
        """Listen to messages from CUL."""
        while not self.exit_loop:
            # readline() blocks until message is available
            try:
                message = self.serial.readline().decode("utf-8")
                if message:
                    _LOGGER.debug("Received RF message: %s", message)
                callback(message)
            except Exception:  # noqa: BLE001
                pass
