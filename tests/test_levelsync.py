"""Tests for the level synchronisation model."""

import time
from unittest import mock

import dali.gear.general as gear
from dali.address import Broadcast, Group, Short
from dali.frame import BackwardFrame

from dali2mqtt.bus import Bus
from dali2mqtt.lamp import Lamp
from dali2mqtt.levelsync import LevelSync


def make_lamp(addr, min_level=85, max_level=254, level=0):
    driver = mock.Mock()
    driver.send = mock.Mock(
        # physical minimum, min, max, actual level, then the DAPC Lamp sends at init
        side_effect=[mock.Mock(value=v) for v in (min_level, min_level, max_level, level)]
        + [None] * 10
    )
    lamp = Lamp("debug", driver, f"lamp {addr}", Short(addr))
    lamp.fade_time = 0.0
    return lamp


class ScriptedDriver:
    """Answers queries from a table of levels per short address."""

    def __init__(self, levels=None, fade_codes=None):
        self.levels = levels or {}
        self.fade_codes = fade_codes or {}
        self.sent = []

    def send(self, command):
        self.sent.append(command)
        if isinstance(command, gear.QueryActualLevel):
            return command.response(BackwardFrame(self.levels[command.destination.address]))
        if isinstance(command, gear.QueryFadeTimeFadeRate):
            code = self.fade_codes.get(command.destination.address, 0)
            return command.response(BackwardFrame(code << 4))
        return None


def make_sync(levels=None, fade_codes=None, poll_interval=0):
    driver = ScriptedDriver(levels, fade_codes)
    bus = Bus(driver)
    publish = mock.Mock()
    sync = LevelSync(bus, publish=publish, poll_interval=poll_interval, fade_margin=0)

    kitchen, hall, bath = make_lamp(0), make_lamp(1, min_level=157), make_lamp(2)
    group = Lamp("debug", mock.Mock(), "group_1", Group(1), [kitchen, hall])
    lamps = {"kitchen": kitchen, "hall": hall, "bath": bath, "group_1": group}
    sync.set_lamps(lamps)
    publish.reset_mock()
    return sync, bus, driver, publish, lamps


def published(publish):
    return [(call.args[0].friendly_name, call.args[0].level) for call in publish.call_args_list]


def test_sniffed_dapc_updates_lamp_and_derived_group():
    sync, bus, _, publish, lamps = make_sync()

    bus.on_sniffed_frame(16, gear.DAPC(Short(0), 200).frame.pack)

    assert lamps["kitchen"].level == 200
    assert lamps["group_1"].level == 200
    assert published(publish) == [("lamp 0", 200), ("group_1", 200)]


def test_group_level_is_mean_of_members_that_are_on():
    sync, bus, _, publish, lamps = make_sync()

    bus.on_sniffed_frame(16, gear.DAPC(Short(0), 100).frame.pack)
    bus.on_sniffed_frame(16, gear.DAPC(Short(1), 200).frame.pack)

    assert lamps["group_1"].level == 150
    assert published(publish)[-1] == ("group_1", 150)


def test_group_command_updates_every_member():
    sync, bus, _, publish, lamps = make_sync()

    bus.on_sniffed_frame(16, gear.DAPC(Group(1), 120).frame.pack)

    assert lamps["kitchen"].level == 120
    # hall's minimum is 157, DALI clamps the request to it
    assert lamps["hall"].level == 157
    assert lamps["bath"].level == 0
    assert lamps["group_1"].level == round((120 + 157) / 2)


def test_off_and_recall_commands():
    sync, bus, _, publish, lamps = make_sync()
    bus.on_sniffed_frame(16, gear.RecallMaxLevel(Broadcast()).frame.pack)
    assert [lamps[n].level for n in ("kitchen", "hall", "bath")] == [254, 254, 254]

    bus.on_sniffed_frame(16, gear.RecallMinLevel(Short(1)).frame.pack)
    assert lamps["hall"].level == 157

    bus.on_sniffed_frame(16, gear.Off(Group(1)).frame.pack)
    assert lamps["kitchen"].level == 0
    assert lamps["hall"].level == 0
    assert lamps["group_1"].level == 0
    assert lamps["bath"].level == 254


def test_own_commands_feed_the_model_too():
    sync, bus, _, publish, lamps = make_sync()

    bus.send(gear.DAPC(Group(1), 200))

    assert lamps["kitchen"].level == 200
    assert lamps["hall"].level == 200
    assert ("group_1", 200) in published(publish)


def test_unknown_outcome_marks_lamp_for_a_confirming_query():
    sync, bus, driver, publish, lamps = make_sync(levels={0: 180})

    bus.on_sniffed_frame(16, gear.GoToScene(Short(0), 3).frame.pack)
    assert lamps["kitchen"].level == 0  # nothing optimistic to say
    publish.assert_not_called()

    sync.start()
    try:
        deadline = time.time() + 2
        while lamps["kitchen"].level != 180 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        sync.stop()

    assert lamps["kitchen"].level == 180
    assert any(isinstance(c, gear.QueryActualLevel) for c in driver.sent)
    assert ("lamp 0", 180) in published(publish)


def test_fade_time_delays_the_confirming_query():
    sync, bus, driver, _, lamps = make_sync(levels={0: 50})
    lamps["kitchen"].fade_time = 0.3

    bus.on_sniffed_frame(16, gear.Up(Short(0)).frame.pack)
    sync.start()
    try:
        time.sleep(0.1)
        assert not any(isinstance(c, gear.QueryActualLevel) for c in driver.sent)
        time.sleep(0.4)
        assert any(isinstance(c, gear.QueryActualLevel) for c in driver.sent)
    finally:
        sync.stop()
    assert lamps["kitchen"].level == 50


def test_poll_catches_changes_the_bus_never_showed():
    sync, bus, driver, publish, lamps = make_sync(levels={0: 77, 1: 0, 2: 0}, poll_interval=0.05)

    sync.start()
    try:
        deadline = time.time() + 2
        while lamps["kitchen"].level != 77 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        sync.stop()

    assert lamps["kitchen"].level == 77
    assert lamps["group_1"].level == 77
    assert ("lamp 0", 77) in published(publish)
    assert ("group_1", 77) in published(publish)


def test_fade_times_are_read_once_from_the_lamps():
    sync, bus, driver, _, lamps = make_sync(levels={0: 0, 1: 0, 2: 0}, fade_codes={0: 4})
    for lamp in ("kitchen", "hall", "bath"):
        lamps[lamp].fade_time = None

    sync.start()
    try:
        deadline = time.time() + 2
        while lamps["bath"].fade_time is None and time.time() < deadline:
            time.sleep(0.01)
    finally:
        sync.stop()

    assert lamps["kitchen"].fade_time == 2.0
    assert lamps["hall"].fade_time == 0.0
    assert sum(isinstance(c, gear.QueryFadeTimeFadeRate) for c in driver.sent) == 3


def test_refresh_ignores_mask_and_failures():
    sync, bus, driver, publish, lamps = make_sync(levels={0: 255})
    assert sync.refresh(lamps["kitchen"]) is None
    assert sync.refresh(lamps["group_1"]) is None
    driver.levels = {}
    assert sync.refresh(lamps["kitchen"]) is None
    publish.assert_not_called()


def test_unreadable_frame_rechecks_every_lamp():
    sync, bus, driver, publish, lamps = make_sync(levels={0: 11, 1: 0, 2: 33})

    bus.on_sniffed_error()
    publish.assert_not_called()

    sync.start()
    try:
        deadline = time.time() + 2
        while lamps["bath"].level != 33 and time.time() < deadline:
            time.sleep(0.01)
    finally:
        sync.stop()

    assert lamps["kitchen"].level == 11
    assert lamps["bath"].level == 33
    assert sum(isinstance(c, gear.QueryActualLevel) for c in driver.sent) == 3


def test_unrelated_commands_are_ignored():
    sync, bus, _, publish, lamps = make_sync()
    bus.on_sniffed_frame(16, gear.SetMaxLevel(Short(0)).frame.pack)
    bus.on_sniffed_frame(16, gear.DAPC(Short(60), 10).frame.pack)
    publish.assert_not_called()


# Colour


def make_colour_sync(**gear_kwargs):
    from tests.fakes import FakeGear

    gear_kwargs.setdefault("device_types", (8,))
    device = FakeGear(**gear_kwargs)
    bus = Bus(device)
    publish = mock.Mock()
    sync = LevelSync(bus, publish=publish, poll_interval=0, fade_margin=0)
    lamp = Lamp("debug", bus, "rgb", Short(3))
    lamp.fade_time = 0.0
    sync.set_lamps({"rgb": lamp})
    publish.reset_mock()
    del device.sent[:]
    return sync, bus, device, publish, lamp


def test_activate_marks_colour_for_re_reading():
    sync, bus, device, publish, lamp = make_colour_sync(rgbwaf_channels=4, rgbw=(0, 0, 0, 0))
    from dali.gear.colour import Activate

    bus.on_sniffed_frame(16, gear.EnableDeviceType(8).frame.pack)
    bus.on_sniffed_frame(16, Activate(Short(3)).frame.pack)

    assert 3 in sync._dirty
    assert 3 in sync._colour_dirty


def test_level_command_dirties_colour_only_with_auto_activation():
    sync, bus, _, _, _ = make_colour_sync(
        rgbwaf_channels=4, rgbw=(0, 0, 0, 0), auto_activation=True
    )
    bus.on_sniffed_frame(16, gear.DAPC(Short(3), 100).frame.pack)
    assert 3 in sync._colour_dirty

    sync, bus, _, _, _ = make_colour_sync(
        rgbwaf_channels=4, rgbw=(0, 0, 0, 0), auto_activation=False
    )
    bus.on_sniffed_frame(16, gear.DAPC(Short(3), 100).frame.pack)
    assert 3 not in sync._colour_dirty
    bus.on_sniffed_frame(16, gear.GoToScene(Short(3), 1).frame.pack)
    assert 3 in sync._colour_dirty


def test_refresh_with_colour_reads_the_active_type_and_publishes():
    sync, bus, device, publish, lamp = make_colour_sync(
        rgbwaf_channels=4, tc_range=(153, 370), tc=250
    )
    assert lamp.colour_mode == "color_temp"

    # Another master switched the gear to a colour.
    device.rgbw = (200, 10, 0, 0)
    device.active = "rgbw"
    sync.refresh(lamp, with_colour=True)

    assert lamp.colour_mode == "rgbwaf"
    assert lamp.rgbw == (200, 10, 0, 0)
    assert lamp.colour_temp == 250
    publish.assert_called_once_with(lamp)

    publish.reset_mock()
    sync.refresh(lamp, with_colour=True)
    publish.assert_not_called()


def test_due_hands_out_colour_flags():
    sync, bus, device, publish, lamp = make_colour_sync(rgbwaf_channels=4, rgbw=(0, 0, 0, 0))
    from dali.gear.colour import Activate

    bus.send(Activate(Short(3)))
    time.sleep(0.01)
    with sync._lock:
        _, due, full = sync._due()
    assert full is False
    assert due == [(lamp, True)]
    assert sync._colour_dirty == set()

    bus.send(gear.DAPC(Short(3), 100))
    time.sleep(0.01)
    with sync._lock:
        _, due, _ = sync._due()
    assert due == [(lamp, False)]


def test_group_colour_follows_members_when_they_agree():
    from dali2mqtt.colour import ColourState

    sync, bus, device, publish, lamps = make_sync()
    a, b = lamps["kitchen"], lamps["hall"]
    for lamp in (a, b):
        lamp.colour = mock.Mock(
            types={"rgbwaf"}, primaries=0, rgbwaf_channels=4, auto_activation=True
        )
    group = lamps["group_1"]
    group.colour, group.colour_temp_range = Lamp._group_colour([a, b])

    sync._set_colour(a, ColourState("rgbwaf", rgbw=(1, 2, 3, 4)))
    assert group.colour_mode is None  # b still unknown

    sync._set_colour(b, ColourState("rgbwaf", rgbw=(1, 2, 3, 4)))
    assert group.colour_mode == "rgbwaf"
    assert group.rgbw == (1, 2, 3, 4)
    assert publish.call_args_list[-1].args[0] is group
