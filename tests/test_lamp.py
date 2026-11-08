"""Tests for lamp."""

from dali2mqtt.lamp import Lamp
from dali2mqtt.consts import __version__
from unittest import mock
from dali.address import Short, Group
from dali.frame import BackwardFrame
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
    drive.dummy = generate_driver_values([
        MIN__PHYSICAL_BRIGHTNESS, MIN_BRIGHTNESS, MAX_BRIGHTNESS,
        ACTUAL_BRIGHTNESS, ACTUAL_BRIGHTNESS
    ])
    drive.send = lambda x: next(drive.dummy)
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

    assert str(lamp1) == (
        f'my-lamp - address: {addr_number}, actual brightness level: '
        f'{ACTUAL_BRIGHTNESS} (minimum: {MIN_BRIGHTNESS}, max: {MAX_BRIGHTNESS}, '
        f'physical minimum: {MIN__PHYSICAL_BRIGHTNESS})'
    )

    assert json.loads(lamp1.gen_ha_config("test")) == {
        "name": friendly_name,
        "def_ent_id": "dali_light_my-lamp",
        "uniq_id": f"Mock_{addr}",
        "stat_t": "test/my-lamp/light/status",
        "cmd_t": "test/my-lamp/light/switch",
        "pl_on": "ON",
        "pl_off": "OFF",
        "bri_stat_t": "test/my-lamp/light/brightness/status",
        "bri_cmd_t": "test/my-lamp/light/brightness/set",
        "bri_scl": MAX_BRIGHTNESS - MIN_BRIGHTNESS + 1,
        "on_cmd_type": "brightness",
        "avty_t": "test/status",
        "pl_avail": "online",
        "pl_not_avail": "offline",
        "json_attr_t": "test/my-lamp/light/attributes",
        "device": {
            "ids": "dali2mqtt",
            "name": "DALI Lights",
            "sw": f"dali2mqtt {__version__}",
            "mdl": "Mock",
            "mf": "dali2mqtt",
        },
    }


def test_group_lamp_str():
    """Test __str__ method for Group addresses."""
    friendly_name = "group 1"
    group_addr = Group(1)

    # Create a mock driver that returns mock responses for query operations
    mock_driver = mock.Mock()
    mock_result = mock.Mock()
    mock_result.value = 100  # For actual level query
    mock_driver.send = mock.Mock(return_value=mock_result)

    lamp_group = Lamp(
        log_level="debug",
        driver=mock_driver,
        friendly_name=friendly_name,
        short_address=group_addr,
    )

    # __str__ should work without AttributeError
    # Address text comes from python-dali, e.g. "<group (control gear) 1>"
    str_repr = str(lamp_group)
    assert "group-1" in str_repr
    assert f"address: {group_addr}," in str_repr
    assert "actual brightness level:" in str_repr


def test_lamp_initialization_short_and_group():
    """Test that both Short and Group addresses initialize correctly."""
    # Short address
    mock_driver_short = mock.Mock()
    mock_result = mock.Mock()
    mock_result.value = 100
    mock_driver_short.send = mock.Mock(return_value=mock_result)

    lamp_short = Lamp("debug", mock_driver_short, "lamp 1", Short(1))
    assert not lamp_short.is_group
    assert lamp_short.min_level == 100
    assert lamp_short.max_level == 100

    # Group address
    mock_driver_group = mock.Mock()
    mock_driver_group.send = mock.Mock(return_value=None)

    lamp_group = Lamp("debug", mock_driver_group, "group 1", Group(1))
    assert lamp_group.is_group
    assert lamp_group.min_level == 1
    assert lamp_group.max_level == 254
    assert lamp_group.level == 0


@pytest.mark.parametrize(
    "addr,expected",
    [
        (Short(5), "Mock_<address (control gear) 5>"),
        (Group(1), "Mock_<group (control gear) 1>"),
    ],
)
def test_ha_unique_id_is_stable(addr, expected):
    """Home Assistant tracks entities by uniq_id, so its format must not change.

    It comes from python-dali's address text, which changed in python-dali 0.10.
    Existing HA installs use the format below.
    """
    driver = mock.Mock()
    driver.send = mock.Mock(return_value=mock.Mock(value=100))
    lamp = Lamp("debug", driver, "lamp", addr)
    assert json.loads(lamp.gen_ha_config("dali2mqtt"))["uniq_id"] == expected


def test_actual_level_stores_number():
    """actual_level() must store the level, not python-dali's response object."""
    driver = mock.Mock()
    driver.send = mock.Mock(return_value=mock.Mock(value=100))
    lamp = Lamp("debug", driver, "lamp", Short(1))

    driver.send = mock.Mock(side_effect=lambda cmd: cmd.response(BackwardFrame(120)))
    lamp.actual_level()

    assert lamp.level == 120


def make_lamp(min_level, max_level, addr=Short(1), level=0):
    driver = mock.Mock()
    driver.send = mock.Mock(
        side_effect=[
            mock.Mock(value=v) for v in (min_level, min_level, max_level, level)
        ]
        + [None] * 10
    )
    return Lamp("debug", driver, "lamp", addr)


@pytest.mark.parametrize(
    "brightness,level",
    [(0, 0), (1, 157), (50, 206), (98, 254), (255, 254)],
)
def test_ha_brightness_maps_onto_min_max(brightness, level):
    """HA brightness 1..bri_scl covers the lamp's own min..max levels."""
    lamp = make_lamp(157, 254)
    assert json.loads(lamp.gen_ha_config("t"))["bri_scl"] == 98
    assert lamp.ha_to_level(brightness) == level
    if 0 < brightness <= 98:
        assert lamp.level_to_ha(level) == brightness


def test_level_below_min_reported_as_lowest_brightness():
    lamp = make_lamp(157, 254)
    assert lamp.level_to_ha(100) == 1
    assert lamp.level_to_ha(0) == 0


def test_fixed_level_device_is_on_off_only():
    """A device with min == max (a fan relay) gets no brightness slider."""
    lamp = make_lamp(254, 254)
    config = json.loads(lamp.gen_ha_config("t"))
    assert not lamp.dimmable
    assert "bri_cmd_t" not in config
    assert "bri_scl" not in config
    assert "on_cmd_type" not in config
    assert config["pl_on"] == "ON"


def test_group_uses_members_range():
    members = [make_lamp(157, 254), make_lamp(85, 254), make_lamp(254, 254)]
    group = Lamp("debug", mock.Mock(), "group", Group(1), members)
    assert (group.min_level, group.max_level) == (85, 254)
    assert json.loads(group.gen_ha_config("t"))["bri_scl"] == 170


def test_group_of_fixed_level_devices_is_on_off_only():
    members = [make_lamp(254, 254), make_lamp(254, 254)]
    group = Lamp("debug", mock.Mock(), "group", Group(1), members)
    assert not group.dimmable


class LossyGear:
    """FakeGear that loses the first ``count`` answers to each named query."""

    def __init__(self, losses, **gear_kwargs):
        from tests.fakes import FakeGear

        self.gear = FakeGear(**gear_kwargs)
        self.losses = dict(losses)

    def send(self, command):
        name = type(command).__name__
        if self.losses.get(name, 0) > 0:
            self.losses[name] -= 1
            self.gear.sent.append(command)
            return command.response(None)
        return self.gear.send(command)


def test_lost_answers_are_asked_again():
    """living-room-rgb loses about one answer in five on the real bus."""
    driver = LossyGear(
        {"QueryMinLevel": 1, "QueryMaxLevel": 2, "QueryPhysicalMinimum": 1},
        min_level=8, max_level=254, level=146,
    )

    lamp = Lamp("debug", driver, "living-room-rgb", Short(12))

    assert (lamp.min_level, lamp.max_level, lamp.level) == (8, 254, 146)
    assert lamp.min_physical_level == 1


def test_lamp_whose_limits_never_answer_is_skipped():
    from dali.exceptions import DALIError

    driver = LossyGear({"QueryMaxLevel": 3})
    with pytest.raises(DALIError):
        Lamp("debug", driver, "silent", Short(12))


def test_unknown_physical_minimum_and_level_have_safe_defaults():
    driver = LossyGear({"QueryPhysicalMinimum": 3, "QueryActualLevel": 3}, min_level=8)

    lamp = Lamp("debug", driver, "lamp", Short(1))

    assert lamp.min_physical_level is None
    assert lamp.level == 0


def test_start_up_sends_no_level():
    """The lamp is already at the level it reports; re-sending it is noise."""
    from tests.fakes import FakeGear

    device = FakeGear(level=120)
    lamp = Lamp("debug", device, "lamp", Short(1))

    assert lamp.level == 120
    assert not any(type(c).__name__ == "DAPC" for c in device.sent)
