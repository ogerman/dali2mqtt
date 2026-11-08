import argparse
import logging
import random
import re
import time
import os

import paho.mqtt.client as mqtt

import dali.address as address
import dali.gear.general as gear
from dali.command import YesNoResponse
from dali.exceptions import DALIError

from dali2mqtt.bus import Bus
from dali2mqtt.devicesnamesconfig import DevicesNamesConfig
from dali2mqtt import colour
from dali2mqtt.lamp import HA_COLOR_MODES, Lamp
from dali2mqtt.levelsync import LevelSync
from dali2mqtt.config import Config
from dali2mqtt.consts import (
    ALL_SUPPORTED_LOG_LEVELS,
    CONF_CONFIG,
    CONF_DALI_DRIVER,
    CONF_DALI_LAMPS,
    CONF_DALI_POLL_INTERVAL,
    CONF_DALI_SNIFF,
    CONF_DEVICES_NAMES_FILE,
    CONF_HA_DISCOVERY_PREFIX,
    CONF_LOG_COLOR,
    CONF_LOG_LEVEL,
    CONF_MQTT_BASE_TOPIC,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_SERVER,
    CONF_MQTT_USERNAME,
    DALI_DRIVERS,
    DALI_SERVER,
    DEFAULT_CONFIG_FILE,
    DEFAULT_HA_DISCOVERY_PREFIX,
    HA_DISCOVERY_PREFIX,
    HA_STATUS_TOPIC,
    HA_STATUS_ONLINE,
    HASSEB,
    LOG_FORMAT,
    MAX_RETRIES,
    MIN_BACKOFF_TIME,
    MAX_BACKOFF_TIME,
    MIN_HASSEB_FIRMWARE_VERSION,
    MQTT_AVAILABLE,
    MQTT_BRIGHTNESS_COMMAND_TOPIC,
    MQTT_BRIGHTNESS_GET_COMMAND_TOPIC,
    MQTT_BRIGHTNESS_MAX_LEVEL_TOPIC,
    MQTT_BRIGHTNESS_MIN_LEVEL_TOPIC,
    MQTT_BRIGHTNESS_PHYSICAL_MINIMUM_LEVEL_TOPIC,
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
    MQTT_SCAN_LAMPS_COMMAND_TOPIC,
    MQTT_STATE_TOPIC,
    RED_COLOR,
    TRIDONIC,
    YELLOW_COLOR,
)


logging.basicConfig(format=LOG_FORMAT, level=os.environ.get("LOGLEVEL", "INFO"))
logger = logging.getLogger(__name__)


def _gear_present(dali_driver, addr):
    """Return True when control gear answers QUERY CONTROL GEAR PRESENT."""
    present = dali_driver.send(gear.QueryControlGearPresent(addr))
    return isinstance(present, YesNoResponse) and bool(present.value)


def dali_scan(dali_driver, attempts=2):
    """Return the short addresses (0..63) of the control gear on the bus.

    An address that stays silent is asked again, since an answer can get
    lost. Gear without a short address can't be used; it is reported.
    """
    lamps = []
    for lamp in range(64):
        for attempt in range(attempts):
            try:
                logging.debug("Search for Lamp %s", lamp)
                if _gear_present(dali_driver, address.Short(lamp)):
                    lamps.append(lamp)
                    logger.debug("Found lamp at address %d", lamp)
                    if attempt:
                        logger.info("Lamp %d only answered on attempt %d", lamp, attempt + 1)
                    break
            except DALIError as err:
                logger.warning("%s not present: %s", lamp, err)
                break
    try:
        answer = dali_driver.send(gear.QueryControlGearPresent(address.BroadcastUnaddressed()))
        if isinstance(answer, YesNoResponse) and answer.value:
            garbled = bool(getattr(answer.raw_value, "error", False))
            logger.warning(
                "Control gear without a short address is on the bus (%s answer); it "
                "can't be controlled until it is given one (commissioning)",
                "garbled, possibly several gear" if garbled else "clean",
            )
    except DALIError as err:
        logger.debug("Can't check for gear without a short address: %s", err)
    return lamps


def scan_groups(dali_driver, lamps):
    """Scan for groups."""
    logger.info("Scanning for groups")
    groups = {}
    for lamp in lamps:
        try:
            logging.debug("Search for groups for Lamp {}".format(lamp))
            group1 = dali_driver.send(
                gear.QueryGroupsZeroToSeven(address.Short(lamp))
            ).value.as_integer
            group2 = dali_driver.send(
                gear.QueryGroupsEightToFifteen(address.Short(lamp))
            ).value.as_integer

            #            logger.debug("Group 0-7: %d", group1)
            #            logger.debug("Group 8-15: %d", group2)

            lamp_groups = []

            for i in range(8):
                checkgroup = 1 << i
                logging.debug("Check pattern: %d", checkgroup)
                if (group1 & checkgroup) == checkgroup:
                    if i not in groups:
                        groups[i] = []
                    groups[i].append(lamp)
                    lamp_groups.append(i)
                if (group2 & checkgroup) != 0:
                    if not i + 8 in groups:
                        groups[i + 8] = []
                    groups[i + 8].append(lamp)
                    lamp_groups.append(i + 8)

            logger.debug("Lamp %d is in groups %s", lamp, lamp_groups)

        except Exception as e:
            logger.warning("Can't get groups for lamp %s: %s", lamp, e)
    logger.info("Finished scanning for groups")
    return groups


def read_hasseb_firmware(driver, attempts=3):
    """Return the hasseb firmware version, or None when it can't be read."""
    for attempt in range(attempts):
        try:
            return float(driver.readFirmwareVersion())
        except ValueError:
            logger.warning("Can't read hasseb firmware version, attempt %d", attempt + 1)
            time.sleep(0.5)
    return None


def initialize_lamps(data_object, client):
    """Initialize all lamps and groups."""

    driver = data_object["driver"]
    mqtt_base_topic = data_object["base_topic"]
    ha_prefix = data_object["ha_prefix"]
    log_level = data_object["log_level"]
    devices_names_config = data_object["devices_names_config"]
    devices_names_config.load_devices_names_file()
    lamps = dali_scan(driver)
    logger.info(
        "Found %d lamps",
        len(lamps),
    )

    def create_mqtt_lamp(address, name, members=None):
        try:
            lamp_object = Lamp(
                log_level,
                driver,
                name,
                address,
                members,
            )

            previous = data_object["all_lamps"].get(name)
            if previous is not None:
                # A rescan replaces the lamp object; keep the colour state of
                # gear that can't report its own.
                lamp_object.adopt_colour(previous)
            data_object["all_lamps"][name] = lamp_object

            # Discovery first: Home Assistant rebuilds an entity whose config
            # changed and only learns its state from messages that follow.
            mqtt_data = [
                (
                    HA_DISCOVERY_PREFIX.format(ha_prefix, name),
                    lamp_object.gen_ha_config(mqtt_base_topic),
                    True,
                ),
                (
                    MQTT_BRIGHTNESS_MAX_LEVEL_TOPIC.format(mqtt_base_topic, name),
                    lamp_object.max_level,
                    True,
                ),
                (
                    MQTT_BRIGHTNESS_MIN_LEVEL_TOPIC.format(mqtt_base_topic, name),
                    lamp_object.min_level,
                    True,
                ),
                (
                    MQTT_BRIGHTNESS_PHYSICAL_MINIMUM_LEVEL_TOPIC.format(
                        mqtt_base_topic, name
                    ),
                    lamp_object.min_physical_level,
                    True,
                ),
            ]
            for topic, payload, retain in mqtt_data:
                client.publish(topic, payload, retain=retain)
            publish_lamp_state(client, mqtt_base_topic, lamp_object)

            logger.info(lamp_object)
            return lamp_object

        except DALIError as err:
            logger.error("While initializing <%s> @ %s: %s", name, address, err)
            return None

    lamps_by_address = {}
    for lamp in lamps:
        short_address = address.Short(lamp)

        lamps_by_address[lamp] = create_mqtt_lamp(
            short_address,
            devices_names_config.get_friendly_name(short_address.address),
        )

    groups = scan_groups(driver, lamps)
    for group in groups:
        logger.debug("Publishing group %d", group)

        group_address = address.Group(int(group))

        members = [lamps_by_address.get(lamp) for lamp in groups[group]]
        create_mqtt_lamp(
            group_address, f"group_{group}", [m for m in members if m is not None]
        )

    if devices_names_config.is_devices_file_empty():
        devices_names_config.save_devices_names_file(data_object["all_lamps"])
    if data_object.get("sync") is not None:
        data_object["sync"].set_lamps(data_object["all_lamps"])
    logger.info("initialize_lamps finished")


def publish_lamp_state(mqtt_client, mqtt_base_topic, lamp):
    """Publish a lamp's current on/off state, brightness and colour."""
    mqtt_client.publish(
        MQTT_STATE_TOPIC.format(mqtt_base_topic, lamp.device_name),
        MQTT_PAYLOAD_ON if lamp.level > 0 else MQTT_PAYLOAD_OFF,
        retain=True,
    )
    mqtt_client.publish(
        MQTT_BRIGHTNESS_STATE_TOPIC.format(mqtt_base_topic, lamp.device_name),
        lamp.level_to_ha(lamp.level),
        retain=True,
    )
    publish_colour_state(mqtt_client, mqtt_base_topic, lamp)


def publish_colour_state(mqtt_client, mqtt_base_topic, lamp, retain=True):
    """Publish a lamp's colour temperature, RGBW levels and active colour mode."""
    if lamp.has_colour_temp and lamp.colour_temp is not None:
        mqtt_client.publish(
            MQTT_COLOR_TEMP_STATE_TOPIC.format(mqtt_base_topic, lamp.device_name),
            lamp.colour_temp,
            retain=retain,
        )
    if lamp.has_rgbw and lamp.rgbw_ha is not None:
        mqtt_client.publish(
            MQTT_RGBW_STATE_TOPIC.format(mqtt_base_topic, lamp.device_name),
            lamp.rgbw_ha,
            retain=retain,
        )
    if lamp.ha_colour_mode is not None:
        mqtt_client.publish(
            MQTT_COLOR_MODE_STATE_TOPIC.format(mqtt_base_topic, lamp.device_name),
            lamp.ha_colour_mode,
            retain=retain,
        )


def on_detect_changes_in_config(mqtt_client):
    """Callback when changes are detected in the configuration file."""
    logger.info("Reconnecting to server")
    mqtt_client.disconnect()


def on_message_ha_online(mqtt_client, data_object, msg):
    """Callback on Home Assistant online message."""
    if HA_STATUS_ONLINE in msg.payload:
        logger.info("Home Assistant online on %s: %s", msg.topic, msg.payload)
        initialize_lamps(data_object, mqtt_client)


def on_message_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT command message."""
    logger.debug("Command on %s: %s", msg.topic, msg.payload)
    light = re.search(
        MQTT_COMMAND_TOPIC.format(data_object["base_topic"], "(.+?)"), msg.topic
    ).group(1)
    if msg.payload == MQTT_PAYLOAD_OFF:
        try:
            lamp_object = get_lamp_object(data_object, light)
            logger.debug("Set light <%s> to %s", light, msg.payload)
            lamp_object.off()
            mqtt_client.publish(
                MQTT_STATE_TOPIC.format(data_object["base_topic"], light),
                MQTT_PAYLOAD_OFF,
                retain=True,
            )
        except DALIError as err:
            logger.error("Failed to set light <%s> to OFF: %s", light, err)
        except KeyError:
            logger.error("Lamp %s doesn't exists", light)
    elif msg.payload == MQTT_PAYLOAD_ON:
        try:
            lamp_object = get_lamp_object(data_object, light)
            logger.debug("Set light <%s> to %s", light, msg.payload)
            lamp_object.level = lamp_object.max_level
            mqtt_client.publish(
                MQTT_STATE_TOPIC.format(data_object["base_topic"], light),
                MQTT_PAYLOAD_ON,
                retain=True,
            )
        except DALIError as err:
            logger.error("Failed to set light <%s> to ON: %s", light, err)
        except KeyError:
            logger.error("Lamp %s doesn't exists", light)


def on_message_colour_state(mqtt_client, data_object, msg):
    """Take back the colour state dali2mqtt published before a restart.

    The state topics are retained, so subscribing to them replays the last
    colour mode, colour temperature and RGBW levels of every lamp. Gear that
    can't report its own colour starts with an unknown colour mode, which
    makes Home Assistant hide the brightness too; such a lamp takes the
    replayed state and publishes it again. Lamps that know their colour
    ignore these messages, which also covers dali2mqtt's own publishes.
    """
    base = re.escape(data_object["base_topic"])
    match = re.fullmatch(base + r"/(.+?)/light/(color_mode|color_temp|rgbw)/status", msg.topic)
    if match is None:
        return
    light, kind = match.groups()
    try:
        lamp_object = get_lamp_object(data_object, light)
    except KeyError:
        return
    if lamp_object.colour is None:
        return
    payload = msg.payload.decode("utf-8", "replace").strip()
    if lamp_object.colour_mode is not None:
        # The gear reported its colour; a retained colour temperature can
        # still say that the RGBW levels it reported mix that temperature.
        if kind == "color_temp" and lamp_object._emulated_rgbw is None:
            try:
                mirek = int(float(payload))
            except ValueError:
                return
            if lamp_object.recognise_colour_temp(mirek):
                publish_colour_state(mqtt_client, data_object["base_topic"], lamp_object)
        return
    try:
        if kind == "color_mode":
            mode = {name: mode for mode, name in HA_COLOR_MODES.items()}.get(payload)
            known = lamp_object.seed_colour(mode=mode)
        elif kind == "color_temp":
            known = lamp_object.seed_colour(colour_temp=int(float(payload)))
        else:
            levels = tuple(colour.ha_to_dim(int(float(v))) for v in payload.split(","))
            if len(levels) != 4:
                return
            known = lamp_object.seed_colour(rgbw=levels)
    except ValueError:
        logger.debug("Ignoring retained %s for <%s>: %r", kind, light, payload)
        return
    if known:
        logger.info(
            "Lamp <%s> colour restored from the broker: %s", light, lamp_object.ha_colour_mode
        )
        publish_colour_state(mqtt_client, data_object["base_topic"], lamp_object)


def on_message_reinitialize_lamps_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT scan lamps command message."""
    logger.debug("Reinitialize Command on %s", msg.topic)
    initialize_lamps(data_object, mqtt_client)


def get_lamp_object(data_object, light):
    """Retrieve a lamp by friendly name or by its MQTT topic name.

    Home Assistant uses the slugified name from the discovery config, so a
    group stored as ``group_1`` is addressed as ``group-1``.
    """
    all_lamps = data_object["all_lamps"]
    if light in all_lamps:
        return all_lamps[light]
    for lamp_object in all_lamps.values():
        if lamp_object.device_name == light:
            return lamp_object
    raise KeyError(light)


def on_message_brightness_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT brightness command message."""
    logger.debug("Brightness Command on %s: %s", msg.topic, msg.payload)
    light = re.search(
        MQTT_BRIGHTNESS_COMMAND_TOPIC.format(data_object["base_topic"], "(.+?)"),
        msg.topic,
    ).group(1)
    try:
        lamp_object = get_lamp_object(data_object, light)

        try:
            lamp_object.level = lamp_object.ha_to_level(
                int(msg.payload.decode("utf-8"))
            )
            if lamp_object.level == 0:
                # 0 in DALI is turn off with fade out
                lamp_object.off()
                logger.debug("Set light <%s> to OFF", light)

            mqtt_client.publish(
                MQTT_STATE_TOPIC.format(data_object["base_topic"], light),
                MQTT_PAYLOAD_ON if lamp_object.level != 0 else MQTT_PAYLOAD_OFF,
                retain=False,
            )
            mqtt_client.publish(
                MQTT_BRIGHTNESS_STATE_TOPIC.format(data_object["base_topic"], light),
                lamp_object.level_to_ha(lamp_object.level),
                retain=True,
            )
        except ValueError as err:
            logger.error(
                "Can't convert <%s> to integer %d..%d: %s",
                msg.payload.decode("utf-8"),
                lamp_object.min_level,
                lamp_object.max_level,
                err,
            )
    except KeyError:
        logger.error("Lamp %s doesn't exists", light)


def on_message_color_temp_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT colour temperature command message (mireds)."""
    logger.debug("Color temperature Command on %s: %s", msg.topic, msg.payload)
    light = re.search(
        MQTT_COLOR_TEMP_COMMAND_TOPIC.format(data_object["base_topic"], "(.+?)"),
        msg.topic,
    ).group(1)
    try:
        lamp_object = get_lamp_object(data_object, light)
    except KeyError:
        logger.error("Lamp %s doesn't exists", light)
        return
    payload = msg.payload.decode("utf-8", "replace")
    try:
        mirek = lamp_object.set_colour_temp(int(float(payload)))
    except ValueError as err:
        logger.error("Can't set colour temperature of <%s> to <%s>: %s", light, payload, err)
        return
    except DALIError as err:
        logger.error("Failed to set colour temperature of <%s>: %s", light, err)
        return
    logger.debug("Set light <%s> colour temperature to %s mirek", light, mirek)
    publish_colour_state(mqtt_client, data_object["base_topic"], lamp_object)


def on_message_rgbw_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT RGBW command message ("r,g,b,w", 0..255 each)."""
    logger.debug("RGBW Command on %s: %s", msg.topic, msg.payload)
    light = re.search(
        MQTT_RGBW_COMMAND_TOPIC.format(data_object["base_topic"], "(.+?)"), msg.topic
    ).group(1)
    try:
        lamp_object = get_lamp_object(data_object, light)
    except KeyError:
        logger.error("Lamp %s doesn't exists", light)
        return
    payload = msg.payload.decode("utf-8", "replace")
    try:
        levels = lamp_object.set_rgbw(int(float(v)) for v in payload.split(","))
    except ValueError as err:
        logger.error("Can't set RGBW of <%s> to <%s>: %s", light, payload, err)
        return
    except DALIError as err:
        logger.error("Failed to set RGBW of <%s>: %s", light, err)
        return
    logger.debug("Set light <%s> RGBW to %s", light, levels)
    publish_colour_state(mqtt_client, data_object["base_topic"], lamp_object)


def on_message_brightness_get_cmd(mqtt_client, data_object, msg):
    """Callback on MQTT brightness get command message."""
    logger.debug("Brightness Get Command on %s: %s", msg.topic, msg.payload)
    light = re.search(
        MQTT_BRIGHTNESS_GET_COMMAND_TOPIC.format(data_object["base_topic"], "(.+?)"),
        msg.topic,
    ).group(1)
    try:
        lamp_object = get_lamp_object(data_object, light)

        try:
            sync = data_object.get("sync")
            if sync is not None:
                sync.refresh(lamp_object)  # also updates the groups it belongs to
            else:
                lamp_object.actual_level()
            logger.debug("Get light <%s> results in %d", light, lamp_object.level)

            mqtt_client.publish(
                MQTT_BRIGHTNESS_STATE_TOPIC.format(data_object["base_topic"], light),
                lamp_object.level_to_ha(lamp_object.level),
                retain=False,
            )

            mqtt_client.publish(
                MQTT_STATE_TOPIC.format(data_object["base_topic"], light),
                MQTT_PAYLOAD_ON if lamp_object.level != 0 else MQTT_PAYLOAD_OFF,
                retain=False,
            )

        except ValueError as err:
            logger.error(
                "Can't convert <%s> to integer %d..%d: %s",
                lamp_object.level,
                lamp_object.min_level,
                lamp_object.max_level,
                err,
            )
    except KeyError:
        logger.error("Lamp %s doesn't exists", light)


def on_message(mqtt_client, data_object, msg):  # pylint: disable=W0613
    """Default callback on MQTT message."""
    logger.error("Don't publish to %s", msg.topic)


def on_connect(
    client,
    data_object,
    flags,
    result,
    ha_prefix=DEFAULT_HA_DISCOVERY_PREFIX,
):  # pylint: disable=W0613,R0913
    """Callback on connection to MQTT server."""
    mqtt_base_topic = data_object["base_topic"]
    client.subscribe(
        [
            (MQTT_COMMAND_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_BRIGHTNESS_COMMAND_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_BRIGHTNESS_GET_COMMAND_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_COLOR_TEMP_COMMAND_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_RGBW_COMMAND_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_COLOR_MODE_STATE_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_COLOR_TEMP_STATE_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_RGBW_STATE_TOPIC.format(mqtt_base_topic, "+"), 0),
            (MQTT_SCAN_LAMPS_COMMAND_TOPIC.format(mqtt_base_topic), 0),
            (HA_STATUS_TOPIC.format(ha_prefix), 0),
        ]
    )
    client.publish(
        MQTT_DALI2MQTT_STATUS.format(mqtt_base_topic), MQTT_AVAILABLE, retain=True
    )
    initialize_lamps(data_object, client)


def create_mqtt_client(
    driver,
    mqtt_server,
    mqtt_port,
    mqtt_username,
    mqtt_password,
    mqtt_base_topic,
    devices_names_config,
    ha_prefix,
    log_level,
    sync=None,
):
    """Create MQTT client object, setup callbacks and connection to server."""
    logger.debug("Connecting to %s:%s", mqtt_server, mqtt_port)
    mqttc = mqtt.Client(
        client_id="dali2mqtt",
        userdata={
            "driver": driver,
            "base_topic": mqtt_base_topic,
            "ha_prefix": ha_prefix,
            "devices_names_config": devices_names_config,
            "log_level": log_level,
            "all_lamps": {},
            "sync": sync,
        },
    )
    if sync is not None:
        sync.publish = lambda lamp: publish_lamp_state(mqttc, mqtt_base_topic, lamp)
    mqttc.will_set(
        MQTT_DALI2MQTT_STATUS.format(mqtt_base_topic), MQTT_NOT_AVAILABLE, retain=True
    )
    mqttc.on_connect = lambda a, b, c, d: on_connect(a, b, c, d, ha_prefix)

    # Add message callbacks that will only trigger on a specific subscription match.
    mqttc.message_callback_add(
        MQTT_COMMAND_TOPIC.format(mqtt_base_topic, "+"), on_message_cmd
    )
    mqttc.message_callback_add(
        MQTT_BRIGHTNESS_COMMAND_TOPIC.format(mqtt_base_topic, "+"),
        on_message_brightness_cmd,
    )
    mqttc.message_callback_add(
        MQTT_BRIGHTNESS_GET_COMMAND_TOPIC.format(mqtt_base_topic, "+"),
        on_message_brightness_get_cmd,
    )
    mqttc.message_callback_add(
        MQTT_COLOR_TEMP_COMMAND_TOPIC.format(mqtt_base_topic, "+"),
        on_message_color_temp_cmd,
    )
    mqttc.message_callback_add(
        MQTT_RGBW_COMMAND_TOPIC.format(mqtt_base_topic, "+"),
        on_message_rgbw_cmd,
    )
    for topic in (
        MQTT_COLOR_MODE_STATE_TOPIC,
        MQTT_COLOR_TEMP_STATE_TOPIC,
        MQTT_RGBW_STATE_TOPIC,
    ):
        mqttc.message_callback_add(topic.format(mqtt_base_topic, "+"), on_message_colour_state)
    mqttc.message_callback_add(
        MQTT_SCAN_LAMPS_COMMAND_TOPIC.format(mqtt_base_topic),
        on_message_reinitialize_lamps_cmd,
    )

    mqttc.message_callback_add(
        HA_STATUS_TOPIC.format(ha_prefix), on_message_ha_online
    )  # Default callback for unmatched topics
    logging.info("Monitoring HA on %s", HA_STATUS_TOPIC.format(ha_prefix))

    mqttc.on_message = on_message
    if mqtt_username:
        mqttc.username_pw_set(mqtt_username, mqtt_password)
    mqttc.connect(mqtt_server, mqtt_port, 60)
    return mqttc


def main(args):
    """Main loop."""
    mqttc = None
    config = Config(args, lambda: on_detect_changes_in_config(mqttc))

    if config.log_color:
        logging.addLevelName(
            logging.WARNING,
            "{}{}".format(YELLOW_COLOR, logging.getLevelName(logging.WARNING)),
        )
        logging.addLevelName(
            logging.ERROR, "{}{}".format(RED_COLOR, logging.getLevelName(logging.ERROR))
        )

    logger.setLevel(ALL_SUPPORTED_LOG_LEVELS[config.log_level])
    # The bus, level sync and driver modules log under the package name.
    logging.getLogger("dali2mqtt").setLevel(ALL_SUPPORTED_LOG_LEVELS[config.log_level])
    devices_names_config = DevicesNamesConfig(
        config.log_level, config.devices_names_file
    )

    dali_driver = None
    logger.debug("Using <%s> driver", config.dali_driver)

    if config.dali_driver == HASSEB:
        from dali2mqtt.hasseb_sniffer import SniffingHassebDriver

        dali_driver = SniffingHassebDriver()

        firmware_version = read_hasseb_firmware(dali_driver)
        if firmware_version is None or firmware_version < MIN_HASSEB_FIRMWARE_VERSION:
            logger.error("Using dali2mqtt requires newest hasseb firmware")
            logger.error(
                "Please, look at https://github.com/hasseb/python-dali/tree/master/dali/driver/hasseb_firmware"
            )
            quit(1)
    elif config.dali_driver == TRIDONIC:
        from dali.driver.tridonic import SyncTridonicDALIUSBDriver

        dali_driver = SyncTridonicDALIUSBDriver()
    elif config.dali_driver == DALI_SERVER:
        from dali.driver.daliserver import DaliServer

        dali_driver = DaliServer("localhost", 55825)

    bus = Bus(dali_driver)
    sync = LevelSync(bus, poll_interval=config.dali_poll_interval)
    if hasattr(dali_driver, "start"):
        # The hasseb reader thread feeds sniffed frames into the bus.
        dali_driver.on_frame = bus.on_sniffed_frame
        dali_driver.on_frame_error = bus.on_sniffed_error
        dali_driver.start(sniff=config.dali_sniff)
    sync.start()
    logger.info(
        "Level sync: poll every %ss, sniffing %s",
        config.dali_poll_interval or "never",
        "on" if config.dali_sniff and hasattr(dali_driver, "start") else "off",
    )

    retries = 0
    while retries < MAX_RETRIES:
        try:
            mqttc = create_mqtt_client(
                bus,
                *config.mqtt_conf,
                devices_names_config,
                config.ha_discovery_prefix,
                config.log_level,
                sync,
            )
            mqttc.loop_forever()
            retries = (
                0  # if we reach here, it means we where already connected successfully
            )
        except Exception as e:
            logger.error("%s: %s", type(e).__name__, e)
            time.sleep(random.randint(MIN_BACKOFF_TIME, MAX_BACKOFF_TIME))
            retries += 1

    logger.error("Maximum retries of %d reached, exiting...", retries)
    sync.stop()
    if hasattr(dali_driver, "stop"):
        dali_driver.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(argument_default=argparse.SUPPRESS)
    parser.add_argument(
        f"--{CONF_CONFIG}", help="configuration file", default=DEFAULT_CONFIG_FILE
    )
    parser.add_argument(
        f"--{CONF_DEVICES_NAMES_FILE.replace('_', '-')}", help="devices names file"
    )
    parser.add_argument(f"--{CONF_MQTT_SERVER.replace('_', '-')}", help="MQTT server")
    parser.add_argument(
        f"--{CONF_MQTT_PORT.replace('_', '-')}", help="MQTT port", type=int
    )
    parser.add_argument(
        f"--{CONF_MQTT_USERNAME.replace('_', '-')}", help="MQTT username"
    )
    parser.add_argument(
        f"--{CONF_MQTT_PASSWORD.replace('_', '-')}", help="MQTT password"
    )
    parser.add_argument(
        f"--{CONF_MQTT_BASE_TOPIC.replace('_', '-')}", help="MQTT base topic"
    )
    parser.add_argument(
        f"--{CONF_DALI_DRIVER.replace('_', '-')}",
        help="DALI device driver",
        choices=DALI_DRIVERS,
    )
    parser.add_argument(
        f"--{CONF_DALI_LAMPS.replace('_', '-')}",
        help="Number of lamps to scan",
        type=int,
    )
    parser.add_argument(
        f"--{CONF_DALI_POLL_INTERVAL.replace('_', '-')}",
        help="Seconds between polls of the lamps' levels, 0 disables",
        type=int,
    )
    parser.add_argument(
        "--no-dali-sniff",
        dest=CONF_DALI_SNIFF,
        help="Don't listen to other masters on the bus",
        action="store_false",
    )
    parser.add_argument(
        f"--{CONF_HA_DISCOVERY_PREFIX.replace('_', '-')}",
        help="HA discovery mqtt prefix",
    )
    parser.add_argument(
        f"--{CONF_LOG_LEVEL.replace('_', '-')}",
        help="Log level",
        choices=ALL_SUPPORTED_LOG_LEVELS,
    )
    parser.add_argument(
        f"--{CONF_LOG_COLOR.replace('_', '-')}",
        help="Coloring output",
        action="store_true",
    )

    args = parser.parse_args()

    main(args)
