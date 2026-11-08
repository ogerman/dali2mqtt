Unreleased
  - Keep light levels in sync with the bus: sniff other masters' commands
    on hasseb and poll actual levels on every driver (dali_poll_interval,
    dali_sniff); group state derived from members
  - Serialize all access to the DALI driver
  - Colour control for DT8 colour gear: colour temperature (Home Assistant
    color_temp in mireds, limited to the lamp's own range) and RGBW; the
    active colour mode is published and colour changes by other masters are
    picked up by sniffing and polling; groups get colour when every member
    supports it

0.3 - October 2025
  - Update to HA 2025.10 discovery

0.2 - August 2020
  - Updated python-dali to 0.7
  - Requires new Firmware on HASSEB DALI Controller

0.1 - Initial Release (2018)
  - Home Assistant discovery
