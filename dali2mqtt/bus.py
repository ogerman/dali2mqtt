"""Serialized access to the DALI bus and distribution of bus traffic."""
import logging
import threading
import time
from collections import deque

from dali.command import from_frame
from dali.frame import ForwardFrame

logger = logging.getLogger(__name__)

# A sniffed copy of a frame we sent ourselves within this window is dropped.
ECHO_WINDOW = 0.5


class Bus:
    """Single point of access to the DALI driver.

    Every command goes through ``send`` under one lock, so the MQTT thread,
    the level poller and any other caller never interleave on the device.

    Listeners registered with ``add_listener`` are called as
    ``listener(command, own)`` for every non-query command that went out on
    the bus: the ones we sent (``own=True``) and, on drivers that can sniff,
    the ones other masters sent (``own=False``). A frame the interface saw
    but could not read is reported as ``command=None``.
    """

    def __init__(self, driver):
        self.driver = driver
        # Home Assistant unique ids are built from the driver class name, so a
        # driver may pin the name its entities were created with.
        self.driver_name = getattr(driver, "driver_name", None) or type(driver).__name__
        self._lock = threading.RLock()
        self._listeners = []
        self._recent_own = deque(maxlen=16)  # (frame bytes, monotonic time)

    def send(self, command):
        """Send a command to the bus and return the driver's response."""
        with self._lock:
            response = self.driver.send(command)
        if not getattr(command, "is_query", False):
            try:
                self._recent_own.append((bytes(command.frame.pack), time.monotonic()))
            except Exception:  # pylint: disable=broad-except
                pass
            self._notify(command, True)
        return response

    def add_listener(self, listener):
        """Register ``listener(command, own)`` for bus traffic."""
        self._listeners.append(listener)

    def on_sniffed_frame(self, bits, data):
        """Handle a frame another master put on the bus.

        Only 16 bit forward frames (control gear commands) are of interest:
        8 bit frames are answers, 24 bit frames belong to control devices.
        """
        if bits != 16 or len(data) < 2:
            logger.debug("Ignoring sniffed %d bit frame %s", bits, bytes(data).hex())
            return
        data = bytes(data[:2])
        now = time.monotonic()
        for frame, when in self._recent_own:
            if frame == data and now - when < ECHO_WINDOW:
                logger.debug("Ignoring echo of our own frame %s", data.hex())
                return
        try:
            command = from_frame(ForwardFrame(16, data))
        except Exception as err:  # pylint: disable=broad-except
            logger.debug("Can't decode sniffed frame %s: %s", data.hex(), err)
            return
        if getattr(command, "is_query", False):
            return
        logger.debug("Sniffed %s", command)
        self._notify(command, False)

    def on_sniffed_error(self):
        """A frame went by that the interface could not read.

        Listeners get ``command=None``: something changed, nobody knows what.
        """
        self._notify(None, False)

    def _notify(self, command, own):
        for listener in list(self._listeners):
            try:
                listener(command, own)
            except Exception:  # pylint: disable=broad-except
                logger.exception("Bus listener failed on %s", command)
