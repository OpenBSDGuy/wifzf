"""Thin wrappers around ifconfig on OpenBSD (no wpa_supplicant: the join list lives in /etc/hostname.<if>)."""
import os
import re
import shlex
import stat
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .wifi import WifiError, ip_address, run  # noqa: F401  (re-exported for cli.py)

CONF = "/etc/hostname.{}"  # format with the interface name
SIGNAL_UNIT = "%"  # OpenBSD reports signal strength as a percentage


def cli(iface: str, *args: str) -> str:
    """Only used by the Status menu entry: print the whole interface state."""
    return run(["ifconfig", iface])[1]


def decode_ssid(tok: str) -> bytes:
    """ifconfig and hostname.if(5) write non-printable SSIDs as 0x<hex>."""
    # shortcut: a real SSID literally spelled 0x<hex> is read as hex, fine until someone has one
    if re.fullmatch(r"0x([0-9a-fA-F]{2})+", tok):
        return bytes.fromhex(tok[2:])
    return tok.encode()


def encode_id(ssid: bytes) -> str:
    """Plain when it is inert to the shell, else 0x hex (always safe)."""
    s = ssid.decode("ascii", errors="replace")
    if re.fullmatch(r"[A-Za-z0-9._@%+=:,/-]+", s) and not s.startswith(("0x", "-")):
        return s
    return "0x" + ssid.hex()


# netstart eval()s every hostname.if line as root, so nothing here may be shell-active
# even inside double quotes ($ ` \ ") or cut by the comment stripper (#).
UNSAFE_IN_QUOTES = re.compile(r'["$`\\#]')


def join_line(ssid: bytes, password: Optional[str]) -> str:
    """The hostname.if line for a network; raises WifiError for unsafe passphrases."""
    nid, s = encode_id(ssid), ssid.decode("ascii", errors="replace")
    if nid.startswith("0x") and re.fullmatch(r"[\x20-\x7e]+", s) and not UNSAFE_IN_QUOTES.search(s) and not s.startswith(("0x", "-")):
        nid = f'"{s}"'  # readable form for names with spaces & co (hostname.if(5) quoting)
    if password is None:
        return f"join {nid}"
    if UNSAFE_IN_QUOTES.search(password):
        # shortcut: could write the derived PSK as 0x hex instead if someone needs these characters
        raise WifiError('passphrases containing " $ ` \\ or # are not supported on OpenBSD')
    return f'join {nid} wpakey "{password}"'


# --- interface --------------------------------------------------------------

def wlan_devices() -> List[str]:
    rc, out = run(["ifconfig", "wlan"])  # "wlan" is the interface group
    return re.findall(r"^(\w+): flags=", out, re.M) if rc == 0 else []


def find_interface() -> Optional[str]:
    devs = wlan_devices()
    return devs[0] if devs else None


def supplicant_running(iface: str) -> bool:
    """No supplicant here: 'running' means the interface is up."""
    m = re.search(r"flags=\w+<([^>]*)>", run(["ifconfig", iface])[1])
    return bool(m) and "UP" in m.group(1).split(",")


def start_supplicant(iface: str) -> bool:
    run(["ifconfig", iface, "up"])
    return supplicant_running(iface)


# --- scanning ---------------------------------------------------------------

@dataclass
class AP:
    ssid: bytes
    signal: int
    chan: int
    flags: str

    @property
    def name(self) -> str:
        return self.ssid.decode("utf-8", errors="replace")

    @property
    def band(self) -> str:
        return "5GHz" if self.chan > 14 else "2.4G"

    @property
    def security(self) -> str:
        f = set(self.flags.split(","))
        if "wpa3" in f and "wpa2" not in f:
            return "WPA3"
        if "wpa2" in f:
            return "WPA2"
        if "wpa1" in f:
            return "WPA"
        if "privacy" in f:
            return "WEP"
        return "open"


SCAN_RE = re.compile(
    r'^\s*nwid (?P<ssid>"[^"]*"|\S+) chan (?P<chan>\d+) bssid (?:[0-9a-f]{2}:){5}[0-9a-f]{2} (?P<sig>\d+)%(?P<rest>.*)$'
)


def parse_scan(text: str) -> List[AP]:
    best: Dict[bytes, AP] = {}
    for line in text.splitlines():
        m = SCAN_RE.match(line)
        if not m:
            continue
        ssid = decode_ssid(m["ssid"].strip('"'))
        if not ssid:  # hidden
            continue
        rest = m["rest"].split()
        ap = AP(ssid, int(m["sig"]), int(m["chan"]), rest[-1] if rest else "")
        if ssid not in best or ap.signal > best[ssid].signal:
            best[ssid] = ap
    return sorted(best.values(), key=lambda a: a.signal, reverse=True)


def scan(iface: str, wait: float = 3.0) -> List[AP]:
    if not supplicant_running(iface):  # down after Disconnect: scan succeeds but lists nothing
        start_supplicant(iface)
    # as root each call restarts the scan and prints only what arrived so far: merge reads over `wait`
    out, end = "", time.time() + wait
    while time.time() < end:
        out += run(["ifconfig", iface, "scan"])[1] + "\n"
        time.sleep(0.5)
    return parse_scan(out)


# --- saved networks (join lines in /etc/hostname.<if>) ----------------------

@dataclass
class Saved:
    ssid: bytes
    key: Optional[str]
    current: bool = False

    @property
    def id(self) -> bytes:  # the SSID is the identity of a join entry
        return self.ssid

    @property
    def name(self) -> str:
        return self.ssid.decode("utf-8", errors="replace")


def parse_join(line: str) -> Optional[Saved]:
    try:
        tok = shlex.split(line, comments=True)
    except ValueError:
        return None
    if len(tok) < 2 or tok[0] != "join":
        return None
    rest = tok[2:]
    key = rest[rest.index("wpakey") + 1] if "wpakey" in rest[:-1] else None
    return Saved(decode_ssid(tok[1]), key)


def parse_hostname(text: str) -> List[Saved]:
    return [s for s in map(parse_join, text.splitlines()) if s]


def add_join_line(text: str, line: str) -> str:
    """Insert `line` after the last join line (or at the top); other lines stay byte-identical."""
    lines = text.splitlines(keepends=True)
    at = max((i + 1 for i, l in enumerate(lines) if parse_join(l)), default=0)
    if at and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, line + "\n")
    return "".join(lines)


def remove_join_lines(text: str, ssid: bytes) -> str:
    keep = [l for l in text.splitlines(keepends=True) if not ((s := parse_join(l)) and s.ssid == ssid)]
    return "".join(keep)


def read_conf(iface: str) -> str:
    try:
        with open(CONF.format(iface), encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_conf(iface: str, edit: Callable[[str], str]) -> None:
    """Replace the file atomically, keeping mode and owner."""
    path = CONF.format(iface)
    new = edit(read_conf(iface))
    try:
        try:
            st = os.stat(path)
            mode, uid, gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
        except FileNotFoundError:
            mode, uid, gid = 0o640, 0, 0
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.write(new)
                os.fchmod(f.fileno(), mode)
                os.fchown(f.fileno(), uid, gid)
                f.flush()
                os.fsync(f.fileno())  # this file holds every saved network; don't rename an empty one in
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as e:
        raise WifiError(f"could not update {path}: {e}") from e


def status(iface: str) -> Dict[str, str]:
    return parse_ifconfig(run(["ifconfig", iface])[1])


def parse_ifconfig(text: str) -> Dict[str, str]:
    """Same keys cli.py reads from wpa_cli: wpa_state ("COMPLETED" when associated) and ssid (+ ssid_hex)."""
    st = {"wpa_state": "INACTIVE"}
    m = re.search(r"flags=\w+<([^>]*)>", text)
    if m and "UP" in m.group(1).split(","):
        st["wpa_state"] = "DISCONNECTED"
    if re.search(r"^\s*status: active", text, re.M):
        st["wpa_state"] = "COMPLETED"
        m = re.search(r'^\s*ieee80211: (?:join|nwid) ("[^"]*"|\S+) chan', text, re.M)
        if m:
            raw = decode_ssid(m[1].strip('"'))
            st["ssid"] = raw.decode("utf-8", errors="replace")
            st["ssid_hex"] = raw.hex()
    return st


def saved_networks(iface: str) -> List[Saved]:
    nets = parse_hostname(read_conf(iface))
    cur = current_id(iface)
    for n in nets:
        n.current = n.ssid == cur
    return nets


def add_network(iface: str, ssid: bytes, password: Optional[str], security: str) -> bytes:
    line = join_line(ssid, password)  # validates before anything is changed
    cmd = ["ifconfig", iface, "join", encode_id(ssid)]
    if password is not None:
        cmd += ["wpakey", password]
    rc, out = run(cmd)
    if rc != 0:
        raise WifiError(f"ifconfig rejected the network: {out}")
    write_conf(iface, lambda t: add_join_line(t, line))
    return ssid


def forget(iface: str, nid: bytes) -> None:
    run(["ifconfig", iface, "-join", encode_id(nid)])
    write_conf(iface, lambda t: remove_join_lines(t, nid))


# --- connection control -----------------------------------------------------

def select(iface: str, nid: bytes) -> bool:
    """Connect to one saved network via an nwid override (the join list is ignored until -nwid)."""
    net = next((n for n in saved_networks(iface) if n.ssid == nid), None)
    if net is None:
        return False
    key = ["wpakey", net.key] if net.key is not None else ["-wpakey"]
    return run(["ifconfig", iface, "nwid", encode_id(nid), *key])[0] == 0


def disconnect(iface: str) -> None:
    run(["ifconfig", iface, "down"])


def reconnect_auto(iface: str) -> None:
    """Drop any nwid override so the join list decides again."""
    run(["ifconfig", iface, "-nwid"])
    run(["ifconfig", iface, "up"])


def wait_connected(iface: str, nid: Optional[bytes] = None, timeout: int = 30) -> bool:
    """Wait until associated (to `nid`). No wrong-password signal exists, so a bad key just times out."""
    end = time.time() + timeout
    while time.time() < end:
        if current_id(iface) is not None and (nid is None or current_id(iface) == nid):
            return True
        time.sleep(1)
    return False


def in_range(iface: str, ssid: bytes) -> bool:
    return any(ap.ssid == ssid for ap in scan(iface))


def current_id(iface: str) -> Optional[bytes]:
    st = status(iface)
    if st["wpa_state"] == "COMPLETED" and "ssid_hex" in st:
        return bytes.fromhex(st["ssid_hex"])
    return None


def restore(iface: str, prev: Optional[bytes]) -> bool:
    """Recover after a failed connect: reset, let the join list reconnect, else re-select prev."""
    run(["ifconfig", iface, "down"])
    time.sleep(1)
    run(["ifconfig", iface, "up"])
    run(["ifconfig", iface, "-nwid"])
    if wait_connected(iface, prev, timeout=15):
        return True
    if prev is not None and select(iface, prev):  # prev was an nwid override, not on the join list
        return wait_connected(iface, prev)
    return False


def ensure_ip(iface: str, grace: int = 15) -> bool:
    """dhcpleased (inet autoconf) does DHCP; just wait for the address."""
    for _ in range(grace * 2):
        # right after a switch the old address lingers while dhcpleased is still "Rebooting"
        if "[Bound]" in run(["dhcpleasectl", "-l", iface])[1] and ip_address(iface):
            return True
        time.sleep(0.5)
    return False
