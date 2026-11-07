"""Hasseb USB DALI master with bus sniffing.

python-dali's ``SyncHassebDALIUSBDriver`` reads the HID device only while it
waits for the answer to a command. Here a dedicated reader thread owns the
device instead: it hands answers to the waiting ``send`` (matched by sequence
number) and passes every sniffed bus frame to ``on_frame(bits, data)``.

The firmware only reports frames from other masters in sniffing mode
(``enableSniffing``); it keeps transmitting our own commands as usual.

Firmware 2.3 garbles answers in sniffing mode: every answer with the lowest
bit set comes back as "invalid answer". So sniffing is paused for queries
and resumed once no query has been sent for ``SNIFF_RESUME_DELAY``. A burst
of queries (a bus scan, a poll sweep) pays for the switch only once. The mode
also survives a restart of dali2mqtt, so ``start`` always sets it explicitly.

Report layout, from the firmware README and the hasseb GUI::

    byte 1   0x07, DALI frame report
    byte 2   sequence number of our command, 0 for sniffed traffic
    byte 3   status: 1 no answer, 2 answer, 3 invalid answer,
             4 answer too early, 5 sniffed frame, 6 sniffed frame with error
    byte 4   data length (the GUI reads it as bytes, the README says bits)
    byte 5+  data
"""
import logging
import threading
import time

from dali.driver.hasseb import (
    HASSEB_DALI_FRAME,
    HASSEB_DRIVER_INVALID_ANSWER,
    HASSEB_DRIVER_NO_DATA_AVAILABLE,
    HASSEB_DRIVER_OK,
    HASSEB_DRIVER_SNIFFER_BYTE,
    HASSEB_DRIVER_SNIFFER_BYTE_ERROR,
    SyncHassebDALIUSBDriver,
)

logger = logging.getLogger(__name__)

REPORT_LENGTH = 10
READ_TIMEOUT_MS = 100
RESPONSE_TIMEOUT = 2.0
SNIFF_RESUME_DELAY = 0.2


class SniffingHassebDriver(SyncHassebDALIUSBDriver):
    """Legacy hasseb driver with a single reader thread and bus sniffing."""

    # Home Assistant unique ids were created with the python-dali class name;
    # keep reporting it so existing entities are not duplicated.
    driver_name = SyncHassebDALIUSBDriver.__name__

    def __init__(self, path=None, on_frame=None):
        super().__init__(path)
        self.on_frame = on_frame
        self._response_ready = threading.Event()
        self._stop = threading.Event()
        self._reader = None
        # Held for a whole command, so sniffing never resumes mid-query.
        self._io_lock = threading.Lock()
        self._sniff_wanted = False
        self._sniff_active = False
        self._last_query = 0.0

    # Lifecycle

    def start(self, sniff=True):
        """Start the reader thread, optionally putting the device in sniff mode."""
        self._sniff_wanted = sniff
        self._set_sniffing(sniff)
        if sniff:
            logger.info("hasseb bus sniffing enabled")
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="hasseb-reader", daemon=True
        )
        self._reader.start()

    def stop(self):
        """Stop the reader thread and leave sniff mode."""
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None
        self._sniff_wanted = False
        try:
            self._set_sniffing(False)
        except Exception:  # pylint: disable=broad-except
            pass

    def _set_sniffing(self, on):
        if on:
            self.enableSniffing()
        else:
            self.disableSniffing()
        if self._sniff_active != on:
            logger.debug("hasseb sniffing %s", "resumed" if on else "paused for queries")
        self._sniff_active = on

    @property
    def reading(self):
        """True while the reader thread owns the device."""
        return self._reader is not None and self._reader.is_alive()

    # Sending

    def send(self, command):
        with self._io_lock:
            if getattr(command, "is_query", False):
                if self._sniff_active:
                    self._set_sniffing(False)
                self._last_query = time.monotonic()
            self._response_ready.clear()
            try:
                return super().send(command)
            finally:
                if getattr(command, "is_query", False):
                    self._last_query = time.monotonic()

    def _maybe_resume_sniffing(self):
        """Turn sniffing back on once the queries have stopped."""
        if not self._sniff_wanted or self._sniff_active:
            return
        if time.monotonic() - self._last_query < SNIFF_RESUME_DELAY:
            return
        # Never block: a command in progress waits for this thread to read.
        if not self._io_lock.acquire(blocking=False):
            return
        try:
            if self._sniff_wanted and not self._sniff_active:
                self._set_sniffing(True)
        except Exception as err:  # pylint: disable=broad-except
            logger.error("Can't resume hasseb sniffing: %s", err)
        finally:
            self._io_lock.release()

    def wait_for_response(self):
        """Wait for the reader thread to deliver the answer to ``_pending``."""
        if not self.reading:
            return super().wait_for_response()
        if not self._response_ready.wait(RESPONSE_TIMEOUT):
            logger.warning("No reply from hasseb device for %s", self._pending)
            self._pending = None
            self._response_message = None

    # Reading

    def _read(self):
        try:
            return self.device.read(REPORT_LENGTH, READ_TIMEOUT_MS)
        except TypeError:  # backend without timeout support
            return self.device.read(REPORT_LENGTH)

    def _read_loop(self):
        while not self._stop.is_set():
            self._maybe_resume_sniffing()
            try:
                data = self._read()
            except Exception as err:  # pylint: disable=broad-except
                logger.error("Reading from hasseb device failed: %s", err)
                time.sleep(0.5)
                continue
            if not data or len(data) < REPORT_LENGTH or data[1] != HASSEB_DALI_FRAME:
                continue
            self._handle_report(bytes(data))

    def _handle_report(self, data):
        status = data[3]
        if status == HASSEB_DRIVER_NO_DATA_AVAILABLE:
            return
        if status == HASSEB_DRIVER_SNIFFER_BYTE_ERROR:
            logger.debug("Sniffed frame with error: %s", data.hex())
            return
        if status == HASSEB_DRIVER_SNIFFER_BYTE:
            length = data[4]
            nbytes = length if length <= 3 else length // 8
            if self.on_frame is not None and nbytes:
                self.on_frame(nbytes * 8, data[5:5 + nbytes])
            return
        # Anything else is the answer to one of our commands.
        if self._pending is None:
            logger.debug("Stray report %s", data.hex())
            return
        if data[2] != self.sn:
            logger.debug("Report for old sequence %d: %s", data[2], data.hex())
            return
        if status in (HASSEB_DRIVER_OK, HASSEB_DRIVER_INVALID_ANSWER):
            self._response_message = data
        else:
            self._response_message = None
        self._pending = None
        self._response_ready.set()
