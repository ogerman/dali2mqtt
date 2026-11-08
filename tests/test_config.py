"""Tests for config."""
import pytest

from dali2mqtt.config import Config
from unittest import mock


def test_load_config():
    args = mock.Mock()
    args.config = "tests/data/config.yaml"

    cfg = Config(args)

    assert cfg.mqtt_conf == ('localhost', 1883, None, None, 'dali2mqtt')
    assert cfg.dali_driver == "hasseb"
    assert cfg.ha_discovery_prefix == "homeassistant"
    assert cfg.log_level == "info"
    assert cfg.log_color is False
    assert cfg.devices_names_file == "devices.yaml"


def test_colour_temp_rgb_gains_default_and_validation():
    import voluptuous as vol
    from dali2mqtt.config import CONF_SCHEMA

    assert CONF_SCHEMA({})["colour_temp_rgb_gains"] == [1.0, 1.0, 1.0]
    assert CONF_SCHEMA({"colour_temp_rgb_gains": [1, "0.9", 0.49]})[
        "colour_temp_rgb_gains"] == [1.0, 0.9, 0.49]
    for bad in ([1.0, 0.9], [1.0, 0.9, 2.0], [1.0, 0.0, 0.5]):
        with pytest.raises(vol.Invalid):
            CONF_SCHEMA({"colour_temp_rgb_gains": bad})
