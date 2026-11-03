"""First-time WiFi setup: wlan0, wpa_supplicant.conf and rc.conf."""
import os
from typing import Optional

from . import ui, wifi


def run_setup() -> Optional[str]:
    devices = wifi.wlan_devices()
    if not devices:
        ui.error("No wireless device found (sysctl net.wlan.devices is empty).")
        ui.info("The driver is probably not loaded. Example for /boot/loader.conf:  if_iwn_load=\"YES\"")
        return None

    dev = devices[0] if len(devices) == 1 else ui.pick(devices, "Pick your wireless device", "device> ")
    if not dev:
        return None

    country = input("Regulatory country code, e.g. DE or US (blank to skip): ").strip().upper()

    # wpa_supplicant.conf
    if not os.path.exists(wifi.CONF):
        fd = os.open(wifi.CONF, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(wifi.GLOBAL_OPTS)
        ui.info(f"Created {wifi.CONF}")
    else:
        os.chmod(wifi.CONF, 0o600)
        with open(wifi.CONF) as f:
            has_update = "update_config=1" in f.read()
        if not has_update:
            ui.error(f"{wifi.CONF} has no 'update_config=1'; add it so wifzf can save networks.")

    # rc.conf (persists across reboots)
    wifi.run(["sysrc", f"wlans_{dev}=wlan0"])
    wifi.run(["sysrc", "ifconfig_wlan0=WPA SYNCDHCP"])
    if country:
        wifi.run(["sysrc", f"create_args_wlan0=country {country}"])
    ui.info("Updated /etc/rc.conf")

    # create the interface now so no reboot is needed
    iface = wifi.find_interface()
    if iface is None:
        cmd = ["ifconfig", "wlan0", "create", "wlandev", dev]
        if country:
            cmd += ["country", country]
        rc, out = wifi.run(cmd + ["up"])
        if rc != 0:
            ui.error(f"Could not create wlan0: {out}")
            return None
        iface = "wlan0"

    if not wifi.supplicant_running(iface) and not wifi.start_supplicant(iface):
        ui.error("wpa_supplicant did not start.")
        return None
    ui.info(f"{iface} is ready.")
    return iface
