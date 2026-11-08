"""Configuration Object."""
import logging

import voluptuous as vol
import yaml
from dali2mqtt.consts import ALL_SUPPORTED_LOG_LEVELS, LOG_FORMAT

logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger(__name__)


def _kelvin_range(value):
    low, high = value
    if low >= high:
        raise vol.Invalid("colour_temp_range must be [warmest, coolest] kelvin, warmest first")
    return [low, high]


KELVIN = vol.All(vol.Coerce(int), vol.Range(min=1000, max=20000))

GAINS = vol.All(
    [vol.All(vol.Coerce(float), vol.Range(min=0.05, max=1.0))],
    vol.Length(min=3, max=3),
)

# Per-lamp settings next to friendly_name, for what changes how dali2mqtt
# drives the lamp and can't be read from the bus.
LAMP_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional("friendly_name"): vol.Coerce(str),
        # Strict: a misspelt colour setting is reported, not silently ignored.
        vol.Optional("colour"): vol.Schema({
            # Colour temperature the strip's white LED really shows.
            vol.Optional("white_kelvin"): KELVIN,
            # Red, green, blue white balance of the strip at 2700 K and 6500 K.
            vol.Optional("rgb_gains"): GAINS,
            vol.Optional("rgb_gains_cool"): GAINS,
            # mixed: from the RGBW channels; native: the gear's own; off: none.
            vol.Optional("colour_temp"): vol.In(["mixed", "native", "off"]),
            vol.Optional("colour_temp_range"): vol.All(
                [KELVIN], vol.Length(min=2, max=2), _kelvin_range
            ),
        }, extra=vol.PREVENT_EXTRA),
    },
    extra=vol.ALLOW_EXTRA,
)


class DevicesNamesConfigLoadError(Exception):
    """Exception class for DevicesNamesConfig."""

    pass


class DevicesNamesConfig:
    """Devices Names Configuration."""

    def __init__(self, log_level, filename):
        """Initialize devices names config."""
        self._path = filename
        self._devices_names = {}

        logger.setLevel(ALL_SUPPORTED_LOG_LEVELS[log_level])
        # Load from file
        try:
            self.load_devices_names_file()
        except FileNotFoundError:
            logger.info("No device names config, creating new one")
            with open(self._path, "w"):
                pass

    def load_devices_names_file(self):
        """Load configuration from yaml file."""
        try:
            with open(self._path, "r") as infile:
                logger.debug("Loading devices names from <%s>", self._path)
                self._devices_names = yaml.safe_load(infile) or {}
        except yaml.YAMLError as error:
            logger.error("In devices file %s: %s", self._path, error)
            raise DevicesNamesConfigLoadError()
        except Exception:
            logger.error(
                "Could not load device names config <%s>, a new one will be created after successfull start",
                self._path,
            )

    def save_devices_names_file(self, all_lamps):
        """Save configuration back to yaml file."""
        self._devices_names = {}
        for lamp_object in all_lamps.values():
            # Skip groups - they don't have .address attribute
            if hasattr(lamp_object.short_address, 'address'):
                self._devices_names[lamp_object.short_address.address] = {
                    "friendly_name": str(lamp_object.short_address.address)
                }
        try:
            with open(self._path, "w") as outfile:
                yaml.dump(
                    self._devices_names,
                    outfile,
                    default_flow_style=False,
                    allow_unicode=True,
                )
        except Exception as err:
            logger.error("Could not save device names config: %s", err)

    def is_devices_file_empty(self) -> bool:
        """Check if we have any device configured."""
        return len(self._devices_names) == 0

    def get_lamp_settings(self, short_address_value) -> dict:
        """Return the validated settings of a lamp, {} when there are none.

        Invalid settings are reported and ignored, so one typo doesn't stop
        the lamp from working.
        """
        entry = self._devices_names.get(short_address_value)
        if not isinstance(entry, dict):
            return {}
        try:
            return LAMP_SETTINGS_SCHEMA(entry)
        except vol.Invalid as err:
            logger.error(
                "Ignoring settings of lamp %s in %s: %s", short_address_value, self._path, err
            )
            return {}

    def get_friendly_name(self, short_address_value) -> str:
        """Retrieve friendly_name."""
        if short_address_value in self._devices_names:
            return self._devices_names[short_address_value].get(
                "friendly_name", f"{short_address_value}"
            )
        return str(short_address_value)
