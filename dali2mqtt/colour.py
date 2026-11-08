"""Colour control gear, IEC 62386-209 (device type 8).

Colour gear keeps one setting per colour type it supports: xy chromaticity,
colour temperature Tc, primary N dim levels or RGBWAF channel levels. A new
colour is written into temporary registers and then activated, either by the
``Activate`` command or, when the gear's "automatic activation" bit is set,
by any arc power command as part of the same fade.

16 bit values travel as LSB in DTR0 and MSB in DTR1; 65535 is MASK, "no
value". Queries answer with the MSB and leave the LSB in DTR0. Colour
temperature is in mirek, the unit Home Assistant calls mireds. RGBWAF dim
levels are single bytes, 0..254, with 255 as MASK.
"""
import logging

from dali.gear.colour import (
    Activate,
    QueryColourStatus,
    QueryColourTypeFeatures,
    QueryColourValue,
    QueryColourValueDTR,
    QueryGearFeaturesStatus,
    SetTemporaryRGBDimLevel,
    SetTemporaryRGBWAFControl,
    SetTemporaryWAFDimLevel,
)
from dali.gear.general import DTR0, DTR1, DTR2, QueryContentDTR0
from dali.gear.sequences import SetDT8ColourValueTc
from dali.sequences import QueryDeviceTypes

logger = logging.getLogger(__name__)

DT8 = 8
MASK8 = 255
MASK16 = 65535

# Colour types, named after the Home Assistant colour mode where one exists.
COLOR_TEMP = "color_temp"
XY = "xy"
PRIMARY_N = "primary_n"
RGBWAF = "rgbwaf"

# Colour temperature Tc is 1..65534 mirek; the gear ignores 0.
TC_MIN = 1
TC_MAX = 65534
# RGBWAF dim levels are arc power levels, 0..254.
DIM_MAX = 254
HA_MAX = 255

# RGBWAF CONTROL byte: bits 0..5 link a channel to the arc power level (it
# then ignores its dim level), bits 7..6 pick the control type. Normalised
# colour control with every channel unlinked scales all channels by
# arc power / brightest channel, so brightness dims the colour as a whole.
RGBWAF_NORMALISED_UNLINKED = 0x80

Q = QueryColourValueDTR

# QUERY COLOUR STATUS bits that name the active colour type.
_ACTIVE_TYPE_BITS = (
    ("colour_type_xy_active", XY),
    ("colour_type_colour_temperature_Tc_active", COLOR_TEMP),
    ("colour_type_primary_N_active", PRIMARY_N),
    ("colour_type_RGBWAF_active", RGBWAF),
)
_VALUE_KEYS = {
    COLOR_TEMP: (Q.ColourTemperatureTC,),
    RGBWAF: (Q.RedDimLevel, Q.GreenDimLevel, Q.BlueDimLevel, Q.WhiteDimLevel),
    XY: (Q.XCoordinate, Q.YCoordinate),
}


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


class ColourState:
    """What a gear shows: its active colour type and that type's value."""

    def __init__(self, mode=None, colour_temp=None, rgbw=None, xy=None):
        self.mode = mode
        self.colour_temp = colour_temp  # mirek
        self.rgbw = rgbw  # (red, green, blue, white) dim levels 0..254
        self.xy = xy  # (x, y) in 1/65536

    def __eq__(self, other):
        return isinstance(other, ColourState) and vars(self) == vars(other)

    def __repr__(self):
        return f"ColourState({vars(self)})"


def ha_to_dim(value):
    """Scale a Home Assistant channel value 0..255 to a DALI dim level."""
    value = min(max(int(value), 0), HA_MAX)
    return round(value * DIM_MAX / HA_MAX)


def dim_to_ha(level):
    """Scale a DALI dim level 0..254 to a Home Assistant channel value."""
    return round(min(max(int(level), 0), DIM_MAX) * HA_MAX / DIM_MAX)


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


def colour_values_sequence(address, keys):
    """Sequence reading 16 bit colour values; returns {key: value or None}.

    Each value takes three frames: DTR0 selects it, QUERY COLOUR VALUE answers
    the MSB and leaves the LSB in DTR0. MASK and missing answers give None.
    """
    result = {}
    for key in keys:
        yield DTR0(key.value)
        msb = yield QueryColourValue(address)
        lsb = yield QueryContentDTR0(address)
        value = None
        high = getattr(msb, "value", None)
        low = getattr(lsb, "value", None)
        if isinstance(high, int) and isinstance(low, int):
            value = (high << 8) | low
        result[key] = None if value is None or value == MASK16 else value
    return result


def query_colour_values(bus, address, keys, attempts=2):
    """Read colour values, retrying once for answers the interface lost."""
    result = {}
    for _ in range(attempts):
        try:
            result = bus.run_sequence(colour_values_sequence(address, keys))
        except Exception as err:  # pylint: disable=broad-except
            logger.debug("Can't read colour values of %s: %s", address, err)
            result = {}
        if all(result.get(key) is not None for key in keys):
            break
    return result


def query_colour_temp_range(bus, address):
    """Return (coolest, warmest) in mirek, or None when unknown.

    These are the limits the gear renders, which may be narrower than its
    physical ones; gear that leaves them at MASK falls back to the physical
    limits. Coolest is the lower number.
    """
    for keys in (
        (Q.ColourTemperatureTcCoolest, Q.ColourTemperatureTcWarmest),
        (Q.ColourTemperatureTcPhysicalCoolest, Q.ColourTemperatureTcPhysicalWarmest),
    ):
        values = query_colour_values(bus, address, keys)
        coolest, warmest = (values.get(key) for key in keys)
        if coolest is not None and warmest is not None and coolest <= warmest:
            return coolest, warmest
        logger.debug("No colour temperature range from %s: %s", address, values)
    return None


def read_colour_state(bus, address):
    """Return the gear's ColourState, or None when it can't be read.

    Only the active colour type's value is read; the others are MASK anyway.
    """
    try:
        status = bus.send(QueryColourStatus(address))
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("Can't read colour status of %s: %s", address, err)
        return None
    if getattr(status, "raw_value", None) is None:
        return None
    mode = None
    for attr, name in _ACTIVE_TYPE_BITS:
        if getattr(status, attr, False):
            mode = name
            break
    state = ColourState(mode)
    keys = _VALUE_KEYS.get(mode, ())
    values = query_colour_values(bus, address, keys) if keys else {}
    if mode == COLOR_TEMP:
        state.colour_temp = values.get(Q.ColourTemperatureTC)
    elif mode == RGBWAF:
        levels = tuple(values.get(key) for key in keys)
        if all(isinstance(level, int) and level <= DIM_MAX for level in levels):
            state.rgbw = levels
        else:
            logger.debug("Unusable RGBW levels from %s: %s", address, levels)
    elif mode == XY:
        xy = tuple(values.get(key) for key in keys)
        if all(isinstance(v, int) for v in xy):
            state.xy = xy
    logger.debug("%s colour state %s", address, state)
    return state


def set_colour_temp(bus, address, mirek):
    """Set and activate a colour temperature; works for groups too."""
    mirek = min(max(int(mirek), TC_MIN), TC_MAX)
    bus.run_sequence(SetDT8ColourValueTc(address, mirek))
    return mirek


def rgbw_sequence(address, red, green, blue, white):
    """Sequence setting RGBW dim levels and activating them.

    The control byte goes first: with channels linked (the factory default)
    the gear ignores dim levels, see RGBWAF_NORMALISED_UNLINKED. Amber and
    freecolour get MASK, "no change".
    """
    yield DTR0(RGBWAF_NORMALISED_UNLINKED)
    yield SetTemporaryRGBWAFControl(address)
    yield DTR0(red)
    yield DTR1(green)
    yield DTR2(blue)
    yield SetTemporaryRGBDimLevel(address)
    yield DTR0(white)
    yield DTR1(MASK8)
    yield DTR2(MASK8)
    yield SetTemporaryWAFDimLevel(address)
    yield Activate(address)


def set_rgbw(bus, address, rgbw):
    """Set and activate RGBW dim levels (0..254 each); works for groups too."""
    levels = tuple(min(max(int(level), 0), DIM_MAX) for level in rgbw)
    if len(levels) != 4:
        raise ValueError("RGBW needs four levels")
    bus.run_sequence(rgbw_sequence(address, *levels))
    return levels
