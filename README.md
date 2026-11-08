![pytest workflow](https://github.com/dgomes/dali2mqtt/actions/workflows/pytest.yml/badge.svg)

# dali2mqtt
DALI <-> MQTT bridge

## About

This daemon is inspired in [zigbee2mqtt](https://github.com/Koenkk/zigbee2mqtt) and provides the means to integrate a DALI light controller into your Home Assistant setup.

Previously I developed a Home Assistant custom component (https://github.com/dgomes/home-assistant-custom-components/tree/master/light) but I've since decided to run Home Assistant in another device, away from the physical DALI Bus.

## Supported Devices

This daemon relies in [python-dali](https://github.com/sde1000/python-dali) so all devices supported by this library should also be supported by dali2mqtt.

## How to use

#### Install Operating System Dependencies

### Python3 library
This package depends on https://github.com/trezor/cython-hidapi

### Create a Virtual Environment (recommended) and install the requirements
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Create a configuration file
You can create a configuration file when you call the daemon the first time

```bash
venv/bin/python3 -m dali2mqtt.dali2mqtt
```

Then just edit the file accordingly. You can also create the file with the right values, by using the arguments of dali_mqtt_daemon.py:

```
  --config CONFIG       configuration file
  --mqtt-server MQTT_SERVER
                        MQTT server
  --mqtt-port MQTT_PORT
                        MQTT port
  --mqtt-username MQTT_USERNAME
                        MQTT user name
  --mqtt-password MQTT_PASSWORD
                        MQTT password
  --mqtt-base-topic MQTT_BASE_TOPIC
                        MQTT base topic
  --dali-driver {hasseb,tridonic,dali_server}
                        DALI device driver
  --dali-lamps DALI_LAMPS
                        Number of lamps to scan
  --dali-poll-interval DALI_POLL_INTERVAL
                        Seconds between polls of the lamps' levels, 0 disables
  --no-dali-sniff       Don't listen to other masters on the bus
  --ha-discover-prefix HA_DISCOVER_PREFIX
                        HA discover mqtt prefix
  --log-level {critical,error,warning,info,debug}  
                        Log level  
  --log-color Coloring output
```

### Devices friendly names
Default all lamps will be displayed in Home Assistant by short address, numbers from 0 to 63
You can give lamps special names to help you identify lamps by name. On the first execution, `devices.yaml` file will be create with all lamps available.
Example `devices.yaml`:
```yaml
0: 
  "friendly_name": "Lamp in kitchen"
8:
  "friendly_name": "Lamp in bathroom"
```
Please note that MQTT topics support a minimum set of characters, therefore friendly names are converted to slug strings, so a lamp with address 0 (as an example) in MQTT will be named "lamp-in-kitchen"

### Per-lamp settings
Next to `friendly_name`, `devices.yaml` can hold settings that change how a lamp is driven and can't be read from the bus. Everything is optional; invalid or misspelt settings are logged and ignored for that lamp.

```yaml
3:
  friendly_name: office-rgb
  colour:
    white_points:                  # RGBW (0..255) that render each colour temperature
      2700: [0, 0, 0, 255]
      6500: [240, 210, 255, 200]
    colour_temp: mixed             # mixed, native or off (default: automatic)
    colour_temp_range: [2700, 6500]
```

- `white_points`: calibration points for the colour temperature mixed on RGBW gear: for each colour temperature in kelvin, the red, green, blue and white values (Home Assistant's 0..255) that render it on this strip. At least two; in between they are interpolated, and without `colour_temp_range` the slider covers the points' span. Without them a textbook mix is used, which on real strips usually looks tinted.
- `colour_temp`: `mixed` mixes colour temperature from the RGBW channels (the default for RGBW gear), `native` uses the gear's own (for instance a MiBoxer DL-X in RGB+CCT mode, with separate cold and warm white strips), `off` hides the colour temperature control.
- `colour_temp_range`: warmest and coolest kelvin offered; for native colour temperature it narrows the range the gear reports.

Groups use the colour settings of their members when all members agree. Each light entity also gets attributes with its DALI address, device types, colour features and these settings.

### Colour (DT8 colour control gear)
Lamps that implement DALI colour control (IEC 62386-209, device type 8) are detected at start-up. What Home Assistant gets depends on what the gear reports:

- colour temperature capable gear gets a colour temperature slider, limited to the range the lamp renders. Values are in mireds (DALI mirek);
- gear with red, green, blue and white channels gets an RGBW colour picker; brightness dims the colour as a whole;
- on RGBW gear the colour temperature is mixed from the red, green, blue and white channels (calibrate it with `white_points`, see above): RGBW gear tested so far claims its own colour temperature support but renders every value as the same white. Gear with colour temperature and no RGBW channels uses its own;
- the active colour mode is published.

```
dali2mqtt/<lamp>/light/color_temp/set      command, mireds
dali2mqtt/<lamp>/light/color_temp/status   state, mireds
dali2mqtt/<lamp>/light/rgbw/set            command, "r,g,b,w" 0..255 each
dali2mqtt/<lamp>/light/rgbw/status         state, "r,g,b,w"
dali2mqtt/<lamp>/light/color_mode/status   state, color_temp or rgbw
```

#### Calibrating the mixed colour temperature
The mixed colour temperature needs `white_points` for each strip type, found by eye. A camera won't do: a strip makes white from narrow red, green and blue LEDs, and cameras see such light differently from the eye, so a mix that looks neutral on camera can look clearly coloured in the room.

1. Light a sheet of white paper with a reference of the colour temperature to match: a lamp you trust at 2700 K, or an adjustable studio light at 6500 K. Switch other lights off; warm room light changes how a cool white looks.
2. Put two candidate mixes on two RGBW lamps next to the paper with `dali2mqtt/<lamp>/light/rgbw/set`, say which is closer, keep that one and change the other a step, one channel at a time, until neither step improves it.
3. Write the winner under that colour temperature.

The Arlight RT-B60-10mm 24V RGBW-Warm strip on MiBoxer DL-X drivers matched 2700 K with its white LED alone and 6500 K with `[240, 210, 255, 200]`.

Colour changes made by other masters on the bus are picked up like level changes: sniffed on hasseb, and on every driver by the periodic poll. Groups get colour when every member supports it, over the colour temperature range they all share. xy and primary N colour control are not exposed.

### Setup systemd
edit dali2mqtt.service and change the path of python3 to the path of your venv, after:

```bash
sudo cp dali2mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dali2mqtt.service 
```

### Give your user permissions to access the USB device
```bash
sudo adduser homeassistant plugdev 
cp 50-hasseb.rules /etc/udev/rules.d/
```
You might need to reboot your device after the last change.

In this example the user is **homeassistant**

### Check everything is OK
```bash
sudo systemctl start dali2mqtt.service 
sudo systemctl status dali2mqtt.service 
```

### Command line arguments and configuration file

When the daemon first runs, it creates a default `config.yaml` file.
You can edit the file to customize your setup.

### Keeping light levels in sync

Lamps are also controlled by other things on the bus: wall switches, sensors,
other controllers. dali2mqtt keeps the state it reports to Home Assistant in
sync with the real levels in two ways, both feeding one model:

* **Bus sniffing** (hasseb only, `dali_sniff: true` by default). The hasseb
  USB master is put in sniffing mode and every command another master sends
  is decoded. Direct level commands update the state immediately; for anything
  whose outcome depends on the lamp (scenes, up/down steps, fades) the lamp is
  queried once its fade time is over.
* **Polling** (every driver, `dali_poll_interval`, default 10 seconds, 0
  disables). Every lamp's actual level is queried in turn, catching whatever
  the bus never showed: missed frames, power cycles, time spent offline. With
  sniffing on, a longer interval such as 60 seconds is enough.

Groups are never queried. A group is on when any member is on, and its
brightness is the mean of the members that are on.

Only one command is on the bus at a time, so commands from Home Assistant wait
at most for a single query to finish.
