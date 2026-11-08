"""Scripted DALI gear for tests: answers queries by command type.

Unlike a mock with a fixed answer list, ``FakeGear`` doesn't care how many
queries a Lamp sends or in which order. It emulates one control gear,
optionally a device type 8 colour gear with colour temperature and RGBW
control, including the DTR registers the 16 bit colour transfers go through
and the temporary-then-activate colour model.
"""
import dali.gear.general as gear
from dali.frame import BackwardFrame
from dali.gear import colour as dt8

MASK8 = 255
MASK16 = 65535
# Single byte colour values: RGBWAF levels and control, colour types.
EIGHT_BIT = {v for v in range(9, 16)} | set(range(201, 209)) | set(range(233, 241))


class FakeGear:
    """One control gear behind a driver's ``send``."""

    def __init__(
        self,
        min_level=85,
        max_level=254,
        level=0,
        device_types=(6,),
        tc_range=None,
        tc_limits_set=True,
        tc_physical_range=None,
        tc=None,
        rgbw=None,
        xy=False,
        xy_value=None,
        primaries=0,
        rgbwaf_channels=0,
        auto_activation=False,
        status_all_bits=False,
        echo_byte=True,
    ):
        self.min_level = min_level
        self.max_level = max_level
        self.level = level
        self.device_types = list(device_types)
        self.tc_range = tc_range  # (coolest, warmest) when Tc capable
        self.tc_limits_set = tc_limits_set  # False: rendered limits left at MASK
        self.tc_physical_range = tc_physical_range or tc_range
        self.tc = tc  # colour temperature; active colour type if given
        self.rgbw = rgbw  # (r, g, b, w) dim levels; active type if no tc
        self.active = "tc" if tc is not None else ("rgbw" if rgbw is not None else None)
        self.xy_value = xy_value  # (x, y) in 1/65536; active type if given
        if xy_value is not None:
            self.active = "xy"
        self.xy = xy
        self.primaries = primaries
        self.rgbwaf_channels = rgbwaf_channels
        self.auto_activation = auto_activation
        # Like the RGBW gear on the real bus: QUERY COLOUR STATUS has every
        # supported type bit set, only the report colour type tells.
        self.status_all_bits = status_all_bits
        # office-rgb echoes single byte answers into DTR0, living-room-rgb leaves 0.
        self.echo_byte = echo_byte
        self.dtr0 = 0
        self.dtr1 = 0
        self.dtr2 = 0
        self.rgbwaf_control = 0x3F  # factory default: every channel linked
        # Amber and freecolour left over from an earlier controller.
        self.amber = 254
        self.free = 254
        self.temporary_tc = None
        self.temporary_rgb = None
        self.temporary_waf = None
        self.sent = []
        self._enabled_type = 0
        self._next_types = []

    @property
    def is_dt8(self):
        return 8 in self.device_types

    def send(self, command):
        self.sent.append(command)
        if isinstance(command, gear.EnableDeviceType):
            self._enabled_type = command.param
            return None
        # ENABLE DEVICE TYPE only covers the very next command.
        enabled = self._enabled_type
        self._enabled_type = 0
        if isinstance(command, gear.DTR0):
            self.dtr0 = command.param
            return None
        if isinstance(command, gear.DTR1):
            self.dtr1 = command.param
            return None
        if isinstance(command, gear.DTR2):
            self.dtr2 = command.param
            return None
        if getattr(command, "devicetype", 0) and not (self.is_dt8 and enabled == 8):
            return command.response(None) if command.is_query else None
        if isinstance(command, dt8.SetTemporaryColourTemperature):
            self.temporary_tc = self.dtr0 | (self.dtr1 << 8)
            return None
        if isinstance(command, dt8.SetTemporaryRGBWAFControl):
            self.rgbwaf_control = self.dtr0
            return None
        if isinstance(command, dt8.SetTemporaryRGBDimLevel):
            self.temporary_rgb = (self.dtr0, self.dtr1, self.dtr2)
            return None
        if isinstance(command, dt8.SetTemporaryWAFDimLevel):
            self.temporary_waf = (self.dtr0, self.dtr1, self.dtr2)
            return None
        if isinstance(command, dt8.Activate):
            self.activate()
            return None
        if isinstance(command, gear.DAPC):
            power = command.power
            self.level = 0 if power == 0 else min(max(power, self.min_level), self.max_level)
            if self.auto_activation:
                self.activate()
            return None
        if isinstance(command, gear.Off):
            self.level = 0
            if self.auto_activation:
                self.activate()
            return None
        if not getattr(command, "is_query", False):
            return None
        answer = self._answer(command)
        return command.response(None if answer is None else BackwardFrame(answer))

    def activate(self):
        """Apply the temporary colour, as ACTIVATE or an auto-activating level command."""
        if self.temporary_tc is not None:
            self.tc = self.temporary_tc
            self.active = "tc"
        if self.temporary_rgb is not None or self.temporary_waf is not None:
            if self.rgbwaf_control & 0x0F:
                raise AssertionError("RGBW dim levels sent while channels are linked")
            red, green, blue = self.temporary_rgb or (MASK8, MASK8, MASK8)
            white, amber, free = self.temporary_waf or (MASK8, MASK8, MASK8)
            if amber != MASK8:
                self.amber = amber
            if free != MASK8:
                self.free = free
            old = self.rgbw or (0, 0, 0, 0)
            self.rgbw = tuple(
                o if n == MASK8 else n for o, n in zip(old, (red, green, blue, white))
            )
            self.active = "rgbw"
        self.temporary_tc = self.temporary_rgb = self.temporary_waf = None

    def _answer(self, command):
        if isinstance(command, gear.QueryControlGearPresent):
            return 0xFF
        if isinstance(command, gear.QueryPhysicalMinimum):
            return 1
        if isinstance(command, gear.QueryMinLevel):
            return self.min_level
        if isinstance(command, gear.QueryMaxLevel):
            return self.max_level
        if isinstance(command, gear.QueryActualLevel):
            return self.level
        if isinstance(command, gear.QueryContentDTR0):
            return self.dtr0
        if isinstance(command, gear.QueryDeviceType):
            if len(self.device_types) == 1:
                return self.device_types[0]
            self._next_types = list(self.device_types)
            return 255
        if isinstance(command, gear.QueryNextDeviceType):
            return self._next_types.pop(0) if self._next_types else 254
        if isinstance(command, dt8.QueryColourTypeFeatures):
            return (
                (1 if self.xy else 0)
                | (2 if self.tc_range else 0)
                | (self.primaries << 2)
                | (self.rgbwaf_channels << 5)
            )
        if isinstance(command, dt8.QueryGearFeaturesStatus):
            return 1 if self.auto_activation else 0
        if isinstance(command, dt8.QueryColourStatus):
            if self.status_all_bits:
                return 0xF0
            return self._colour_type()
        if isinstance(command, dt8.QueryColourValue):
            key = self.dtr0
            value = self._colour_value(key)
            if value is None:
                return None
            if key in EIGHT_BIT:
                self.dtr0 = value if self.echo_byte else 0
                return value
            self.dtr0 = value & 0xFF
            return value >> 8
        return None

    def _colour_type(self):
        return {"xy": 0x10, "tc": 0x20, "rgbw": 0x80}.get(self.active, 0)

    def _colour_value(self, key):
        Q = dt8.QueryColourValueDTR
        if key == Q.ReportColourType:
            return self._colour_type() or MASK8
        if key == Q.ColourTemperatureTC:
            return MASK16 if self.active != "tc" else self.tc
        if key == Q.ColourTemperatureTcCoolest and self.tc_range:
            return self.tc_range[0] if self.tc_limits_set else MASK16
        if key == Q.ColourTemperatureTcWarmest and self.tc_range:
            return self.tc_range[1] if self.tc_limits_set else MASK16
        if key == Q.ColourTemperatureTcPhysicalCoolest and self.tc_range:
            return self.tc_physical_range[0]
        if key == Q.ColourTemperatureTcPhysicalWarmest and self.tc_range:
            return self.tc_physical_range[1]
        if key in (Q.XCoordinate, Q.YCoordinate):
            if self.active != "xy":
                return MASK16
            return self.xy_value[0 if key == Q.XCoordinate else 1]
        channels = (Q.RedDimLevel, Q.GreenDimLevel, Q.BlueDimLevel, Q.WhiteDimLevel)
        if key in channels:
            if self.active != "rgbw" or self.rgbw is None:
                return MASK8
            return self.rgbw[channels.index(key)]
        return None
