"""Tests for lamp."""

from dali2mqtt.lamp import Lamp
from dali2mqtt.consts import __version__
from unittest import mock
from dali.address import Short, Group
import pytest
import json
from slugify import slugify

MIN__PHYSICAL_BRIGHTNESS = 1
MIN_BRIGHTNESS = 2
MAX_BRIGHTNESS = 250
ACTUAL_BRIGHTNESS = 100

def generate_driver_values(results):
    for res in results:
        result = mock.Mock()
        result.value = res
        print(result.value)
        yield result

@pytest.fixture
def fake_driver():
    drive = mock.Mock()
    drive.dummy = generate_driver_values([MIN__PHYSICAL_BRIGHTNESS, MIN_BRIGHTNESS, MAX_BRIGHTNESS, ACTUAL_BRIGHTNESS, ACTUAL_BRIGHTNESS])
    drive.send = mock.Mock(side_effect=lambda x: next(drive.dummy))
    return drive


@pytest.fixture
def fake_address():
    address = mock.Mock()
    address.address = 1
    address.__repr__ = lambda: "1"

def test_ha_config(fake_driver, fake_address):

    friendly_name = "my lamp"
    addr_number = 1
    addr = Short(1)

    lamp1 = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name=friendly_name,
        short_address=addr,
    )

    assert lamp1.device_name == slugify(friendly_name)
    assert lamp1.short_address.address == addr_number

    assert str(lamp1) == f'my-lamp - address: {addr_number}, actual brightness level: {ACTUAL_BRIGHTNESS} (minimum: {MIN_BRIGHTNESS}, max: {MAX_BRIGHTNESS}, physical minimum: {MIN__PHYSICAL_BRIGHTNESS})'

    assert json.loads(lamp1.gen_ha_config("test")) == {
        "name": friendly_name,
        "def_ent_id": "dali_light_my-lamp",
        "uniq_id": f"Mock_{addr_number}",
        "stat_t": "test/my-lamp/light/status",
        "cmd_t": "test/my-lamp/light/switch",
        "pl_off": "OFF",
        "bri_stat_t": "test/my-lamp/light/brightness/status",
        "bri_cmd_t": "test/my-lamp/light/brightness/set",
        "bri_scl": MAX_BRIGHTNESS,
        "on_cmd_type": "brightness",
        "avty_t": "test/status",
        "pl_avail": "online",
        "pl_not_avail": "offline",
        "device": {
            "ids": "dali2mqtt",
            "name": "DALI Lights",
            "sw": f"dali2mqtt {__version__}",
            "mdl": "Mock",
            "mf": "dali2mqtt",
        },
    }


def test_group_lamp_initialization(fake_driver):
    """Test that Group addresses are initialized with default values."""
    friendly_name = "group 1"
    group_addr = Group(1)
    
    # Reset mock - groups shouldn't call driver.send for query operations
    fake_driver.send.reset_mock()

    # Groups shouldn't call driver.send for query operations
    lamp_group = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name=friendly_name,
        short_address=group_addr,
    )

    assert lamp_group.is_group is True
    assert lamp_group.device_name == slugify(friendly_name)
    assert lamp_group.short_address == group_addr
    # Groups use default values
    assert lamp_group.min_physical_level is None
    assert lamp_group.min_level == 1  # Standard DALI minimum
    assert lamp_group.max_level == 254  # Standard DALI maximum
    assert lamp_group.level == 0  # Default to off
    # Groups don't query, so send should not be called during initialization
    assert not fake_driver.send.called


def test_group_lamp_str(fake_driver):
    """Test __str__ method for Group addresses."""
    friendly_name = "group 2"
    group_addr = Group(2)

    lamp_group = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name=friendly_name,
        short_address=group_addr,
    )

    # __str__ should work without AttributeError
    str_repr = str(lamp_group)
    assert "group-2" in str_repr
    # Group string representation is "<group 2>"
    assert "<group 2>" in str_repr or "group 2" in str_repr
    assert "actual brightness level: 0" in str_repr
    assert "minimum: 1" in str_repr
    assert "max: 254" in str_repr


def test_group_lamp_ha_config(fake_driver):
    """Test gen_ha_config for Group addresses."""
    friendly_name = "group 3"
    group_addr = Group(3)

    lamp_group = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name=friendly_name,
        short_address=group_addr,
    )

    config = json.loads(lamp_group.gen_ha_config("test"))
    assert config["name"] == friendly_name
    assert config["def_ent_id"] == "dali_light_group-3"
    # uniq_id format: Mock_group_Group(3)
    assert config["uniq_id"].startswith("Mock_group_")
    assert "Group(3)" in config["uniq_id"] or "3" in config["uniq_id"]
    assert config["bri_scl"] == 254  # Group max level


def test_group_lamp_actual_level(fake_driver):
    """Test that actual_level() doesn't query for groups."""
    group_addr = Group(1)
    lamp_group = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name="group 1",
        short_address=group_addr,
    )

    initial_level = lamp_group.level
    # actual_level() should not raise an error and should not query
    lamp_group.actual_level()
    # Level should remain unchanged (can't query groups)
    assert lamp_group.level == initial_level


def test_group_lamp_level_setter(fake_driver):
    """Test that level setter works for groups."""
    group_addr = Group(1)
    # Create a new mock driver for this test that doesn't use the generator
    mock_driver = mock.Mock()
    mock_driver.send = mock.Mock(return_value=None)  # DAPC doesn't return a value
    
    lamp_group = Lamp(
        log_level="debug",
        driver=mock_driver,
        friendly_name="group 1",
        short_address=group_addr,
    )

    # Groups don't call send during initialization (we set __level directly)
    assert mock_driver.send.call_count == 0
    
    # Set level should work and call driver.send with DAPC
    lamp_group.level = 128
    assert lamp_group.level == 128
    # Verify driver.send was called (for DAPC command)
    assert mock_driver.send.call_count == 1


def test_group_lamp_off(fake_driver):
    """Test that off() works for groups."""
    group_addr = Group(1)
    lamp_group = Lamp(
        log_level="debug",
        driver=fake_driver,
        friendly_name="group 1",
        short_address=group_addr,
    )

    # Reset mock call count
    fake_driver.send.reset_mock()
    
    # off() should call driver.send with Off command
    lamp_group.off()
    assert fake_driver.send.called
