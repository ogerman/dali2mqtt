"""End to end: emulated hasseb device -> reader thread -> bus -> level sync -> MQTT."""

import struct
import threading
import time
from unittest import mock

import dali.gear.general as gear
from dali.address import Short
from dali.frame import ForwardFrame
from dali.command import from_frame

from dali2mqtt.bus import Bus
from dali2mqtt.dali2mqtt import (
    initialize_lamps,
    on_message_brightness_cmd,
    publish_lamp_state,
)
from dali2mqtt.hasseb_sniffer import SniffingHassebDriver
from dali2mqtt.levelsync import LevelSync


def report(status, seq=0, data=b"", length=None):
    length = len(data) if length is None else length
    payload = data + bytes(3 - len(data))
    return list(struct.pack("BBBBB3sBB", 0xAA, 0x07, seq, status, length, payload, 0, 0))


class FakeHasseb:
    """Enough of the hasseb firmware to answer gear queries and sniff."""

    def __init__(self, gears):
        self.gears = gears  # short address -> dict(min, max, level, groups)
        self.queue = []
        self.lock = threading.Lock()
        self.sniffing = False

    def sniff(self, command):
        """Another master puts a command on the bus."""
        self.apply(command)
        if self.sniffing:
            with self.lock:
                self.queue.append(report(5, data=command.frame.pack))

    def apply(self, command):
        for addr, g in self.gears.items():
            dest = command.destination
            if hasattr(dest, "address") and dest.address != addr:
                continue
            if hasattr(dest, "group") and dest.group not in g["groups"]:
                continue
            if isinstance(command, gear.DAPC):
                g["level"] = 0 if command.power == 0 else min(max(command.power, g["min"]), g["max"])
            elif isinstance(command, gear.Off):
                g["level"] = 0

    def answer(self, command):
        dest = command.destination
        g = self.gears.get(getattr(dest, "address", None))
        if g is None:
            return None
        if isinstance(command, gear.QueryControlGearPresent):
            return 0xFF
        if isinstance(command, gear.QueryPhysicalMinimum):
            return 1
        if isinstance(command, gear.QueryMinLevel):
            return g["min"]
        if isinstance(command, gear.QueryMaxLevel):
            return g["max"]
        if isinstance(command, gear.QueryActualLevel):
            return g["level"]
        if isinstance(command, gear.QueryGroupsZeroToSeven):
            return sum(1 << n for n in g["groups"] if n < 8)
        if isinstance(command, gear.QueryGroupsEightToFifteen):
            return sum(1 << (n - 8) for n in g["groups"] if n >= 8)
        if isinstance(command, gear.QueryFadeTimeFadeRate):
            return 0x07
        return None

    # hid.device interface
    def write(self, data):
        data = bytes(data)
        if data[1] == 0x05:
            self.sniffing = bool(data[3])
            return
        if data[1] != 0x07:
            return
        seq, expect_reply = data[2], data[4]
        command = from_frame(ForwardFrame(16, data[7:9]))
        self.apply(command)
        if expect_reply:
            answer = self.answer(command)
            with self.lock:
                if answer is None:
                    self.queue.append(report(1, seq=seq))
                else:
                    self.queue.append(report(2, seq=seq, data=bytes([answer]), length=1))

    def read(self, length, timeout_ms=0):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
        time.sleep(0.002)
        return report(0)


def wait_for(predicate, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def brightness_published(client, name):
    return [
        call.args[1]
        for call in client.publish.call_args_list
        if call.args[0] == f"dali2mqtt/{name}/light/brightness/status"
    ]


def test_end_to_end_with_emulated_hasseb():
    device = FakeHasseb(
        {
            0: {"min": 85, "max": 254, "level": 0, "groups": [1]},
            1: {"min": 85, "max": 254, "level": 120, "groups": [1]},
        }
    )
    with mock.patch("dali.driver.hasseb.hid") as hid:
        hid.device.return_value = device
        driver = SniffingHassebDriver()
    bus = Bus(driver)
    sync = LevelSync(bus, poll_interval=0.2, fade_margin=0.05)
    driver.on_frame = bus.on_sniffed_frame
    driver.start(sniff=True)
    client = mock.Mock()
    sync.publish = lambda lamp: publish_lamp_state(client, "dali2mqtt", lamp)
    names = mock.Mock()
    names.get_friendly_name = lambda addr: f"lamp{addr}"
    names.is_devices_file_empty = lambda: False
    data_object = {
        "driver": bus,
        "base_topic": "dali2mqtt",
        "ha_prefix": "homeassistant",
        "log_level": "debug",
        "devices_names_config": names,
        "all_lamps": {},
        "sync": sync,
    }
    sync.start()
    try:
        initialize_lamps(data_object, client)
        lamps = data_object["all_lamps"]
        assert set(lamps) == {"lamp0", "lamp1", "group_1"}
        import json
        assert json.loads(lamps["lamp0"].gen_ha_config("dali2mqtt"))["uniq_id"].startswith(
            "SyncHassebDALIUSBDriver_"
        )
        assert lamps["lamp1"].level == 120
        assert lamps["group_1"].level == 120  # derived from members at startup

        # A wall switch sets lamp0 to 200: sniffed, applied, confirmed.
        device.sniff(gear.DAPC(Short(0), 200))
        assert wait_for(lambda: lamps["lamp0"].level == 200)
        assert wait_for(lambda: lamps["group_1"].level == 160)
        assert 116 in brightness_published(client, "lamp0")  # 200 - 85 + 1

        # Home Assistant turns the group off; members follow in the model.
        msg = mock.Mock(topic="dali2mqtt/group_1/light/brightness/set", payload=b"0")
        on_message_brightness_cmd(client, data_object, msg)
        assert lamps["lamp0"].level == 0
        assert lamps["lamp1"].level == 0
        assert device.gears[1]["level"] == 0

        # A change the bus never showed (power cycle) is caught by polling.
        device.gears[1]["level"] = 254
        assert wait_for(lambda: lamps["lamp1"].level == 254)
        assert 170 in brightness_published(client, "lamp1")
        assert lamps["group_1"].level == 254
        assert lamps["lamp0"].fade_time == 0.0
    finally:
        sync.stop()
        driver.stop()
