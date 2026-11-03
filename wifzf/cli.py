"""wifzf: fzf-driven WiFi manager for FreeBSD."""
import getpass
import os
import shlex
import shutil
import sys
from typing import List

from . import setup, ui, wifi

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    for tool in ("doas", "sudo"):
        path = shutil.which(tool)
        if path:
            os.execv(path, [path, "env", f"PYTHONPATH={PKG_PARENT}", sys.executable, "-m", "wifzf", *sys.argv[1:]])
    sys.exit("wifzf needs root. Install doas or sudo, or run it as root.")


def header(iface: str) -> str:
    st = wifi.status(iface)
    if st.get("wpa_state") == "COMPLETED":
        return f"{iface}: connected to {st.get('ssid', '?')} ({wifi.ip_address(iface) or 'no IP'})"
    return f"{iface}: not connected ({st.get('wpa_state', 'unknown').lower()})"


def activate(iface: str, nid: int, name: str, new: bool = False) -> None:
    prev = wifi.current_id(iface)
    ui.info(f"Connecting to {name}...")
    if wifi.select(iface, nid) and wifi.wait_connected(iface, nid):
        ok = wifi.ensure_ip(iface)
        ui.info(f"Connected to {name}, IP: {wifi.ip_address(iface)}" if ok else "Associated, but no IP address (DHCP failed).")
        return ui.pause()

    ui.error("Could not connect (wrong password or network not reachable).")
    if new:
        wifi.forget(iface, nid)
        ui.info("Removed the new network again.")
    ui.info("Resetting the WiFi interface and restoring the previous connection...")
    if wifi.restore(iface, prev):
        ui.info("Back online.")
    else:
        ui.info("Not connected. Pick a network or use 'Reconnect (auto)'.")
    ui.pause()


def network_lines(iface: str) -> List[str]:
    """One fzf line per network: 'label<TAB>ssid-hex<TAB>security'.
    fzf only shows the label (field 1); the rest is for us."""
    saved = {s.ssid for s in wifi.saved_networks(iface)}
    lines = []
    for ap in wifi.scan(iface):
        tag = "saved" if ap.ssid in saved else ""
        label = f"{ap.name:<32.32} {ap.signal:>4} dBm  {ap.band:<5} {ap.security:<6} {tag}"
        lines.append(f"{label}\t{ap.ssid.hex()}\t{ap.security}")
    return lines


def action_connect(iface: str) -> None:
    ui.info("Scanning...")
    lines = network_lines(iface)
    if not lines:
        ui.error("No networks found.")
        return ui.pause()

    # ctrl-r re-runs this program in "print the list" mode and reloads fzf with it
    reload_cmd = f"env PYTHONPATH={shlex.quote(PKG_PARENT)} {shlex.quote(sys.executable)} -m wifzf --scan-lines {iface} 2>>/tmp/wifzf.log"
    extra = "--delimiter='\\t' --with-nth=1 --bind=" + shlex.quote(f"ctrl-r:reload({reload_cmd})")
    choice = ui.pick(lines, "ctrl-r: rescan (takes ~3s) | esc: back", "network> ", extra)
    if not choice:
        return

    _, ssid_hex, security = choice.split("\t")
    ssid = bytes.fromhex(ssid_hex)
    name = ssid.decode("utf-8", errors="replace")

    saved = {s.ssid: s for s in wifi.saved_networks(iface)}
    if ssid in saved:
        return activate(iface, saved[ssid].id, name)
    if security in ("802.1X", "WEP"):
        ui.error(f"{security} networks are not supported.")
        return ui.pause()
    password = None
    if security != "open":
        password = getpass.getpass(f"Password for {name}: ")
        if not 8 <= len(password) <= 63:
            ui.error("WPA passphrases must be 8-63 characters.")
            return ui.pause()
    try:
        nid = wifi.add_network(iface, ssid, password, security)
    except wifi.WifiError as e:
        ui.error(str(e))
        return ui.pause()
    activate(iface, nid, name, new=True)


def pick_saved(iface: str, title: str, skip_current: bool = False):
    nets = [n for n in wifi.saved_networks(iface) if not (skip_current and n.current)]
    if not nets:
        ui.error("No saved networks.")
        ui.pause()
        return None
    lines = {f"{n.name:<32.32} {'(connected)' if n.current else ''}": n for n in nets}
    choice = ui.pick(list(lines), title, "saved> ")
    return lines[choice] if choice else None


def action_switch(iface: str) -> None:
    net = pick_saved(iface, "Switch to a saved network", skip_current=True)
    if not net:
        return
    ui.info("Checking that the network is in range...")
    if not wifi.in_range(iface, net.ssid):
        ui.error(f"'{net.name}' is not in range. Nothing changed.")
        return ui.pause()
    activate(iface, net.id, net.name)


def action_forget(iface: str) -> None:
    net = pick_saved(iface, "Forget a saved network")
    if net and ui.confirm(f"Forget '{net.name}'?"):
        wifi.forget(iface, net.id)
        ui.info("Forgotten.")
        ui.pause()


def action_disconnect(iface: str) -> None:
    wifi.disconnect(iface)
    ui.info("Disconnected. Use 'Reconnect (auto)' or pick a network to connect again.")
    ui.pause()


def action_reconnect(iface: str) -> None:
    wifi.reconnect_auto(iface)
    wifi.wait_connected(iface)
    ui.info(header(iface))
    ui.pause()


def action_status(iface: str) -> None:
    print(wifi.cli(iface, "status"))
    print(f"ip_address={wifi.ip_address(iface)}")
    ui.pause()


def main() -> int:
    ensure_root()

    if len(sys.argv) >= 3 and sys.argv[1] == "--scan-lines":  # used by fzf's ctrl-r
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # pipes may default to ASCII
            print("\n".join(network_lines(sys.argv[2])))
        except Exception:
            import traceback

            with open("/tmp/wifzf.log", "a") as f:
                traceback.print_exc(file=f)
            return 1
        return 0

    os.environ["SHELL"] = "/bin/sh"  # fzf runs reload/execute via $SHELL; csh/fish choke on our commands

    ui.require_fzf()
    iface = wifi.find_interface()
    if iface is None or not os.path.exists(wifi.CONF):
        ui.info("WiFi is not set up yet. Let's do that first.")
        iface = setup.run_setup()
        if iface is None:
            return 1
    elif not wifi.supplicant_running(iface) and not wifi.start_supplicant(iface):
        ui.error("Could not start wpa_supplicant.")
        return 1

    actions = {
        "Connect to a network (scan)": action_connect,
        "Switch to a saved network": action_switch,
        "Disconnect": action_disconnect,
        "Reconnect (auto, by priority)": action_reconnect,
        "Forget a saved network": action_forget,
        "Status": action_status,
    }
    try:
        while True:
            os.system("clear")
            choice = ui.pick(list(actions) + ["Setup / reconfigure", "Quit"], header(iface), "wifzf> ")
            if choice in (None, "Quit"):
                return 0
            if choice == "Setup / reconfigure":
                iface = setup.run_setup() or iface
                ui.pause()
            else:
                actions[choice](iface)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
