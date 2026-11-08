# wifzf

A minimal WiFi manager for FreeBSD and OpenBSD built on top of fzf (and wpa_supplicant on FreeBSD). Think `nmtui`, but only for WiFi and made for switching networks fast.

## Requirements

- FreeBSD or OpenBSD
- Python 3.10+
- [fzf](https://github.com/junegunn/fzf)
- `doas` or `sudo`

## Installation

### Quick install (recommended)

```
$ pkg install fzf py311-pipx doas
$ pipx install wifzf
```

On OpenBSD:

```
$ pkg_add fzf py3-pipx
$ pipx install wifzf
```

Or with pip:

```
$ pip install wifzf
```

Then run:

```
$ wifzf
```

### From source

```
$ git clone https://github.com/kasramp/wifzf
$ cd wifzf
$ python -m venv venv && source venv/bin/activate
$ pip install -r requirements.txt
$ ./run-wifzf
```

## First run

On OpenBSD, see [OpenBSD notes](#openbsd-notes) below; setup there picks the device, creates `/etc/hostname.<if>` (mode 640) with `inet autoconf` if missing and brings the interface up.

On FreeBSD:

If there is no `wlan0` or no `/etc/wpa_supplicant.conf`, wifzf sets things up for you: it asks for your wireless device and country code, writes `wpa_supplicant.conf` (mode 600), updates `/etc/rc.conf` through `sysrc`, creates `wlan0` and starts `wpa_supplicant`. No reboot needed.

wifzf re-runs itself through `doas` or `sudo` when it isn't started as root.

## Usage

| Menu entry                    | Action                                              |
|-------------------------------|-----------------------------------------------------|
| Connect to a network (scan)   | Scan, pick a network, enter the password, connect. Ctrl-R rescans |
| Switch to a saved network     | Quickly jump to a network you already saved         |
| Disconnect                    | Drop the current connection                         |
| Reconnect (auto, by priority) | Re-enable all saved networks and let priority decide |
| Forget a saved network        | Remove a network from `wpa_supplicant.conf`         |
| Status                        | Show `wpa_cli status` and the IP address            |
| Setup / reconfigure           | Run the first-time setup again                      |
| Esc                           | Go back                                             |

## Notes

- Networks are stored in `/etc/wpa_supplicant.conf` via `wpa_cli save_config`.
- "Switch" uses `select_network`, which disables the other saved networks until you pick "Reconnect (auto)".
- If a connect fails (wrong password, out of range) wifzf resets the interface (`ifconfig down/up`) and goes back to your previous network.
- Supports open, WPA/WPA2 and WPA3 personal networks. No WEP, no 802.1X, no hidden networks.

## OpenBSD notes

- Saved networks are the `join` lines of `/etc/hostname.<if>`; wifzf edits only those lines.
- "Switch" uses an `nwid` override until you pick "Reconnect (auto)", which runs `ifconfig <if> -nwid` so the join list decides again.
- No WPA3-only (SAE), WEP or 802.1X networks. Networks offering both WPA3 and WPA2 are treated as WPA2. Passphrases containing `"`, `$`, `` ` ``, `\` or `#` are refused (they would be shell-active in `/etc/netstart`).
- Signal strength is shown in %, not dBm.
- OpenBSD gives no wrong-password signal: a bad password is detected by a timeout (up to ~30 s).
- DHCP is done by `inet autoconf` (dhcpleased). If no device is found, try `fw_update`.

## License

GPL-3.0
