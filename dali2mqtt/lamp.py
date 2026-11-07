"""Class to represent dali lamps."""
import json
import logging

import dali.address as address
import dali.gear.general as gear
from dali2mqtt.consts import (
    ALL_SUPPORTED_LOG_LEVELS,
    LOG_FORMAT,
    MQTT_AVAILABLE,
    MQTT_BRIGHTNESS_COMMAND_TOPIC,
    MQTT_BRIGHTNESS_STATE_TOPIC,
    MQTT_COMMAND_TOPIC,
    MQTT_DALI2MQTT_STATUS,
    MQTT_NOT_AVAILABLE,
    MQTT_PAYLOAD_OFF,
    MQTT_PAYLOAD_ON,
    MQTT_STATE_TOPIC,
    __version__,
)
from slugify import slugify

logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger(__name__)


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
        self.short_address = short_address
        self.friendly_name = friendly_name
        self.is_group = isinstance(short_address, address.Group)

        self.device_name = slugify(friendly_name)

        logger.setLevel(ALL_SUPPORTED_LOG_LEVELS[log_level])

        # Groups can't be queried: every member would answer at once.
        if self.is_group:
            self.min_physical_level = None
            self.min_level, self.max_level = self._group_range(members)
            # Set level directly without calling setter (which would send DAPC command)
            self.__level = 0  # Default to off, can't query actual level for groups
            logger.debug("Initialized group lamp %s with default values", friendly_name)
        else:
            _min_physical_level = driver.send(gear.QueryPhysicalMinimum(short_address))

            try:
                self.min_physical_level = _min_physical_level.value
            except Exception as err:
                self.min_physical_level = None
                logger.warning(
                    "Set min_physical_level to None as %s failed: %s",
                    _min_physical_level,
                    err,
                )
            self.min_level = driver.send(gear.QueryMinLevel(short_address)).value
            self.max_level = driver.send(gear.QueryMaxLevel(short_address)).value
            self.level = driver.send(gear.QueryActualLevel(short_address)).value

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

    @property
    def dimmable(self):
        """Return True if the lamp has more than one light level."""
        return self.max_level > self.min_level

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
            "uniq_id": f"{type(self.driver).__name__}_{self.short_address}",
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
                "mdl": f"{type(self.driver).__name__}",
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
        return (
            f"{self.device_name} - address: {addr_str}, "
            f"actual brightness level: {self.level} (minimum: {self.min_level}, "
            f"max: {self.max_level}, physical minimum: {self.min_physical_level})"
        )
