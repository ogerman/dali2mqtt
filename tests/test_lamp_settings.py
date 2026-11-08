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

NF_RGB = {
    "friendly_name": "office-rgb",
    "model": "MiBoxer DL-X",
    "strip": "Arlight RT-B60-10mm 24V RGBW-Warm",
    "colour": {"white_kelvin": 2700, "rgb_gains": [1.0, 0.96, 0.62]},
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
  model: MiBoxer DL-X
  colour:
    white_kelvin: 2700
    rgb_gains: [1, 0.96, 0.62]
    colour_temp: mixed
    colour_temp_range: [2200, 6500]
4:
  friendly_name: kitchen
""")
    settings = names.get_lamp_settings(3)
    assert settings["model"] == "MiBoxer DL-X"
    assert settings["colour"]["rgb_gains"] == [1.0, 0.96, 0.62]
    assert names.get_friendly_name(3) == "office-rgb"
    assert names.get_lamp_settings(4) == {"friendly_name": "kitchen"}
    assert names.get_lamp_settings(9) == {}


@pytest.mark.parametrize("colour", [
    "rgb_gains: [1, 0.9]",
    "rgb_gains: [1, 0.9, 2]",
    "colour_temp: sometimes",
    "colour_temp_range: [6500, 2200]",
    "white_kelvin: 50",
])
def test_invalid_settings_are_ignored_not_fatal(tmp_path, caplog, colour):
    names = devices_file(tmp_path, f"3:\n  friendly_name: x\n  colour:\n    {colour}\n")

    assert names.get_lamp_settings(3) == {}
    assert names.get_friendly_name(3) == "x"
    assert "Ignoring settings of lamp 3" in caplog.text


# The mix


def test_white_led_is_used_fully_from_its_own_colour_temperature():
    mix = ColourMix.build(white_kelvin=2700)

    assert colour_temp_to_rgbw(370, mix)[3] == 255  # 2700 K
    assert colour_temp_to_rgbw(450, mix)[3] == 255  # warmer: still full
    assert 0 < colour_temp_to_rgbw(250, mix)[3] < 255  # 4000 K: blended
    assert colour_temp_to_rgbw(153, mix)[3] == 0  # 6500 K: off
    assert colour_temp_to_rgbw(250, mix)[3] > colour_temp_to_rgbw(250)[3]


def test_without_white_kelvin_the_mix_is_unchanged():
    assert colour_temp_to_rgbw(300, ColourMix.build()) == colour_temp_to_rgbw(300)


def test_kelvin_range_becomes_the_mirek_range():
    assert ColourMix.build(kelvin_range=[2200, 6500]).mirek_range == (154, 455)


# Effect on a lamp


def test_lamp_mixes_with_its_own_settings():
    lamp, device = rgbw_lamp(NF_RGB)

    lamp.set_colour_temp(250)

    expected = colour_temp_to_rgbw(250, ColourMix.build([1.0, 0.96, 0.62], 2700))
    assert device.rgbw == tuple(ha_to_dim(v) for v in expected)
    assert lamp.model == "MiBoxer DL-X"
    assert "model: MiBoxer DL-X" in str(lamp)


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
    assert attributes["model"] == "MiBoxer DL-X"
    assert attributes["strip"] == "Arlight RT-B60-10mm 24V RGBW-Warm"
    assert attributes["colour_temp"] == "mixed"
    assert attributes["white_kelvin"] == 2700
    assert attributes["rgb_gains"] == [1.0, 0.96, 0.62]
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
    assert data["all_lamps"]["office-rgb"].model == "MiBoxer DL-X"
    published = {c.args[0]: c for c in client.publish.call_args_list}
    attrs = published["dali2mqtt/office-rgb/light/attributes"]
    assert json.loads(attrs.args[1])["model"] == "MiBoxer DL-X"
    assert attrs.kwargs["retain"] is True
