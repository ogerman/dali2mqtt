"""Tests for colour control gear: detection, colour temperature, HA discovery."""

import json
from unittest import mock

import pytest
from dali.address import Group, Short

from dali2mqtt.bus import Bus
from dali2mqtt.colour import COLOR_TEMP, RGBWAF
from dali2mqtt.lamp import Lamp
from tests.fakes import FakeGear


def make_lamp(name="lamp", addr=1, **kwargs):
    device = FakeGear(**kwargs)
    return Lamp("debug", Bus(device), name, Short(addr)), device


def sent_names(device):
    return [type(c).__name__ for c in device.sent]


def test_plain_gear_has_no_colour():
    lamp, _ = make_lamp(device_types=(6,))

    assert lamp.device_types == [6]
    assert lamp.colour is None
    assert not lamp.has_colour_temp
    assert "clr_temp_cmd_t" not in json.loads(lamp.gen_ha_config("t"))


def test_tunable_white_gear_is_detected_and_discovered():
    lamp, _ = make_lamp(device_types=(8,), tc_range=(153, 370), tc=250)

    assert lamp.device_types == [8]
    assert lamp.colour.types == {COLOR_TEMP}
    assert lamp.colour_temp_range == (153, 370)
    assert lamp.colour_temp == 250
    config = json.loads(lamp.gen_ha_config("t"))
    assert config["clr_temp_cmd_t"] == "t/lamp/light/color_temp/set"
    assert config["clr_temp_stat_t"] == "t/lamp/light/color_temp/status"
    assert (config["min_mirs"], config["max_mirs"]) == (153, 370)
    assert "colour temperature: 250 (153..370 mirek)" in str(lamp)


def test_gear_with_several_device_types():
    lamp, _ = make_lamp(device_types=(6, 8), tc_range=(153, 370), tc=250)

    assert lamp.device_types == [6, 8]
    assert lamp.has_colour_temp


def test_rgb_gear_without_tc_gets_no_colour_temp():
    lamp, _ = make_lamp(device_types=(8,), rgbwaf_channels=3, auto_activation=True)

    assert lamp.colour.types == {RGBWAF}
    assert lamp.colour.rgbwaf_channels == 3
    assert lamp.colour.auto_activation
    assert not lamp.has_colour_temp
    assert "clr_temp_cmd_t" not in json.loads(lamp.gen_ha_config("t"))
    assert "3 RGBWAF channels" in str(lamp)


def test_set_colour_temp_writes_dtrs_then_activates():
    lamp, device = make_lamp(device_types=(8,), tc_range=(153, 370), tc=250)
    del device.sent[:]

    assert lamp.set_colour_temp(300) == 300

    assert sent_names(device) == [
        "DTR0", "DTR1",
        "EnableDeviceType", "SetTemporaryColourTemperature",
        "EnableDeviceType", "Activate",
    ]
    assert device.tc == 300
    assert lamp.colour_temp == 300


def test_colour_temp_is_clamped_to_the_gear_range():
    lamp, device = make_lamp(device_types=(8,), tc_range=(153, 370), tc=250)

    assert lamp.set_colour_temp(100) == 153
    assert lamp.set_colour_temp(1000) == 370
    assert device.tc == 370


def test_colour_temp_on_plain_gear_is_rejected():
    lamp, device = make_lamp(device_types=(6,))
    del device.sent[:]

    with pytest.raises(ValueError):
        lamp.set_colour_temp(300)
    assert device.sent == []


def test_16_bit_values_use_both_dtr_bytes():
    lamp, device = make_lamp(device_types=(8,), tc_range=(100, 1000), tc=500)
    assert lamp.colour_temp == 500

    lamp.set_colour_temp(770)

    assert device.tc == 770


def test_colour_temp_unknown_while_gear_is_in_another_colour_mode():
    lamp, _ = make_lamp(device_types=(8,), tc_range=(153, 370), tc=None)

    assert lamp.has_colour_temp
    assert lamp.colour_temp is None


def test_group_offers_the_range_all_members_render():
    warm, _ = make_lamp("a", 1, device_types=(8,), tc_range=(153, 370), tc=250)
    cool, _ = make_lamp("b", 2, device_types=(8,), tc_range=(200, 454), tc=250)

    group = Lamp("debug", mock.Mock(), "group_1", Group(1), [warm, cool])

    assert group.colour.types == {COLOR_TEMP}
    assert group.colour_temp_range == (200, 370)
    assert group.has_colour_temp


def test_group_with_a_plain_member_has_no_colour():
    tunable, _ = make_lamp("a", 1, device_types=(8,), tc_range=(153, 370), tc=250)
    plain, _ = make_lamp("b", 2, device_types=(6,))

    group = Lamp("debug", mock.Mock(), "group_1", Group(1), [tunable, plain])

    assert group.colour is None
    assert not group.has_colour_temp


def test_group_colour_temp_is_sent_to_the_group_and_mirrored_to_members():
    a, _ = make_lamp("a", 1, device_types=(8,), tc_range=(153, 370), tc=250)
    b, _ = make_lamp("b", 2, device_types=(8,), tc_range=(200, 454), tc=250)
    device = FakeGear(device_types=(8,), tc_range=(153, 454))
    group = Lamp("debug", Bus(device), "group_1", Group(1), [a, b])

    group.set_colour_temp(180)

    assert type(device.sent[-1]).__name__ == "Activate"
    assert device.sent[-1].destination == Group(1)
    assert group.colour_temp == 200  # clamped to the shared range
    assert a.colour_temp == 200
    assert b.colour_temp == 200


def test_lamp_with_a_mock_driver_still_initializes():
    """Older fixtures answer every query with the same mock; no DT8 there."""
    driver = mock.Mock()
    driver.send = mock.Mock(return_value=mock.Mock(value=100))

    lamp = Lamp("debug", driver, "lamp", Short(1))

    assert lamp.device_types == []
    assert lamp.colour is None


def test_physical_limits_stand_in_for_unset_rendered_limits():
    lamp, _ = make_lamp(device_types=(8,), tc_range=(153, 370), tc_limits_set=False, tc=250)

    assert lamp.colour_temp_range == (153, 370)
    assert lamp.has_colour_temp


# RGBW


def make_rgbw_lamp(name="lamp", addr=3, **kwargs):
    kwargs.setdefault("device_types", (8,))
    kwargs.setdefault("rgbwaf_channels", 4)
    kwargs.setdefault("auto_activation", True)
    return make_lamp(name, addr, **kwargs)


def test_rgbw_gear_is_discovered_with_rgbw_and_colour_mode_topics():
    lamp, _ = make_rgbw_lamp(rgbw=(254, 0, 127, 0))

    assert lamp.has_rgbw
    assert lamp.colour_mode == RGBWAF
    assert lamp.rgbw == (254, 0, 127, 0)
    assert lamp.rgbw_ha == "255,0,128,0"  # 127 of 254 rounds up
    config = json.loads(lamp.gen_ha_config("t"))
    assert config["rgbw_cmd_t"] == "t/lamp/light/rgbw/set"
    assert config["rgbw_stat_t"] == "t/lamp/light/rgbw/status"
    assert config["clrm_stat_t"] == "t/lamp/light/color_mode/status"
    assert lamp.ha_colour_mode == "rgbw"


def test_three_channel_gear_gets_no_rgbw():
    lamp, _ = make_rgbw_lamp(rgbwaf_channels=3)

    assert not lamp.has_rgbw
    assert "rgbw_cmd_t" not in json.loads(lamp.gen_ha_config("t"))


def test_set_rgbw_unlinks_channels_then_writes_levels_and_activates():
    lamp, device = make_rgbw_lamp(tc_range=(153, 370), tc=250)
    del device.sent[:]

    assert lamp.set_rgbw((255, 128, 0, 10)) == (254, 127, 0, 10)

    names = [n for n in sent_names(device) if n != "EnableDeviceType"]
    assert names == [
        "DTR0", "SetTemporaryRGBWAFControl",
        "DTR0", "DTR1", "DTR2", "SetTemporaryRGBDimLevel",
        "DTR0", "DTR1", "DTR2", "SetTemporaryWAFDimLevel",
        "Activate",
    ]
    assert device.rgbwaf_control == 0x80
    assert device.rgbw == (254, 127, 0, 10)
    assert device.active == "rgbw"
    assert lamp.colour_mode == RGBWAF
    assert lamp.ha_colour_mode == "rgbw"
    assert lamp.colour_temp == 250  # last known, kept


def test_colour_temp_after_rgbw_switches_mode_back():
    lamp, device = make_rgbw_lamp(tc_range=(153, 370), rgbw=(254, 0, 0, 0))
    assert lamp.ha_colour_mode == "rgbw"

    lamp.set_colour_temp(200)

    assert device.active == "tc"
    assert lamp.ha_colour_mode == "color_temp"
    assert lamp.rgbw == (254, 0, 0, 0)


def test_rgbw_needs_four_values():
    lamp, _ = make_rgbw_lamp()
    with pytest.raises(ValueError):
        lamp.set_rgbw((1, 2, 3))


def test_rgbw_on_plain_gear_is_rejected():
    lamp, _ = make_lamp(device_types=(6,))
    with pytest.raises(ValueError):
        lamp.set_rgbw((1, 2, 3, 4))


def test_group_rgbw_goes_to_the_group_and_members():
    a, _ = make_rgbw_lamp("a", 1)
    b, _ = make_rgbw_lamp("b", 2)
    device = FakeGear(device_types=(8,), rgbwaf_channels=4)
    group = Lamp("debug", Bus(device), "group_1", Group(1), [a, b])
    assert group.has_rgbw

    group.set_rgbw((0, 255, 0, 0))

    assert device.sent[-1].destination == Group(1)
    assert a.rgbw == b.rgbw == group.rgbw == (0, 254, 0, 0)


def test_update_colour_only_takes_the_active_value():
    from dali2mqtt.colour import ColourState

    lamp, _ = make_rgbw_lamp(tc_range=(153, 370), tc=250)

    assert lamp.update_colour(ColourState(RGBWAF, rgbw=(1, 2, 3, 4)))
    assert lamp.colour_mode == RGBWAF
    assert lamp.colour_temp == 250
    assert not lamp.update_colour(ColourState(RGBWAF, rgbw=(1, 2, 3, 4)))
    assert not lamp.update_colour(None)
    assert not lamp.update_colour(ColourState(None))
