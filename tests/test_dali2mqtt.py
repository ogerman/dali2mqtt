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
