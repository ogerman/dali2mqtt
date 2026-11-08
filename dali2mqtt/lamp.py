"""Class to represent dali lamps."""
import json
import logging

import dali.address as address
import dali.gear.general as gear
from dali.exceptions import DALIError
from dali2mqtt import colour
from dali2mqtt.consts import (
    ALL_SUPPORTED_LOG_LEVELS,
    LOG_FORMAT,
    MQTT_AVAILABLE,
    MQTT_BRIGHTNESS_COMMAND_TOPIC,
    MQTT_BRIGHTNESS_STATE_TOPIC,
    MQTT_COLOR_MODE_STATE_TOPIC,
    MQTT_COLOR_TEMP_COMMAND_TOPIC,
    MQTT_COLOR_TEMP_STATE_TOPIC,
    MQTT_COMMAND_TOPIC,
    MQTT_DALI2MQTT_STATUS,
    MQTT_NOT_AVAILABLE,
    MQTT_PAYLOAD_OFF,
    MQTT_PAYLOAD_ON,
    MQTT_RGBW_COMMAND_TOPIC,
    MQTT_RGBW_STATE_TOPIC,
    MQTT_STATE_TOPIC,
    __version__,
)
from slugify import slugify

logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# Colour types exposed to Home Assistant and the colour mode names it uses.
HA_COLOR_MODES = {colour.COLOR_TEMP: "color_temp", colour.RGBWAF: "rgbw"}


class Lamp:
    """Representation of a DALI Lamp."""

    def __init__(
        self,
        log_level,
        driver,
        friendly_name,
        short_address,
        members=None,
    ):
        """Initialize Lamp.

        For a group, ``members`` are the Lamp objects of its member lamps.
        """
        self.driver = driver
        # Home Assistant unique ids carry the DALI driver's class name; a Bus
        # wrapper reports the name of the driver it wraps.
        self.driver_name = (
            driver.driver_name
            if isinstance(getattr(driver, "driver_name", None), str)
            else type(driver).__name__
        )
        self.short_address = short_address
        self.friendly_name = friendly_name
        self.is_group = isinstance(short_address, address.Group)
        self.members = list(members or [])
        # Fade time in seconds, filled in by LevelSync; None while unknown.
        self.fade_time = None
        # IEC 62386 part 2xx device types; 8 is colour control.
        self.device_types = []
        # colour.ColourFeatures for colour control gear, else None.
        self.colour = None
        # (coolest, warmest) colour temperature in mirek, None when unknown.
        self.colour_temp_range = None
        # Active colour type (a colour.* constant), None when unknown.
        self.colour_mode = None
        self.__colour_temp = None  # mirek
        self.__rgbw = None  # (red, green, blue, white) dim levels 0..254

        self.device_name = slugify(friendly_name)

        logger.setLevel(ALL_SUPPORTED_LOG_LEVELS[log_level])

        # Groups can't be queried: every member would answer at once.
        if self.is_group:
            self.min_physical_level = None
            self.min_level, self.max_level = self._group_range(members)
            self.colour, self.colour_temp_range = self._group_colour(members)
            # Set level directly without calling setter (which would send DAPC command)
            self.__level = 0  # Default to off, can't query actual level for groups
            logger.debug("Initialized group lamp %s with default values", friendly_name)
        else:
            self.min_physical_level = self._query_level(gear.QueryPhysicalMinimum)
            if self.min_physical_level is None:
                logger.warning("%s doesn't report its physical minimum", friendly_name)
            self.min_level = self._query_level(gear.QueryMinLevel, required=True)
            self.max_level = self._query_level(gear.QueryMaxLevel, required=True)
            # Recorded, not sent: the lamp already is at this level.
            self.__level = self._query_level(gear.QueryActualLevel)
            if self.__level is None:
                self.__level = 0
                logger.warning("%s doesn't report its level, assuming off", friendly_name)
            self._query_colour()

    def _query_level(self, command, required=False, attempts=3):
        """Return a lamp's 8 bit answer to ``command``, or None.

        Some gear loses answers now and then, so a missing one is asked again.
        A ``required`` value that never comes raises DALIError, so the lamp is
        skipped rather than set up with a placeholder in place of a number.
        """
        response = None
        for _ in range(attempts):
            response = self.driver.send(command(self.short_address))
            value = getattr(response, "value", None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        if required:
            raise DALIError(
                f"{self.friendly_name} gave no answer to {command.__name__}: {response}"
            )
        return None

    def _query_colour(self):
        """Find out whether the gear controls colour, what it supports and shows."""
        self.device_types = colour.query_device_types(self.driver, self.short_address)
        if colour.DT8 not in self.device_types:
            return
        self.colour = colour.query_colour_features(self.driver, self.short_address)
        if self.colour is None:
            return
        if colour.COLOR_TEMP in self.colour.types:
            self.colour_temp_range = colour.query_colour_temp_range(
                self.driver, self.short_address
            )
            if self.colour_temp_range is None:
                self.colour_temp_range = colour.TC_DEFAULT_RANGE
                logger.warning(
                    "%s supports colour temperature but reports no usable range, "
                    "assuming %s..%s mirek",
                    self.friendly_name,
                    *colour.TC_DEFAULT_RANGE,
                )
        self.update_colour(colour.read_colour_state(self.driver, self.short_address))

    @staticmethod
    def _group_range(members):
        """Return the (min, max) level range in which any group member responds.

        Members with a higher minimum stay at their own minimum below it.
        """
        levels = [
            (m.min_level, m.max_level)
            for m in members or []
            if isinstance(m.min_level, int) and isinstance(m.max_level, int)
        ]
        if not levels:
            return 1, 254  # Full DALI range
        return min(lo for lo, _ in levels), max(hi for _, hi in levels)

    @staticmethod
    def _group_colour(members):
        """Return the colour features and Tc range shared by every member.

        A group only offers what all its members can do, within the colour
        temperature range they all render.
        """
        members = list(members or [])
        if not members or any(m.colour is None for m in members):
            return None, None
        types = frozenset(members[0].colour.types)
        for member in members[1:]:
            types &= frozenset(member.colour.types)
        features = colour.ColourFeatures(
            types,
            primaries=min(m.colour.primaries for m in members),
            rgbwaf_channels=min(m.colour.rgbwaf_channels for m in members),
            auto_activation=all(m.colour.auto_activation for m in members),
        )
        ranges = [m.colour_temp_range for m in members]
        if colour.COLOR_TEMP not in features.types or any(r is None for r in ranges):
            return features, None
        coolest = max(r[0] for r in ranges)
        warmest = min(r[1] for r in ranges)
        if coolest > warmest:
            return features, None
        return features, (coolest, warmest)

    @property
    def dimmable(self):
        """Return True if the lamp has more than one light level."""
        return self.max_level > self.min_level

    @property
    def has_colour_temp(self):
        """Return True if the lamp's colour temperature can be set."""
        return (
            self.colour is not None
            and colour.COLOR_TEMP in self.colour.types
            and self.colour_temp_range is not None
        )

    @property
    def has_rgbw(self):
        """Return True if the lamp has red, green, blue and white channels."""
        return (
            self.colour is not None
            and colour.RGBWAF in self.colour.types
            and self.colour.rgbwaf_channels >= 4
        )

    @property
    def ha_colour_mode(self):
        """Return the Home Assistant colour mode name, or None if not exposed."""
        exposed = {
            colour.COLOR_TEMP: self.has_colour_temp,
            colour.RGBWAF: self.has_rgbw,
        }
        if exposed.get(self.colour_mode):
            return HA_COLOR_MODES[self.colour_mode]
        return None

    def level_to_ha(self, level):
        """Convert a DALI level to Home Assistant brightness (0 = off).

        Home Assistant brightness 1..bri_scl maps onto min_level..max_level.
        """
        if not isinstance(level, int) or level <= 0:
            return 0
        level = min(max(level, self.min_level), self.max_level)
        return level - self.min_level + 1

    def ha_to_level(self, brightness):
        """Convert Home Assistant brightness to a DALI level (0 = off)."""
        if brightness <= 0:
            return 0
        return min(self.min_level + brightness - 1, self.max_level)

    def gen_ha_config(self, mqtt_base_topic):
        """Generate a automatic configuration for Home Assistant."""
        json_config = {
            "name": self.friendly_name,
            "def_ent_id": f"dali_light_{self.device_name}",
            "uniq_id": f"{self.driver_name}_{self.short_address}",
            "stat_t": MQTT_STATE_TOPIC.format(mqtt_base_topic, self.device_name),
            "cmd_t": MQTT_COMMAND_TOPIC.format(mqtt_base_topic, self.device_name),
            "pl_on": MQTT_PAYLOAD_ON.decode("utf-8"),
            "pl_off": MQTT_PAYLOAD_OFF.decode("utf-8"),
            "avty_t": MQTT_DALI2MQTT_STATUS.format(mqtt_base_topic),
            "pl_avail": MQTT_AVAILABLE,
            "pl_not_avail": MQTT_NOT_AVAILABLE,
            "device": {
                "ids": "dali2mqtt",
                "name": "DALI Lights",
                "sw": f"dali2mqtt {__version__}",
                "mdl": f"{self.driver_name}",
                "mf": "dali2mqtt",
            },
        }
        if self.dimmable:
            json_config.update(
                {
                    "bri_stat_t": MQTT_BRIGHTNESS_STATE_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                    "bri_cmd_t": MQTT_BRIGHTNESS_COMMAND_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                    "bri_scl": self.level_to_ha(self.max_level),
                    "on_cmd_type": "brightness",
                }
            )
        if self.has_colour_temp:
            # Home Assistant mireds are DALI mirek; coolest is the lower number.
            coolest, warmest = self.colour_temp_range
            json_config.update(
                {
                    "clr_temp_stat_t": MQTT_COLOR_TEMP_STATE_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                    "clr_temp_cmd_t": MQTT_COLOR_TEMP_COMMAND_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                    "min_mirs": coolest,
                    "max_mirs": warmest,
                }
            )
        if self.has_rgbw:
            json_config.update(
                {
                    "rgbw_stat_t": MQTT_RGBW_STATE_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                    "rgbw_cmd_t": MQTT_RGBW_COMMAND_TOPIC.format(
                        mqtt_base_topic, self.device_name
                    ),
                }
            )
        if self.has_colour_temp or self.has_rgbw:
            json_config["clrm_stat_t"] = MQTT_COLOR_MODE_STATE_TOPIC.format(
                mqtt_base_topic, self.device_name
            )
        return json.dumps(json_config)

    def actual_level(self):
        """Retrieve actual level from ballast."""
        if self.is_group:
            # Groups don't support query operations, keep current level
            logger.debug("Cannot query actual level for group %s", self.friendly_name)
            return
        self.__level = self.driver.send(gear.QueryActualLevel(self.short_address)).value

    @property
    def level(self):
        """Return brightness level."""
        return self.__level

    def update_level(self, level):
        """Record a level observed on the bus without sending anything.

        Returns True when the level changed.
        """
        if level == self.__level:
            return False
        self.__level = level
        logger.debug("Lamp <%s> level is now %s", self.friendly_name, level)
        return True

    @level.setter
    def level(self, value):
        """Commit level to ballast."""
        if not self.min_level <= value <= self.max_level and value != 0:
            raise ValueError
        self.__level = value
        self.driver.send(gear.DAPC(self.short_address, self.level))
        logger.debug(
            "Set lamp <%s> brightness level to %s", self.friendly_name, self.level
        )

    # Colour

    @property
    def colour_temp(self):
        """Return the colour temperature in mirek, None while unknown."""
        return self.__colour_temp

    @property
    def rgbw(self):
        """Return (red, green, blue, white) dim levels, None while unknown."""
        return self.__rgbw

    @property
    def rgbw_ha(self):
        """Return the RGBW levels as Home Assistant's "r,g,b,w" payload, or None."""
        if self.__rgbw is None:
            return None
        return ",".join(str(colour.dim_to_ha(level)) for level in self.__rgbw)

    def colour_state(self):
        """Return the lamp's colour as a colour.ColourState."""
        return colour.ColourState(self.colour_mode, self.__colour_temp, self.__rgbw)

    def update_colour(self, state):
        """Record colour observed on the bus without sending anything.

        Only the active colour type's value is taken from ``state``; the
        others keep their last known value. Returns True when anything changed.
        """
        if state is None or state.mode is None:
            return False
        changed = state.mode != self.colour_mode
        self.colour_mode = state.mode
        if state.mode == colour.COLOR_TEMP and state.colour_temp is not None:
            changed |= state.colour_temp != self.__colour_temp
            self.__colour_temp = state.colour_temp
        elif state.mode == colour.RGBWAF and state.rgbw is not None:
            changed |= state.rgbw != self.__rgbw
            self.__rgbw = state.rgbw
        if changed:
            logger.debug("Lamp <%s> colour is now %s", self.friendly_name, self.colour_state())
        return changed

    def update_colour_temp(self, mirek):
        """Record a colour temperature observed on the bus. Returns True on change."""
        return self.update_colour(colour.ColourState(colour.COLOR_TEMP, colour_temp=mirek))

    def _lit_levels(self):
        """Return [(address, level)] of the lamps this lamp addresses that are on."""
        lamps = self.members if self.is_group else [self]
        return [
            (lamp.short_address, lamp.level)
            for lamp in lamps
            if isinstance(lamp.level, int) and 0 < lamp.level < 255
        ]

    def _apply_colour(self, write):
        """Run ``write()`` and keep the lamps at their brightness.

        The colour is always activated with ACTIVATE. Leaving that to a DAPC
        on gear that reports automatic activation did not work on the real
        bus: office-rgb claims the feature, but its answers are unreliable
        and the colour never changed.

        Colour gear has been seen jumping to full brightness on a colour
        change, and the hardware colour picker on the same bus re-sends the
        level after every colour. So the level of every lit lamp is sent
        again afterwards.
        """
        lit = self._lit_levels()
        result = write()
        for addr, level in lit:
            self.driver.send(gear.DAPC(addr, level))
        return result

    def set_colour_temp(self, mirek):
        """Commit a colour temperature (mirek) to the ballast.

        The value is clamped to the range the gear renders; the value actually
        set is returned.
        """
        if not self.has_colour_temp:
            raise ValueError(f"{self.friendly_name} has no colour temperature control")
        coolest, warmest = self.colour_temp_range
        mirek = min(max(int(mirek), coolest), warmest)
        self._apply_colour(
            lambda: colour.set_colour_temp(self.driver, self.short_address, mirek)
        )
        self.update_colour_temp(mirek)
        for member in self.members:
            if member.has_colour_temp:
                lo, hi = member.colour_temp_range
                member.update_colour_temp(min(max(mirek, lo), hi))
        logger.debug(
            "Set lamp <%s> colour temperature to %s mirek", self.friendly_name, mirek
        )
        return mirek

    def set_rgbw(self, rgbw):
        """Commit Home Assistant RGBW channel values (0..255 each) to the ballast.

        Returns the DALI dim levels actually set.
        """
        if not self.has_rgbw:
            raise ValueError(f"{self.friendly_name} has no RGBW control")
        values = tuple(rgbw)
        if len(values) != 4:
            raise ValueError(f"RGBW needs four values, got {values!r}")
        levels = self._apply_colour(
            lambda: colour.set_rgbw(
                self.driver, self.short_address, [colour.ha_to_dim(v) for v in values]
            )
        )
        state = colour.ColourState(colour.RGBWAF, rgbw=levels)
        self.update_colour(state)
        for member in self.members:
            if member.has_rgbw:
                member.update_colour(state)
        logger.debug("Set lamp <%s> RGBW to %s", self.friendly_name, levels)
        return levels

    def off(self):
        """Turn off ballast."""
        self.driver.send(gear.Off(self.short_address))

    def __str__(self):
        """Serialize lamp information."""
        # Handle both Short (has .address) and Group (no .address) objects
        if self.is_group:
            addr_str = str(self.short_address)
        else:
            addr_str = str(self.short_address.address)
        text = (
            f"{self.device_name} - address: {addr_str}, "
            f"actual brightness level: {self.level} (minimum: {self.min_level}, "
            f"max: {self.max_level}, physical minimum: {self.min_physical_level})"
        )
        if self.colour is not None:
            text += f", colour: {self.colour}"
        if self.has_colour_temp:
            coolest, warmest = self.colour_temp_range
            text += f", colour temperature: {self.colour_temp} ({coolest}..{warmest} mirek)"
        if self.has_rgbw:
            text += f", RGBW: {self.rgbw}"
        if self.colour_mode is not None:
            text += f", active colour type: {self.colour_mode}"
        return text
