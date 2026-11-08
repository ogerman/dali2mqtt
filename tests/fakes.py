"""Scripted DALI gear for tests: answers queries by command type.

Unlike a mock with a fixed answer list, ``FakeGear`` doesn't care how many
queries a Lamp sends or in which order. It emulates one control gear,
optionally a device type 8 colour gear with colour temperature control, and
the DTR registers the 16 bit colour transfers go through.
"""
import dali.gear.general as gear
from dali.frame import BackwardFrame
from dali.gear import colour as dt8

MASK16 = 65535


class FakeGear:
    """One control gear behind a driver's ``send``."""

    def __init__(
        self,
        min_level=85,
        max_level=254,
        level=0,
        device_types=(6,),
        tc_range=None,
        tc=None,
        xy=False,
        primaries=0,
        rgbwaf_channels=0,
        auto_activation=False,
    ):
        self.min_level = min_level
        self.max_level = max_level
        self.level = level
        self.device_types = list(device_types)
        self.tc_range = tc_range  # (coolest, warmest) when Tc capable
        self.tc = tc  # current colour temperature, None when not in Tc mode
        self.xy = xy
        self.primaries = primaries
        self.rgbwaf_channels = rgbwaf_channels
        self.auto_activation = auto_activation
        self.dtr0 = 0
        self.dtr1 = 0
        self.temporary_tc = None
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
        if getattr(command, "devicetype", 0) and not (self.is_dt8 and enabled == 8):
            return None if not command.is_query else command.response(None)
        if isinstance(command, dt8.SetTemporaryColourTemperature):
            self.temporary_tc = self.dtr0 | (self.dtr1 << 8)
            return None
        if isinstance(command, dt8.Activate):
            if self.temporary_tc is not None:
                self.tc = self.temporary_tc
                self.temporary_tc = None
            return None
        if isinstance(command, gear.DAPC):
            power = command.power
            self.level = 0 if power == 0 else min(max(power, self.min_level), self.max_level)
            return None
        if isinstance(command, gear.Off):
            self.level = 0
            return None
        if not getattr(command, "is_query", False):
            return None
        answer = self._answer(command)
        return command.response(None if answer is None else BackwardFrame(answer))

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
            return 0x20 if self.tc is not None else 0
        if isinstance(command, dt8.QueryColourValue):
            value = self._colour_value(self.dtr0)
            if value is None:
                return None
            self.dtr0 = value & 0xFF
            return value >> 8
        return None

    def _colour_value(self, key):
        Q = dt8.QueryColourValueDTR
        if key == Q.ColourTemperatureTC:
            return MASK16 if self.tc is None else self.tc
        if key == Q.ColourTemperatureTcCoolest and self.tc_range:
            return self.tc_range[0]
        if key == Q.ColourTemperatureTcWarmest and self.tc_range:
            return self.tc_range[1]
        return None
