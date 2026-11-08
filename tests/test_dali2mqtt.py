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
