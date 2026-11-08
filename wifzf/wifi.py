"""Thin wrappers around ifconfig / wpa_cli / wpa_supplicant on FreeBSD."""
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

CONF = "/etc/wpa_supplicant.conf"
SIGNAL_UNIT = "dBm"
GLOBAL_OPTS = """ctrl_interface=/var/run/wpa_supplicant
update_config=1
eapol_version=2
ap_scan=1
fast_reauth=1
"""


class WifiError(Exception):
    pass


def run(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    out = p.stdout if p.returncode == 0 else (p.stderr or p.stdout)
    return p.returncode, out.strip()


def cli(iface: str, *args: str) -> str:
    return run(["wpa_cli", "-i", iface, *args])[1]


def ssid_bytes(s: str) -> bytes:
    """Decode wpa_cli's escaped SSID (\\xNN, \\\\, \\") into raw bytes."""
    out, i = bytearray(), 0
    while i < len(s):
        if s[i] == "\\" and s[i + 1 : i + 2] == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", s[i + 2 : i + 4]):
            out.append(int(s[i + 2 : i + 4], 16))
            i += 4
        elif s[i] == "\\" and s[i + 1 : i + 2] in ("\\", '"'):
            out.append(ord(s[i + 1]))
            i += 2
        else:
            out.extend(s[i].encode())
            i += 1
    return bytes(out)


# --- interface / supplicant -------------------------------------------------

def wlan_devices() -> List[str]:
    rc, out = run(["sysctl", "-n", "net.wlan.devices"])
    return out.split() if rc == 0 else []


def find_interface() -> Optional[str]:
    rc, out = run(["ifconfig", "-l"])
    for name in out.split():
        if name.startswith("wlan"):
            return name
    return None


def supplicant_running(iface: str) -> bool:
    return "PONG" in cli(iface, "ping")


def start_supplicant(iface: str) -> bool:
    run(["ifconfig", iface, "up"])
    run(["wpa_supplicant", "-B", "-i", iface, "-c", CONF])
    for _ in range(10):
        if supplicant_running(iface):
            return True
        time.sleep(0.3)
    return False


# --- scanning ---------------------------------------------------------------

@dataclass
class AP:
    raw: str  # as printed by wpa_cli
    signal: int
    freq: int
    flags: str

    @property
    def ssid(self) -> bytes:
        return ssid_bytes(self.raw)

    @property
    def name(self) -> str:
        return self.ssid.decode("utf-8", errors="replace")

    @property
    def band(self) -> str:
        return "5GHz" if self.freq >= 4900 else "2.4G"

    @property
    def security(self) -> str:
        f = self.flags
        if "EAP" in f:
            return "802.1X"
        if "WEP" in f:
            return "WEP"
        if "SAE" in f and "PSK" not in f:
            return "WPA3"
        if "WPA2" in f or "RSN" in f:
            return "WPA2"
        if "WPA" in f:
            return "WPA"
        return "open"


def scan(iface: str, wait: float = 3.0) -> List[AP]:
    cli(iface, "scan")
    time.sleep(wait)
    best: Dict[bytes, AP] = {}
    for line in cli(iface, "scan_results").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 5 or not parts[4].strip() or parts[4].startswith("\\x00"):
            continue
        try:
            ap = AP(parts[4], int(parts[2]), int(parts[1]), parts[3])
        except ValueError:
            continue
        if ap.ssid not in best or ap.signal > best[ap.ssid].signal:
            best[ap.ssid] = ap
    return sorted(best.values(), key=lambda a: a.signal, reverse=True)


# --- saved networks ---------------------------------------------------------

@dataclass
class Saved:
    id: int
    raw: str
    flags: str

    @property
    def ssid(self) -> bytes:
        return ssid_bytes(self.raw)

    @property
    def name(self) -> str:
        return self.ssid.decode("utf-8", errors="replace")

    @property
    def current(self) -> bool:
        return "[CURRENT]" in self.flags


def saved_networks(iface: str) -> List[Saved]:
    nets = []
    for line in cli(iface, "list_networks").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit():
            nets.append(Saved(int(parts[0]), parts[1], parts[3] if len(parts) > 3 else ""))
    return nets


def add_network(iface: str, ssid: bytes, password: Optional[str], security: str) -> int:
    lines = cli(iface, "add_network").splitlines()
    nid = lines[-1].strip() if lines else ""
    if not nid.isdigit():
        raise WifiError("could not add network (is wpa_supplicant running?)")

    def setn(key: str, value: str) -> None:
        if "FAIL" in cli(iface, "set_network", nid, key, value):
            cli(iface, "remove_network", nid)
            raise WifiError(f"wpa_supplicant rejected '{key}'")

    setn("ssid", ssid.hex())
    if password is None:
        setn("key_mgmt", "NONE")
    else:
        setn("psk", f'"{password}"')
        if security == "WPA3":
            setn("key_mgmt", "SAE")
            setn("ieee80211w", "2")
    cli(iface, "save_config")
    return int(nid)


def forget(iface: str, nid: int) -> None:
    cli(iface, "remove_network", str(nid))
    cli(iface, "save_config")


# --- connection control -----------------------------------------------------

def select(iface: str, nid: int) -> bool:
    """Connect to one saved network (this disables the others)."""
    return "OK" in cli(iface, "select_network", str(nid))


def disconnect(iface: str) -> None:
    cli(iface, "disconnect")


def reconnect_auto(iface: str) -> None:
    """Re-enable all saved networks and let priorities decide."""
    cli(iface, "enable_network", "all")
    cli(iface, "reconnect")


def status(iface: str) -> Dict[str, str]:
    st = {}
    for line in cli(iface, "status").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            st[k] = v
    return st


def ip_address(iface: str) -> Optional[str]:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", run(["ifconfig", iface])[1])
    return m.group(1) if m else None


def wait_connected(iface: str, nid: Optional[int] = None, timeout: int = 15) -> bool:
    """Wait until connected to network `nid`. Gives up early if wpa_supplicant
    temp-disables the network (it does that after repeated auth failures,
    i.e. a wrong password)."""
    end = time.time() + timeout
    while time.time() < end:
        st = status(iface)
        if st.get("wpa_state") == "COMPLETED" and (nid is None or st.get("id") == str(nid)):
            return True
        if nid is not None and any(
            n.id == nid and "TEMP-DISABLED" in n.flags for n in saved_networks(iface)
        ):
            return False
        time.sleep(1)
    return False


def in_range(iface: str, ssid: bytes) -> bool:
    return any(ap.ssid == ssid for ap in scan(iface, wait=2.5))


def current_id(iface: str) -> Optional[int]:
    st = status(iface)
    if st.get("wpa_state") == "COMPLETED" and st.get("id", "").isdigit():
        return int(st["id"])
    return None


def reset_interface(iface: str) -> None:
    """Same as `ifconfig down && ifconfig up`: clears a stuck net80211 state."""
    run(["ifconfig", iface, "down"])
    time.sleep(1)
    run(["ifconfig", iface, "up"])
    time.sleep(1)
    if not supplicant_running(iface):
        start_supplicant(iface)


def restore(iface: str, prev: Optional[int]) -> bool:
    """Recover after a failed connect: reset the interface, go back to the
    previous network (if any) and re-enable all saved networks as fallback."""
    reset_interface(iface)
    if prev is not None:
        select(iface, prev)
    cli(iface, "enable_network", "all")
    cli(iface, "reconnect")
    return wait_connected(iface, prev, timeout=15)


def ensure_ip(iface: str, grace: int = 8) -> bool:
    for _ in range(grace * 2):
        if ip_address(iface):
            return True
        time.sleep(0.5)
    try:
        subprocess.run(["dhclient", iface], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ip_address(iface) is not None
