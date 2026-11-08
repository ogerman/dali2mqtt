"""Colour control gear, IEC 62386-209 (device type 8).

Colour gear keeps one setting per colour type it supports: xy chromaticity,
colour temperature Tc, primary N dim levels or RGBWAF channel levels. A new
colour is written into temporary registers and then activated, either by the
``Activate`` command or, when the gear's "automatic activation" bit is set,
by any arc power command as part of the same fade.

16 bit values travel as LSB in DTR0 and MSB in DTR1; 65535 is MASK, "no
value". Colour temperature is in mirek, the unit Home Assistant calls mireds.
"""
import logging

from dali.gear.colour import (
    QueryColourTypeFeatures,
    QueryColourValueDTR,
    QueryGearFeaturesStatus,
)
from dali.gear.sequences import QueryDT8ColourValue, SetDT8ColourValueTc
from dali.sequences import QueryDeviceTypes

logger = logging.getLogger(__name__)

DT8 = 8
MASK16 = 65535

# Colour types, named after the Home Assistant colour mode where one exists.
COLOR_TEMP = "color_temp"
XY = "xy"
PRIMARY_N = "primary_n"
RGBWAF = "rgbwaf"

# Colour temperature Tc is 1..65534 mirek; the gear ignores 0.
TC_MIN = 1
TC_MAX = 65534


class ColourFeatures:
    """What a colour gear can do, from QUERY COLOUR TYPE FEATURES."""

    def __init__(self, types, primaries=0, rgbwaf_channels=0, auto_activation=False):
        self.types = frozenset(types)
        self.primaries = primaries
        self.rgbwaf_channels = rgbwaf_channels
        self.auto_activation = auto_activation

    def __str__(self):
        parts = sorted(self.types)
        if self.primaries:
            parts.append(f"{self.primaries} primaries")
        if self.rgbwaf_channels:
            parts.append(f"{self.rgbwaf_channels} RGBWAF channels")
        if self.auto_activation:
            parts.append("auto activation")
        return ", ".join(parts) or "none"


def query_device_types(bus, address):
    """Return the gear's device types, [] when they can't be read."""
    try:
        types = bus.run_sequence(QueryDeviceTypes(address))
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("Can't read device types of %s: %s", address, err)
        return []
    if not isinstance(types, list) or not all(isinstance(t, int) for t in types):
        return []
    return types


def query_colour_features(bus, address):
    """Return the gear's ColourFeatures, or None when it doesn't answer."""
    features = bus.send(QueryColourTypeFeatures(address))
    try:
        types = set()
        if features.xy_capable:
            types.add(XY)
        if features.Tc_capable:
            types.add(COLOR_TEMP)
        primaries = features.primary_n
        channels = features.RGBWAF_channels
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("No colour features from %s: %s", address, err)
        return None
    if primaries:
        types.add(PRIMARY_N)
    if channels:
        types.add(RGBWAF)
    status = bus.send(QueryGearFeaturesStatus(address))
    auto_activation = bool(getattr(status, "auto_activation_enabled", False))
    return ColourFeatures(types, primaries, channels, auto_activation)


def query_colour_value(bus, address, key):
    """Return a 16 bit colour value, or None for MASK or no answer.

    ``key`` is a QueryColourValueDTR member. Values tied to a colour type
    (the current Tc, x, y, ...) are only valid while that type is active.
    """
    try:
        value = bus.run_sequence(QueryDT8ColourValue(address, key))
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("Can't read %s of %s: %s", key.name, address, err)
        return None
    if not isinstance(value, int) or value == MASK16:
        return None
    return value


def query_colour_temp(bus, address):
    """Return the current colour temperature in mirek, or None."""
    return query_colour_value(bus, address, QueryColourValueDTR.ColourTemperatureTC)


def query_colour_temp_range(bus, address):
    """Return (coolest, warmest) in mirek, or None when unknown.

    These are the limits the gear renders, which may be narrower than its
    physical ones. Coolest is the lower number.
    """
    coolest = query_colour_value(
        bus, address, QueryColourValueDTR.ColourTemperatureTcCoolest
    )
    warmest = query_colour_value(
        bus, address, QueryColourValueDTR.ColourTemperatureTcWarmest
    )
    if coolest is None or warmest is None or coolest > warmest:
        return None
    return coolest, warmest


def set_colour_temp(bus, address, mirek):
    """Set and activate a colour temperature; works for groups too."""
    mirek = min(max(int(mirek), TC_MIN), TC_MAX)
    bus.run_sequence(SetDT8ColourValueTc(address, mirek))
    return mirek
