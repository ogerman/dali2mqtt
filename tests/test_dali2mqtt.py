"""Tests for MQTT command handling."""

from unittest import mock

from dali.address import Group, Short

from dali2mqtt.dali2mqtt import on_message_brightness_cmd, on_message_cmd
from dali2mqtt.lamp import Lamp


def make_data_object():
    driver = mock.Mock()
    # Lamp queries: physical minimum, min level, max level, actual level
    driver.send = mock.Mock(
        side_effect=[mock.Mock(value=v) for v in (85, 85, 254, 0)] + [None] * 10
    )
    # initialize_lamps stores lamps by friendly name and groups as group_<n>
    all_lamps = {
        "kitchen": Lamp("debug", driver, "kitchen", Short(0)),
        "group_1": Lamp("debug", driver, "group_1", Group(1)),
    }
    driver.send.reset_mock()
    return {"driver": driver, "base_topic": "dali2mqtt", "all_lamps": all_lamps}


def test_brightness_command_reaches_group():
    data = make_data_object()
    msg = mock.Mock(topic="dali2mqtt/group_1/light/brightness/set", payload=b"200")

    on_message_brightness_cmd(mock.Mock(), data, msg)

    (cmd,), _ = data["driver"].send.call_args
    assert type(cmd).__name__ == "DAPC"
    assert cmd.destination == Group(1)
    assert data["all_lamps"]["group_1"].level == 200


def test_brightness_command_reaches_lamp():
    data = make_data_object()
    msg = mock.Mock(topic="dali2mqtt/kitchen/light/brightness/set", payload=b"150")

    on_message_brightness_cmd(mock.Mock(), data, msg)

    (cmd,), _ = data["driver"].send.call_args
    assert cmd.destination == Short(0)
    # HA brightness 1..170 maps onto kitchen's levels 85..254
    assert data["all_lamps"]["kitchen"].level == 234


def test_lowest_brightness_sets_lamp_minimum():
    """The bottom of the HA slider is the lamp's own minimum, not rejected."""
    data = make_data_object()
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/kitchen/light/brightness/set", payload=b"1")

    on_message_brightness_cmd(client, data, msg)

    assert data["all_lamps"]["kitchen"].level == 85
    client.publish.assert_any_call(
        "dali2mqtt/kitchen/light/brightness/status", 1, retain=True
    )


def test_on_command_sets_max_level():
    """On/off-only devices get ON on the switch topic."""
    data = make_data_object()
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/kitchen/light/switch", payload=b"ON")

    on_message_cmd(client, data, msg)

    (cmd,), _ = data["driver"].send.call_args
    assert type(cmd).__name__ == "DAPC"
    assert data["all_lamps"]["kitchen"].level == 254
    client.publish.assert_called_once_with(
        "dali2mqtt/kitchen/light/status", b"ON", retain=True
    )


def test_firmware_read_is_retried():
    from dali2mqtt.dali2mqtt import read_hasseb_firmware

    driver = mock.Mock()
    driver.readFirmwareVersion = mock.Mock(side_effect=["VERSION_ERROR", "2.3"])
    with mock.patch("time.sleep"):
        assert read_hasseb_firmware(driver) == 2.3

    driver.readFirmwareVersion = mock.Mock(return_value="VERSION_ERROR")
    with mock.patch("time.sleep"):
        assert read_hasseb_firmware(driver) is None


def test_commands_on_home_assistant_topic_names():
    """HA addresses group_1 as group-1, the slug from the discovery config."""
    data = make_data_object()
    client = mock.Mock()

    msg = mock.Mock(topic="dali2mqtt/group-1/light/brightness/set", payload=b"200")
    on_message_brightness_cmd(client, data, msg)
    (cmd,), _ = data["driver"].send.call_args
    assert cmd.destination == Group(1)

    msg = mock.Mock(topic="dali2mqtt/group-1/light/switch", payload=b"OFF")
    on_message_cmd(client, data, msg)
    (cmd,), _ = data["driver"].send.call_args
    assert type(cmd).__name__ == "Off"
    client.publish.assert_called_with("dali2mqtt/group-1/light/status", b"OFF", retain=True)


def make_colour_data_object():
    from dali2mqtt.bus import Bus
    from tests.fakes import FakeGear

    device = FakeGear(device_types=(8,), tc_range=(153, 370), tc=250)
    bus = Bus(device)
    lamp = Lamp("debug", bus, "office", Short(3))
    del device.sent[:]
    return {"driver": bus, "base_topic": "dali2mqtt", "all_lamps": {"office": lamp}}, device


def test_colour_temp_command_sets_the_gear_and_publishes_state():
    from dali2mqtt.dali2mqtt import on_message_color_temp_cmd

    data, device = make_colour_data_object()
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/office/light/color_temp/set", payload=b"300")

    on_message_color_temp_cmd(client, data, msg)

    assert device.tc == 300
    client.publish.assert_any_call(
        "dali2mqtt/office/light/color_temp/status", 300, retain=True
    )
    client.publish.assert_any_call(
        "dali2mqtt/office/light/color_mode/status", "color_temp", retain=True
    )


def test_colour_temp_command_on_plain_lamp_sends_nothing():
    from dali2mqtt.dali2mqtt import on_message_color_temp_cmd

    data = make_data_object()
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/kitchen/light/color_temp/set", payload=b"300")

    on_message_color_temp_cmd(client, data, msg)

    data["driver"].send.assert_not_called()
    client.publish.assert_not_called()


def test_lamp_state_includes_colour_temp():
    from dali2mqtt.dali2mqtt import publish_lamp_state

    data, _ = make_colour_data_object()
    client = mock.Mock()

    publish_lamp_state(client, "dali2mqtt", data["all_lamps"]["office"])

    client.publish.assert_any_call(
        "dali2mqtt/office/light/color_temp/status", 250, retain=True
    )


def test_rgbw_command_sets_the_gear_and_publishes_state_and_mode():
    from dali2mqtt.bus import Bus
    from dali2mqtt.dali2mqtt import on_message_rgbw_cmd
    from tests.fakes import FakeGear

    device = FakeGear(device_types=(8,), rgbwaf_channels=4, auto_activation=True,
                      tc_range=(153, 370), tc=250)
    bus = Bus(device)
    lamp = Lamp("debug", bus, "office-rgb", Short(3))
    data = {"driver": bus, "base_topic": "dali2mqtt", "all_lamps": {"office-rgb": lamp}}
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/office-rgb/light/rgbw/set", payload=b"255,0,0,0")

    on_message_rgbw_cmd(client, data, msg)

    assert device.rgbw == (254, 0, 0, 0)
    published = {call.args[0]: call.args[1] for call in client.publish.call_args_list}
    assert published["dali2mqtt/office-rgb/light/rgbw/status"] == "255,0,0,0"
    assert published["dali2mqtt/office-rgb/light/color_mode/status"] == "rgbw"
    assert published["dali2mqtt/office-rgb/light/color_temp/status"] == 250


def test_bad_rgbw_payload_is_logged_not_sent():
    from dali2mqtt.dali2mqtt import on_message_rgbw_cmd

    data, device = make_colour_data_object()
    client = mock.Mock()
    msg = mock.Mock(topic="dali2mqtt/office/light/rgbw/set", payload=b"1,2,3")

    on_message_rgbw_cmd(client, data, msg)

    assert device.sent == []
    client.publish.assert_not_called()


class ScanBus:
    """Answers QUERY CONTROL GEAR PRESENT for a set of addresses."""

    def __init__(self, present, flaky=(), unaddressed=False):
        from dali.frame import BackwardFrame

        self.present = set(present)
        self.flaky = set(flaky)  # silent on the first query only
        self.unaddressed = unaddressed
        self.frame = BackwardFrame
        self.asked = []

    def send(self, command):
        dest = command.destination
        self.asked.append(dest)
        if isinstance(dest, Short):
            answers = dest.address in self.present
            if dest.address in self.flaky:
                self.flaky.discard(dest.address)
                answers = False
        else:
            answers = self.unaddressed
        return command.response(self.frame(0xFF) if answers else None)


def test_scan_covers_all_64_addresses():
    from dali2mqtt.dali2mqtt import dali_scan

    assert dali_scan(ScanBus({0, 12, 63})) == [0, 12, 63]


def test_scan_asks_a_silent_address_again():
    from dali2mqtt.dali2mqtt import dali_scan

    bus = ScanBus({3, 12}, flaky={12})
    assert dali_scan(bus) == [3, 12]
    assert bus.asked.count(Short(12)) == 2
    assert bus.asked.count(Short(3)) == 1


def test_scan_reports_gear_without_a_short_address(caplog):
    from dali2mqtt.dali2mqtt import dali_scan

    assert dali_scan(ScanBus({1}, unaddressed=True)) == [1]
    assert "without a short address" in caplog.text

    caplog.clear()
    dali_scan(ScanBus({1}))
    assert "without a short address" not in caplog.text


# Colour state across restarts and rescans


def make_unreadable_rgbw_lamp(name="living-room-rgb", addr=12):
    """RGBW gear whose colour readback is useless, like living-room-rgb."""
    from dali2mqtt.bus import Bus
    from tests.fakes import FakeGear

    device = FakeGear(device_types=(8,), rgbwaf_channels=4, min_level=8, level=200,
                      xy_value=(48145, 17388))
    bus = Bus(device)
    return Lamp("debug", bus, name, Short(addr)), bus


def retained(topic, payload):
    return mock.Mock(topic=topic, payload=payload.encode())


def test_unreadable_lamp_takes_its_colour_back_from_the_broker():
    from dali2mqtt.dali2mqtt import on_message_colour_state

    lamp, bus = make_unreadable_rgbw_lamp()
    assert lamp.colour_mode is None  # the stuck xy readback is not taken
    data = {"driver": bus, "base_topic": "dali2mqtt", "all_lamps": {"living-room-rgb": lamp}}
    client = mock.Mock()

    on_message_colour_state(client, data, retained(
        "dali2mqtt/living-room-rgb/light/color_temp/status", "345"))
    on_message_colour_state(client, data, retained(
        "dali2mqtt/living-room-rgb/light/rgbw/status", "255,0,0,0"))
    client.publish.assert_not_called()  # mode still unknown

    on_message_colour_state(client, data, retained(
        "dali2mqtt/living-room-rgb/light/color_mode/status", "color_temp"))

    assert lamp.ha_colour_mode == "color_temp"
    assert lamp.colour_temp == 345
    assert lamp.rgbw == (254, 0, 0, 0)
    client.publish.assert_any_call(
        "dali2mqtt/living-room-rgb/light/color_mode/status", "color_temp", retain=True)

    # Later messages, dali2mqtt's own publishes among them, change nothing.
    client.reset_mock()
    on_message_colour_state(client, data, retained(
        "dali2mqtt/living-room-rgb/light/color_mode/status", "rgbw"))
    assert lamp.ha_colour_mode == "color_temp"
    client.publish.assert_not_called()


def test_retained_colour_is_ignored_by_lamps_that_know_their_own():
    from dali2mqtt.dali2mqtt import on_message_colour_state

    data, device = make_colour_data_object()  # tunable white gear, reads Tc 250
    client = mock.Mock()

    on_message_colour_state(client, data, retained(
        "dali2mqtt/office/light/color_temp/status", "400"))

    assert data["all_lamps"]["office"].colour_temp == 250
    client.publish.assert_not_called()


def test_retained_garbage_and_plain_lamps_are_ignored():
    from dali2mqtt.dali2mqtt import on_message_colour_state

    lamp, bus = make_unreadable_rgbw_lamp()
    data = make_data_object()
    data["all_lamps"]["living-room-rgb"] = lamp
    client = mock.Mock()

    on_message_colour_state(client, data, retained(
        "dali2mqtt/living-room-rgb/light/rgbw/status", "red"))
    on_message_colour_state(client, data, retained(
        "dali2mqtt/kitchen/light/color_mode/status", "rgbw"))
    on_message_colour_state(client, data, retained(
        "dali2mqtt/nowhere/light/color_mode/status", "rgbw"))

    assert lamp.rgbw is None and lamp.colour_mode is None
    client.publish.assert_not_called()


def test_start_up_publishes_discovery_then_retained_state():
    from dali2mqtt.bus import Bus
    from dali2mqtt.dali2mqtt import initialize_lamps
    from tests.fakes import FakeGear

    device = FakeGear(device_types=(8,), rgbwaf_channels=4, min_level=8, level=200)
    names = mock.Mock()
    names.get_friendly_name.return_value = "office-rgb"
    names.is_devices_file_empty.return_value = False
    data = {"driver": Bus(device), "base_topic": "dali2mqtt", "ha_prefix": "homeassistant",
            "log_level": "debug", "devices_names_config": names, "all_lamps": {}}
    client = mock.Mock()

    with mock.patch("dali2mqtt.dali2mqtt.dali_scan", return_value=[3]), \
            mock.patch("dali2mqtt.dali2mqtt.scan_groups", return_value={}):
        initialize_lamps(data, client)

    calls = [(c.args[0], c.kwargs.get("retain")) for c in client.publish.call_args_list]
    topics = [topic for topic, _ in calls]
    assert topics[0] == "homeassistant/light/office-rgb/config"
    assert calls[0][1] is True
    state_topics = ("dali2mqtt/office-rgb/light/status",
                    "dali2mqtt/office-rgb/light/brightness/status")
    for topic in state_topics:
        assert topics.index(topic) > 0
        assert dict(calls)[topic] is True


def rescan_after_colour_temp(gear_reports_back):
    from dali2mqtt.dali2mqtt import initialize_lamps

    old, bus = make_unreadable_rgbw_lamp()
    old.set_colour_temp(300)
    if not gear_reports_back:
        bus.driver.active = "xy"  # keeps claiming its stuck xy point
    names = mock.Mock()
    names.get_friendly_name.return_value = "living-room-rgb"
    names.is_devices_file_empty.return_value = False
    data = {"driver": bus, "base_topic": "dali2mqtt", "ha_prefix": "homeassistant",
            "log_level": "debug", "devices_names_config": names,
            "all_lamps": {"living-room-rgb": old}}

    with mock.patch("dali2mqtt.dali2mqtt.dali_scan", return_value=[12]), \
            mock.patch("dali2mqtt.dali2mqtt.scan_groups", return_value={}):
        initialize_lamps(data, mock.Mock())

    new = data["all_lamps"]["living-room-rgb"]
    assert new is not old
    return new


def test_rescan_keeps_the_colour_of_unreadable_gear():
    new = rescan_after_colour_temp(gear_reports_back=False)
    assert new.ha_colour_mode == "color_temp"
    assert new.colour_temp == 300


def test_rescan_recognises_a_mixed_colour_temperature_read_back_as_rgbw():
    new = rescan_after_colour_temp(gear_reports_back=True)
    assert new.ha_colour_mode == "color_temp"
    assert new.colour_temp == 300


def test_retained_colour_temp_names_the_mixed_levels_the_gear_reports():
    from dali2mqtt.bus import Bus
    from dali2mqtt.colour import colour_temp_to_rgbw, ha_to_dim
    from dali2mqtt.dali2mqtt import on_message_colour_state
    from tests.fakes import FakeGear

    levels = tuple(ha_to_dim(v) for v in colour_temp_to_rgbw(157))
    device = FakeGear(device_types=(8,), rgbwaf_channels=4, min_level=8, level=74,
                      rgbw=levels, status_all_bits=True)
    bus = Bus(device)
    lamp = Lamp("debug", bus, "office-rgb", Short(3))
    data = {"driver": bus, "base_topic": "dali2mqtt", "all_lamps": {"office-rgb": lamp}}
    client = mock.Mock()
    assert lamp.ha_colour_mode == "rgbw"

    on_message_colour_state(client, data, retained(
        "dali2mqtt/office-rgb/light/color_temp/status", "157"))

    assert lamp.ha_colour_mode == "color_temp"
    client.publish.assert_any_call(
        "dali2mqtt/office-rgb/light/color_mode/status", "color_temp", retain=True)
