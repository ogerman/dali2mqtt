"""Per-lamp settings from the devices file: validation and effect."""

import json
from unittest import mock

import pytest
from dali.address import Group, Short

from dali2mqtt.bus import Bus
from dali2mqtt.colour import ColourMix, colour_temp_to_rgbw, ha_to_dim
from dali2mqtt.devicesnamesconfig import DevicesNamesConfig
from dali2mqtt.lamp import Lamp
from tests.fakes import FakeGear

# Arlight RT-B60-10mm 24V RGBW-Warm on MiBoxer DL-X, matched by eye.
NF_RGB = {
    "friendly_name": "office-rgb",
    "colour": {"white_points": {2700: [0, 0, 0, 255], 6500: [240, 210, 255, 200]}},
}


def devices_file(tmp_path, content):
    path = tmp_path / "devices.yaml"
    path.write_text(content)
    return DevicesNamesConfig("debug", str(path))


def rgbw_lamp(settings=None, addr=3, **kwargs):
    kwargs.setdefault("device_types", (8,))
    kwargs.setdefault("rgbwaf_channels", 4)
    kwargs.setdefault("min_level", 8)
    kwargs.setdefault("level", 100)
    device = FakeGear(**kwargs)
    return Lamp("debug", Bus(device), "lamp", Short(addr), settings=settings), device


# Validation


def test_settings_are_read_next_to_the_friendly_name(tmp_path):
    names = devices_file(tmp_path, """
3:
  friendly_name: office-rgb
  colour:
    white_points:
      2700: [0, 0, 0, 255]
      6500: [240, 210, 255, 200]
    colour_temp: mixed
    colour_temp_range: [2200, 6500]
4:
  friendly_name: kitchen
""")
    settings = names.get_lamp_settings(3)
    assert settings["colour"]["white_points"] == {2700: [0, 0, 0, 255], 6500: [240, 210, 255, 200]}
    assert names.get_friendly_name(3) == "office-rgb"
    assert names.get_lamp_settings(4) == {"friendly_name": "kitchen"}
    assert names.get_lamp_settings(9) == {}


@pytest.mark.parametrize("colour", [
    "white_points: {2700: [0, 0, 0, 255]}",
    "white_points: {2700: [0, 0, 255], 6500: [1, 2, 3, 4]}",
    "white_points: {2700: [0, 0, 0, 300], 6500: [1, 2, 3, 4]}",
    "white_points: {50: [0, 0, 0, 255], 6500: [1, 2, 3, 4]}",
    "colour_temp: sometimes",
    "colour_temp_range: [6500, 2200]",
    "rgb_gains: [1, 0.9, 0.6]",
    "brightness_boost: 3",
])
def test_invalid_settings_are_ignored_not_fatal(tmp_path, caplog, colour):
    names = devices_file(tmp_path, f"3:\n  friendly_name: x\n  colour:\n    {colour}\n")

    assert names.get_lamp_settings(3) == {}
    assert names.get_friendly_name(3) == "x"
    assert "Ignoring settings of lamp 3" in caplog.text


# The mix


def test_points_set_the_range_unless_one_is_configured():
    from dali2mqtt.colour import ColourMix

    points = {2700: [0, 0, 0, 255], 6500: [240, 210, 255, 200]}
    assert ColourMix.build(points).mirek_range == (154, 370)
    assert ColourMix.build(points, [3000, 6000]).mirek_range == (167, 333)
    assert ColourMix.build(kelvin_range=[2200, 6500]).mirek_range == (154, 455)


# Effect on a lamp


def test_lamp_mixes_with_its_own_settings():
    lamp, device = rgbw_lamp(NF_RGB)

    lamp.set_colour_temp(250)

    expected = colour_temp_to_rgbw(250, ColourMix.build(NF_RGB["colour"]["white_points"]))
    assert device.rgbw == tuple(ha_to_dim(v) for v in expected)
    lamp.set_colour_temp(370)
    assert device.rgbw == (0, 0, 0, 254)  # 2700 K: the white LED alone
    config = json.loads(lamp.gen_ha_config("t"))
    assert (config["min_mirs"], config["max_mirs"]) == (154, 370)


def test_configured_range_reaches_home_assistant():
    lamp, _ = rgbw_lamp({"colour": {"colour_temp_range": [2200, 6500]}})

    config = json.loads(lamp.gen_ha_config("t"))
    assert (config["min_mirs"], config["max_mirs"]) == (154, 455)


def test_colour_temp_off_hides_the_slider():
    lamp, _ = rgbw_lamp({"colour": {"colour_temp": "off"}})

    assert not lamp.has_colour_temp
    assert lamp.has_rgbw
    assert "clr_temp_cmd_t" not in json.loads(lamp.gen_ha_config("t"))


def test_colour_temp_native_uses_the_gear_on_rgbw_gear():
    """A DL-X in RGB+CCT mode renders colour temperature itself."""
    lamp, device = rgbw_lamp({"colour": {"colour_temp": "native"}}, tc_range=(153, 370), tc=250)

    assert not lamp.colour_temp_emulated
    assert lamp.colour_temp_range == (153, 370)
    lamp.set_colour_temp(300)
    assert device.tc == 300


def test_native_range_is_narrowed_by_the_configured_one():
    lamp, _ = rgbw_lamp(
        {"colour": {"colour_temp": "native", "colour_temp_range": [3000, 5000]}},
        tc_range=(153, 370), tc=250,
    )
    assert lamp.colour_temp_range == (200, 333)


def test_mixed_without_rgbw_channels_is_reported(caplog):
    lamp, _ = rgbw_lamp({"colour": {"colour_temp": "mixed"}}, rgbwaf_channels=3,
                        tc_range=(153, 370), tc=250)

    assert not lamp.colour_temp_emulated
    assert "needs four RGBWAF channels" in caplog.text


def test_group_takes_settings_its_members_agree_on():
    a, _ = rgbw_lamp(NF_RGB, addr=1)
    b, _ = rgbw_lamp(NF_RGB, addr=2)
    c, _ = rgbw_lamp(None, addr=4)

    agreeing = Lamp("debug", mock.Mock(), "g", Group(1), [a, b])
    mixed = Lamp("debug", mock.Mock(), "g", Group(2), [a, c])

    assert agreeing.mix == a.mix
    assert mixed.mix == ColourMix.build()


def test_attributes_describe_the_lamp():
    lamp, _ = rgbw_lamp(NF_RGB)

    attributes = lamp.ha_attributes()
    assert attributes["dali_address"] == 3
    assert "model" not in attributes
    assert attributes["colour_temp"] == "mixed"
    assert attributes["white_points"] == {2700: [0, 0, 0, 255], 6500: [240, 210, 255, 200]}
    assert json.loads(lamp.gen_ha_config("t"))["json_attr_t"] == "t/lamp/light/attributes"


def test_start_up_passes_settings_and_publishes_attributes():
    from dali2mqtt.dali2mqtt import initialize_lamps

    device = FakeGear(device_types=(8,), rgbwaf_channels=4, min_level=8, level=74)
    names = mock.Mock()
    names.get_friendly_name.return_value = "office-rgb"
    names.get_lamp_settings.return_value = NF_RGB
    names.is_devices_file_empty.return_value = False
    data = {"driver": Bus(device), "base_topic": "dali2mqtt", "ha_prefix": "homeassistant",
            "log_level": "debug", "devices_names_config": names, "all_lamps": {}}
    client = mock.Mock()

    with mock.patch("dali2mqtt.dali2mqtt.dali_scan", return_value=[3]), \
            mock.patch("dali2mqtt.dali2mqtt.scan_groups", return_value={}):
        initialize_lamps(data, client)

    names.get_lamp_settings.assert_called_with(3)
    assert len(data["all_lamps"]["office-rgb"].mix.points) == 2
    published = {c.args[0]: c for c in client.publish.call_args_list}
    attrs = published["dali2mqtt/office-rgb/light/attributes"]
    assert json.loads(attrs.args[1])["white_points"]["2700"] == [0, 0, 0, 255]
    assert attrs.kwargs["retain"] is True


def test_old_reference_keys_are_tolerated(tmp_path):
    names = devices_file(tmp_path, "3:\n  friendly_name: x\n  model: MiBoxer DL-X\n")
    assert names.get_lamp_settings(3)["friendly_name"] == "x"
