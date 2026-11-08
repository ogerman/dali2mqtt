"""Tests for colour control gear: detection, colour temperature, HA discovery."""

import json
from unittest import mock

import pytest
from dali.address import Group, Short

from dali2mqtt.bus import Bus
from dali2mqtt.colour import COLOR_TEMP, RGBWAF, ha_to_dim
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


def test_garbage_limits_give_the_default_range():
    """office-rgb answers MASK for its limits and ~65200 for the physical ones."""
    lamp, _ = make_lamp(
        device_types=(8,), tc_range=(153, 370), tc_limits_set=False,
        tc_physical_range=(65166, 65268), tc=250,
    )

    assert lamp.colour_temp_range == (153, 370)
    assert lamp.has_colour_temp


# RGBW


def make_rgbw_lamp(name="lamp", addr=3, **kwargs):
    kwargs.setdefault("device_types", (8,))
    kwargs.setdefault("rgbwaf_channels", 4)
    kwargs.setdefault("auto_activation", True)
    kwargs.setdefault("min_level", 8)  # like the RGB lamps on the real bus
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

    assert lamp.ha_colour_mode == "color_temp"
    assert lamp.colour_temp == 200
    assert lamp.rgbw == (254, 0, 0, 0)  # the last colour chosen, kept for the picker


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


# Brightness is kept across colour changes


def commands_after(device, mark):
    return [c for c in device.sent[mark:] if type(c).__name__ != "EnableDeviceType"]


def test_colour_is_activated_even_when_the_gear_claims_auto_activation():
    """office-rgb claims automatic activation, yet a DAPC alone left its colour."""
    lamp, device = make_rgbw_lamp(level=63, auto_activation=True)
    mark = len(device.sent)

    lamp.set_rgbw((255, 0, 0, 0))

    sent = commands_after(device, mark)
    assert [type(c).__name__ for c in sent][-2:] == ["Activate", "DAPC"]
    assert sent[-1].power == 63
    assert device.rgbw == (254, 0, 0, 0)
    assert device.level == 63


def test_colour_on_a_lit_lamp_without_auto_activation_activates_then_restores():
    lamp, device = make_rgbw_lamp(level=63, auto_activation=False)
    mark = len(device.sent)

    lamp.set_rgbw((0, 255, 0, 0))

    names = [type(c).__name__ for c in commands_after(device, mark)]
    assert names[-2:] == ["Activate", "DAPC"]
    assert device.rgbw == (0, 254, 0, 0)
    assert device.level == 63


def test_colour_on_an_off_lamp_only_activates():
    lamp, device = make_rgbw_lamp(level=0)
    mark = len(device.sent)

    lamp.set_rgbw((0, 0, 255, 0))

    names = [type(c).__name__ for c in commands_after(device, mark)]
    assert names[-1] == "Activate"
    assert "DAPC" not in names
    assert device.level == 0


def test_colour_temp_restores_the_level_too():
    lamp, device = make_lamp(device_types=(8,), tc_range=(153, 370), tc=250, level=120,
                             auto_activation=True)
    mark = len(device.sent)

    lamp.set_colour_temp(300)

    sent = commands_after(device, mark)
    assert [type(c).__name__ for c in sent][-1] == "DAPC"
    assert device.tc == 300
    assert device.level == 120


def test_group_colour_activates_the_group_and_restores_each_lit_member():
    a, _ = make_rgbw_lamp("a", 1, level=60)
    b, _ = make_rgbw_lamp("b", 2, level=0)
    c, _ = make_rgbw_lamp("c", 4, level=200)
    device = FakeGear(device_types=(8,), rgbwaf_channels=4)
    group = Lamp("debug", Bus(device), "group_1", Group(1), [a, b, c])
    mark = len(device.sent)

    group.set_rgbw((255, 255, 255, 0))

    sent = commands_after(device, mark)
    tail = [(type(cmd).__name__, cmd.destination) for cmd in sent[-3:]]
    assert tail == [("Activate", Group(1)), ("DAPC", Short(1)), ("DAPC", Short(4))]
    assert [cmd.power for cmd in sent[-2:]] == [60, 200]


# Readback that can't be right is ignored


def test_garbage_xy_readback_is_ignored():
    from dali2mqtt.colour import read_colour_state

    _, device = make_rgbw_lamp(xy_value=(65194, 65243))
    assert read_colour_state(Bus(device), Short(3)) is None


def test_real_xy_readback_is_kept():
    from dali2mqtt.colour import XY, read_colour_state

    _, device = make_rgbw_lamp(xy_value=(48145, 17388))  # saturated red
    state = read_colour_state(Bus(device), Short(3))
    assert state.mode == XY
    assert state.xy == (48145, 17388)


def test_commanded_colour_survives_a_garbage_readback():
    from dali2mqtt.colour import read_colour_state

    lamp, device = make_rgbw_lamp(xy_value=(65194, 65243), level=63)
    assert lamp.colour_mode is None  # nothing believable at start-up

    lamp.set_rgbw((255, 0, 0, 0))
    device.active = "xy"  # the gear keeps claiming xy
    assert not lamp.update_colour(read_colour_state(lamp.driver, lamp.short_address))

    assert lamp.colour_mode == RGBWAF
    assert lamp.rgbw == (254, 0, 0, 0)
    assert lamp.ha_colour_mode == "rgbw"


def test_out_of_range_colour_temp_readback_is_ignored():
    from dali2mqtt.colour import read_colour_state

    _, device = make_lamp(device_types=(8,), tc_range=(153, 370), tc=65200)
    assert read_colour_state(Bus(device), Short(1)) is None


def test_colour_detection_survives_lost_answers():
    from tests.test_lamp import LossyGear

    driver = LossyGear(
        {"QueryDeviceType": 2, "QueryColourTypeFeatures": 2},
        device_types=(8,), rgbwaf_channels=4, min_level=8,
    )

    lamp = Lamp("debug", Bus(driver), "living-room-rgb", Short(12))

    assert lamp.device_types == [8]
    assert lamp.has_rgbw


# Colour temperature mixed from RGBW


def test_black_body_colours():
    from dali2mqtt.colour import kelvin_to_rgb

    assert all(c >= 250 for c in kelvin_to_rgb(6600))  # about white
    warm, cool = kelvin_to_rgb(2700), kelvin_to_rgb(6500)
    assert warm[0] == 255 and warm[2] < warm[1] < 255
    assert cool[2] > warm[2] and cool[1] > warm[1]


def test_mixed_white_gets_cooler_and_less_w_towards_6500k():
    from dali2mqtt.colour import colour_temp_to_rgbw

    warmest = colour_temp_to_rgbw(500)
    middle = colour_temp_to_rgbw(250)
    coolest = colour_temp_to_rgbw(153)
    assert warmest[3] == 255 and coolest[3] == 0
    assert warmest[3] > middle[3] > coolest[3]
    assert warmest[2] < middle[2] < coolest[2]  # blue rises
    assert colour_temp_to_rgbw(1000) == warmest  # clamped to the range


def test_rgbw_gear_mixes_colour_temperature_from_its_channels():
    """office-rgb and living-room-rgb render every native Tc as one warm white."""
    from dali2mqtt.colour import colour_temp_to_rgbw, ha_to_dim

    lamp, device = make_rgbw_lamp(tc_range=(153, 370), rgbw=(254, 0, 0, 0), level=100)
    assert lamp.colour_temp_emulated
    config = json.loads(lamp.gen_ha_config("t"))
    assert (config["min_mirs"], config["max_mirs"]) == (153, 500)
    mark = len(device.sent)

    lamp.set_colour_temp(400)

    names = [type(c).__name__ for c in device.sent[mark:]]
    assert "SetTemporaryColourTemperature" not in names
    assert "SetTemporaryRGBDimLevel" in names and "Activate" in names
    assert device.rgbw == tuple(ha_to_dim(v) for v in colour_temp_to_rgbw(400))
    assert device.level == 100
    assert lamp.ha_colour_mode == "color_temp"
    assert "mixed from RGBW" in str(lamp)


def test_tunable_white_gear_keeps_its_own_colour_temperature():
    lamp, device = make_lamp(device_types=(8,), tc_range=(153, 370), tc=250)
    assert not lamp.colour_temp_emulated

    lamp.set_colour_temp(300)

    assert any(type(c).__name__ == "SetTemporaryColourTemperature" for c in device.sent)
    assert device.tc == 300


def test_readback_of_the_mixed_levels_keeps_colour_temp_mode():
    from dali2mqtt.colour import read_colour_state

    lamp, device = make_rgbw_lamp(rgbw=(0, 0, 0, 0))
    lamp.set_colour_temp(250)
    assert device.active == "rgbw"  # what a well-behaved gear reports back

    assert not lamp.update_colour(read_colour_state(lamp.driver, lamp.short_address))
    assert lamp.ha_colour_mode == "color_temp"
    assert lamp.colour_temp == 250

    device.rgbw = (254, 0, 0, 0)  # someone else picked red
    assert lamp.update_colour(read_colour_state(lamp.driver, lamp.short_address))
    assert lamp.ha_colour_mode == "rgbw"


def test_group_of_rgbw_lamps_mixes_colour_temperature():
    a, _ = make_rgbw_lamp("a", 1)
    b, _ = make_rgbw_lamp("b", 2)
    device = FakeGear(device_types=(8,), rgbwaf_channels=4)
    group = Lamp("debug", Bus(device), "group_1", Group(1), [a, b])

    assert group.colour_temp_range == (153, 500)
    group.set_colour_temp(300)

    assert not any(type(c).__name__ == "SetTemporaryColourTemperature" for c in device.sent)
    assert a.colour_temp == b.colour_temp == 300
    assert a.ha_colour_mode == "color_temp"


def test_readback_in_a_mode_home_assistant_is_not_offered_is_ignored():
    from dali2mqtt.colour import XY, ColourState

    lamp, _ = make_rgbw_lamp(rgbw=(254, 0, 0, 0))
    assert lamp.ha_colour_mode == "rgbw"

    assert not lamp.update_colour(ColourState(XY, xy=(48145, 17388)))
    assert lamp.ha_colour_mode == "rgbw"


def test_seed_colour_only_fills_an_unknown_state():
    lamp, _ = make_rgbw_lamp(xy_value=(48145, 17388))
    assert lamp.colour_mode is None

    assert not lamp.seed_colour(colour_temp=300)
    assert lamp.seed_colour(mode="color_temp")
    assert lamp.colour_temp == 300
    assert not lamp.seed_colour(mode="rgbwaf", rgbw=(1, 2, 3, 4))
    assert lamp.ha_colour_mode == "color_temp"


def test_rgbw_switches_off_leftover_amber_and_freecolour():
    """Amber drives the white LED on the real RGBW gear; "no change" kept it lit."""
    lamp, device = make_rgbw_lamp()
    assert device.amber == 254

    lamp.set_rgbw((255, 0, 0, 128))

    assert (device.amber, device.free) == (0, 0)
    assert device.rgbw == (254, 0, 0, 127)  # 128 of 255 is dim level 127


# Reading the colour the gear shows


def test_boot_reads_rgbw_from_gear_with_every_status_bit_set():
    """office-rgb: status 0xF0, report colour type RGBWAF, levels as single bytes."""
    lamp, _ = make_rgbw_lamp(rgbw=(254, 251, 246, 3), status_all_bits=True)

    assert lamp.colour_mode == RGBWAF
    assert lamp.rgbw == (254, 251, 246, 3)


def test_single_byte_levels_do_not_depend_on_dtr0():
    """living-room-rgb answers the level and leaves DTR0 at 0."""
    lamp, _ = make_rgbw_lamp(rgbw=(254, 0, 12, 206), status_all_bits=True, echo_byte=False)

    assert lamp.rgbw == (254, 0, 12, 206)


def test_ambiguous_status_without_report_type_gives_no_state():
    from dali2mqtt.colour import query_colour_mode

    _, device = make_rgbw_lamp(rgbw=(1, 2, 3, 4), status_all_bits=True)
    device._colour_type = lambda: 0  # no usable report colour type

    assert query_colour_mode(Bus(device), Short(3)) is None


def test_poll_picks_up_a_colour_set_from_a_wall_panel():
    from dali2mqtt.colour import read_colour_state

    lamp, device = make_rgbw_lamp(rgbw=(254, 251, 246, 3), status_all_bits=True)
    device.rgbw = (0, 254, 40, 100)  # the panel's frames were unreadable

    assert lamp.update_colour(read_colour_state(lamp.driver, lamp.short_address))
    assert lamp.rgbw == (0, 254, 40, 100)
    assert lamp.rgbw_ha == "0,255,40,100"


def test_reported_levels_of_a_mixed_colour_temperature_are_recognised():
    from dali2mqtt.colour import colour_temp_to_rgbw

    levels = tuple(ha_to_dim(v) for v in colour_temp_to_rgbw(157))
    lamp, _ = make_rgbw_lamp(rgbw=levels, status_all_bits=True)
    assert lamp.colour_mode == RGBWAF

    assert not lamp.recognise_colour_temp(300)  # other levels
    assert lamp.recognise_colour_temp(157)
    assert lamp.ha_colour_mode == "color_temp"
    assert lamp.colour_temp == 157
