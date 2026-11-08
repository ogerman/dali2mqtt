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
import math
from dataclasses import dataclass

from dali.gear.colour import (
    Activate,
    QueryColourStatus,
    QueryColourTypeFeatures,
    QueryColourValue,
    QueryColourValueDTR,
    QueryGearFeaturesStatus,
    SetTemporaryRGBDimLevel,
    SetTemporaryColourTemperature,
    SetTemporaryRGBWAFControl,
    SetTemporaryWAFDimLevel,
)
from dali.gear.general import DTR0, DTR1, DTR2, QueryActualLevel, QueryContentDTR0
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
# Limits a lamp can plausibly render: 25000 K down to 800 K. Gear has been
# seen answering near-MASK garbage for limits it never had set.
TC_SANE = (40, 1250)
# Used when the gear reports no usable limits; Home Assistant's own default.
TC_DEFAULT_RANGE = (153, 370)
# Range of the colour temperature mixed from RGBW channels: 6500 K .. 2000 K.
TC_EMULATED_RANGE = (153, 500)
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
# "REPORT COLOUR TYPE" values (part 209, 9.12.6).
_REPORT_TYPES = {0x10: XY, 0x20: COLOR_TEMP, 0x40: PRIMARY_N, 0x80: RGBWAF}
# Colour values that are a single byte: RGBWAF dim levels and control, the
# colour types. The gear answers them directly; on the real bus office-rgb
# echoes the byte into both DTRs and living-room-rgb leaves DTR0 at 0, so
# assembling them as 16 bit values gives nonsense.
EIGHT_BIT_KEYS = frozenset(
    key for key in Q
    if 9 <= key.value <= 15 or 201 <= key.value <= 208 or 233 <= key.value <= 240
)


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


def kelvin_to_rgb(kelvin):
    """Return the (r, g, b) of a black body at ``kelvin``, 0..255 each.

    Tanner Helland's fit, the one Home Assistant uses too; good from 1000 K
    to 40000 K.
    """
    t = min(max(kelvin, 1000), 40000) / 100
    if t <= 66:
        red = 255
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        red = 329.698727446 * (t - 60) ** -0.1332047592
        green = 288.1221695283 * (t - 60) ** -0.0755148492
        blue = 255
    return tuple(round(min(max(c, 0), 255)) for c in (red, green, blue))


@dataclass(frozen=True)
class ColourMix:
    """How a colour temperature is mixed from a strip's RGBW channels.

    ``points`` are calibration points, ((mirek, (r, g, b, w)), ...) sorted
    coolest first: the RGBW values (0..255) that render that colour
    temperature on this strip. In between they are interpolated by mirek.
    Without points the textbook black body colour is used, with the W
    channel faded out towards the cool end.

    Camera-based white balance factors didn't work: the strip mixes white
    from narrow red, green and blue LEDs, and a camera and the eye see such
    light differently, so camera matches looked strongly coloured. Points
    matched by eye against a reference light do work. On the nf strips
    (Arlight RT-B60-10mm RGBW-Warm on MiBoxer DL-X drivers) 2700 K is the
    white LED alone and 6500 K is 240, 210, 255 with W 200.

    ``mirek_range`` is the (coolest, warmest) range offered.
    """

    points: tuple = ()
    mirek_range: tuple = TC_EMULATED_RANGE

    @classmethod
    def build(cls, white_points=None, kelvin_range=None):
        """Return a mix from ``{kelvin: [r, g, b, w]}`` and a kelvin range.

        Without a range, calibration points span the range they cover.
        """
        points = ()
        if white_points:
            points = tuple(sorted(
                (1_000_000 / kelvin, tuple(int(v) for v in rgbw))
                for kelvin, rgbw in white_points.items()
            ))
        if kelvin_range is not None:
            low, high = sorted(kelvin_range)
            mirek_range = (round(1_000_000 / high), round(1_000_000 / low))
        elif len(points) >= 2:
            mirek_range = (round(points[0][0]), round(points[-1][0]))
        else:
            mirek_range = TC_EMULATED_RANGE
        return cls(points, mirek_range)

    def kelvin_points(self):
        """Return the calibration points as {kelvin: [r, g, b, w]}."""
        return {round(1_000_000 / m): list(rgbw) for m, rgbw in self.points}


def _interpolate(points, mirek):
    """RGBW for ``mirek`` from calibration points, clamped at both ends."""
    if mirek <= points[0][0]:
        return points[0][1]
    if mirek >= points[-1][0]:
        return points[-1][1]
    for (m1, c1), (m2, c2) in zip(points, points[1:]):
        if m1 <= mirek <= m2:
            t = (mirek - m1) / (m2 - m1)
            return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))
    return points[-1][1]


def colour_temp_to_rgbw(mirek, mix=None):
    """Return Home Assistant RGBW values (0..255) that render ``mirek``.

    For RGBW gear whose own colour temperature mode doesn't work: a MiBoxer
    DL-X in RGBW mode, as on the real bus, can only drive its one white
    channel with it. With calibration points the mix interpolates between
    them; without, the RGB channels make the textbook black body colour and
    the W channel fades out from the warm end to the cool end.
    """
    mix = mix or ColourMix.build()
    coolest, warmest = mix.mirek_range
    mirek = min(max(int(mirek), coolest), warmest)
    if len(mix.points) >= 2:
        return _interpolate(mix.points, mirek)
    red, green, blue = kelvin_to_rgb(1_000_000 / mirek)
    warmth = (mirek - coolest) / (warmest - coolest) if warmest > coolest else 1.0
    return red, green, blue, round(255 * warmth)


def ha_to_dim(value):
    """Scale a Home Assistant channel value 0..255 to a DALI dim level."""
    value = min(max(int(value), 0), HA_MAX)
    return round(value * DIM_MAX / HA_MAX)


def dim_to_ha(level):
    """Scale a DALI dim level 0..254 to a Home Assistant channel value."""
    return round(min(max(int(level), 0), DIM_MAX) * HA_MAX / DIM_MAX)


def query_device_types(bus, address, attempts=3):
    """Return the gear's device types, [] when they can't be read.

    The query is repeated when an answer is lost.
    """
    for _ in range(attempts):
        try:
            types = bus.run_sequence(QueryDeviceTypes(address))
        except Exception as err:  # pylint: disable=broad-except
            logger.debug("Can't read device types of %s: %s", address, err)
            continue
        if isinstance(types, list) and all(isinstance(t, int) for t in types):
            return types
    return []


def query_colour_features(bus, address, attempts=3):
    """Return the gear's ColourFeatures, or None when it doesn't answer."""
    for _ in range(attempts):
        features = bus.send(QueryColourTypeFeatures(address))
        if getattr(features, "raw_value", None) is not None:
            break
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
    """Sequence reading colour values; returns {key: value or None}.

    DTR0 selects the value and QUERY COLOUR VALUE answers it. For 16 bit
    values the answer is the MSB and the LSB is left in DTR0; single byte
    values (EIGHT_BIT_KEYS) are the answer itself. MASK and missing answers
    give None.
    """
    result = {}
    for key in keys:
        yield DTR0(key.value)
        answer = yield QueryColourValue(address)
        high = _answer_byte(answer)
        if key in EIGHT_BIT_KEYS:
            result[key] = None if high is None or high == MASK8 else high
            continue
        lsb = yield QueryContentDTR0(address)
        low = _answer_byte(lsb)
        value = None if high is None or low is None else (high << 8) | low
        result[key] = None if value is None or value == MASK16 else value
    return result


def _answer_byte(response):
    """Return the byte a gear answered, or None for no or a garbled answer."""
    raw = getattr(response, "raw_value", None)
    if raw is None or getattr(raw, "error", False):
        return None
    return raw.as_integer


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
    """Return (coolest, warmest) in mirek, or None when the gear gives none.

    These are the limits the gear renders, which may be narrower than its
    physical ones; gear that leaves them at MASK falls back to the physical
    limits. Limits outside TC_SANE are ignored. Coolest is the lower number.
    """
    for keys in (
        (Q.ColourTemperatureTcCoolest, Q.ColourTemperatureTcWarmest),
        (Q.ColourTemperatureTcPhysicalCoolest, Q.ColourTemperatureTcPhysicalWarmest),
    ):
        values = query_colour_values(bus, address, keys)
        coolest, warmest = (values.get(key) for key in keys)
        if (
            isinstance(coolest, int)
            and isinstance(warmest, int)
            and TC_SANE[0] <= coolest <= warmest <= TC_SANE[1]
        ):
            return coolest, warmest
        logger.debug("No usable colour temperature range from %s: %s", address, values)
    return None


def plausible_xy(xy):
    """Return True for a real chromaticity: x, y > 0 and x + y <= 1.

    RGBW gear answers near-MASK values (x, y around 0.995) when asked for
    xy while another colour type is active.
    """
    x, y = xy
    return isinstance(x, int) and isinstance(y, int) and x > 0 and y > 0 and x + y <= 65536


def query_colour_mode(bus, address):
    """Return the gear's active colour type, or None when it can't be told.

    QUERY ACTUAL LEVEL refreshes the report colour type, which says exactly
    one type. QUERY COLOUR STATUS is the fallback, used only when it has a
    single type bit set: the RGBW gear on the real bus sets three or four.
    """
    try:
        bus.send(QueryActualLevel(address))
        report = query_colour_values(bus, address, (Q.ReportColourType,)).get(Q.ReportColourType)
        if report in _REPORT_TYPES:
            return _REPORT_TYPES[report]
        status = bus.send(QueryColourStatus(address))
    except Exception as err:  # pylint: disable=broad-except
        logger.debug("Can't read colour type of %s: %s", address, err)
        return None
    if getattr(status, "raw_value", None) is None:
        return None
    active = [name for attr, name in _ACTIVE_TYPE_BITS if getattr(status, attr, False)]
    if len(active) != 1:
        logger.debug("Colour status of %s names %s", address, active or "no type")
        return None
    return active[0]


def read_colour_state(bus, address):
    """Return the gear's ColourState, or None when it can't be read.

    Only the active colour type's value is read; the others are MASK anyway.
    A state is only returned when that value is readable and plausible, so a
    bad reading never overwrites a colour that was set correctly.
    """
    mode = query_colour_mode(bus, address)
    keys = _VALUE_KEYS.get(mode)
    if not keys:
        logger.debug("%s colour type %s is not read", address, mode)
        return None
    values = query_colour_values(bus, address, keys)
    found = tuple(values.get(key) for key in keys)
    state = ColourState(mode)
    if mode == COLOR_TEMP and isinstance(found[0], int) and TC_SANE[0] <= found[0] <= TC_SANE[1]:
        state.colour_temp = found[0]
    elif mode == RGBWAF and all(isinstance(v, int) and v <= DIM_MAX for v in found):
        state.rgbw = found
    elif mode == XY and plausible_xy(found):
        state.xy = found
    else:
        logger.debug("Implausible %s colour from %s: %s", mode, address, found)
        return None
    logger.debug("%s colour state %s", address, state)
    return state


def colour_temp_sequence(address, mirek, activate=True):
    """Sequence setting a colour temperature, then activating it if asked."""
    yield DTR0(mirek & 0xFF)
    yield DTR1(mirek >> 8)
    yield SetTemporaryColourTemperature(address)
    if activate:
        yield Activate(address)


def set_colour_temp(bus, address, mirek, activate=True):
    """Set a colour temperature, activating it unless asked not to.

    Works for groups too. Without activation the value waits in the gear's
    temporary register for an ACTIVATE or, on gear with automatic
    activation, the next arc power command.
    """
    mirek = min(max(int(mirek), TC_MIN), TC_MAX)
    bus.run_sequence(colour_temp_sequence(address, mirek, activate))
    return mirek


def rgbw_sequence(address, red, green, blue, white, activate=True):
    """Sequence setting RGBW dim levels, then activating them if asked.

    The control byte goes first: with channels linked (the factory default)
    the gear ignores dim levels, see RGBWAF_NORMALISED_UNLINKED. Amber and
    freecolour are set to 0, as the wall colour picker on the real bus does:
    "no change" kept whatever amber the gear last had, and on the RGBW gear
    tested amber drives the same LED as white, so the white level had no
    visible effect.
    """
    yield DTR0(RGBWAF_NORMALISED_UNLINKED)
    yield SetTemporaryRGBWAFControl(address)
    yield DTR0(red)
    yield DTR1(green)
    yield DTR2(blue)
    yield SetTemporaryRGBDimLevel(address)
    yield DTR0(white)
    yield DTR1(0)
    yield DTR2(0)
    yield SetTemporaryWAFDimLevel(address)
    if activate:
        yield Activate(address)


def set_rgbw(bus, address, rgbw, activate=True):
    """Set RGBW dim levels (0..254 each), activating them unless asked not to.

    Works for groups too; see set_colour_temp for activation.
    """
    levels = tuple(min(max(int(level), 0), DIM_MAX) for level in rgbw)
    if len(levels) != 4:
        raise ValueError("RGBW needs four levels")
    bus.run_sequence(rgbw_sequence(address, *levels, activate=activate))
    return levels
