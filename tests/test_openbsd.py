import os
import unittest

from wifzf import openbsd as ob

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def line(ssid, sig, chan, flags):
    return f"\t\tnwid {ssid} chan {chan} bssid 00:11:22:33:44:55 {sig}% HT-MCS1 {flags}\n"


class ParseScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aps = ob.parse_scan(fixture("openbsd_scan.txt"))
        cls.by_name = {a.name: a for a in cls.aps}

    def test_hidden_skipped(self):
        self.assertNotIn("", self.by_name)
        self.assertEqual(len(self.aps), len(self.by_name))

    def test_quoted_ssid_with_spaces(self):
        self.assertIn("FRITZ!Box 7530 SO", self.by_name)
        self.assertEqual(self.by_name["Fancy Bear"].ssid, b"Fancy Bear")

    def test_dedup_keeps_strongest(self):
        self.assertEqual(self.by_name["Fancy Bear"].signal, 56)
        self.assertEqual(self.by_name["MagentaWLAN-W8JI"].chan, 48)  # 65% beats 59% on chan 6

    def test_sorted_descending(self):
        sigs = [a.signal for a in self.aps]
        self.assertEqual(sigs, sorted(sigs, reverse=True))

    def test_fixture_security_and_band(self):
        self.assertEqual(self.by_name["MagentaWLAN-W8JI"].security, "WPA2")  # wpa3,wpa2
        self.assertEqual(self.by_name["Vodafone Hotspot"].security, "open")
        self.assertEqual(self.by_name["MagentaWLAN-W8JI"].band, "5GHz")
        self.assertEqual(self.by_name["Vodafone-993D"].band, "2.4G")

    def test_wpa3_only_and_wep(self):
        aps = {a.name: a for a in ob.parse_scan(
            line("Sae", 50, 6, "privacy,wpa3") + line("Old", 40, 6, "privacy"))}
        self.assertEqual(aps["Sae"].security, "WPA3")
        self.assertEqual(aps["Old"].security, "WEP")

    def test_hex_ssid_decoded_to_bytes(self):
        (ap,) = ob.parse_scan(line("0x414243", 50, 1, "privacy,wpa2"))
        self.assertEqual(ap.ssid, b"ABC")

    def test_band_boundary(self):
        self.assertEqual(ob.AP(b"x", 1, 14, "").band, "2.4G")
        self.assertEqual(ob.AP(b"x", 1, 15, "").band, "5GHz")


class ParseIfconfigTest(unittest.TestCase):
    def test_joined_sample(self):
        st = ob.parse_ifconfig(fixture("openbsd_ifconfig_joined.txt"))
        self.assertEqual(st["wpa_state"], "COMPLETED")
        self.assertEqual(st["ssid"], "Vodafone-993D")
        self.assertEqual(st["ssid_hex"], b"Vodafone-993D".hex())

    def test_active_nwid_line_with_quoted_ssid(self):
        text = ('iwm0: flags=808843<UP,RUNNING> mtu 1500\n\tstatus: active\n'
                '\tieee80211: nwid "Vodafone Hotspot" chan 11 bssid 02:00:00:00:01:00 59%\n')
        self.assertEqual(ob.parse_ifconfig(text)["ssid"], "Vodafone Hotspot")

    def test_no_network_is_disconnected(self):
        text = "iwm0: flags=808843<UP,RUNNING> mtu 1500\n\tstatus: no network\n"
        self.assertEqual(ob.parse_ifconfig(text), {"wpa_state": "DISCONNECTED"})

    def test_not_up_is_inactive(self):
        text = "iwm0: flags=808802<BROADCAST,SIMPLEX> mtu 1500\n\tstatus: no network\n"
        self.assertEqual(ob.parse_ifconfig(text), {"wpa_state": "INACTIVE"})


HOSTNAME = (
    "# my wifi\n"
    "join Home wpakey secret123\n"
    'join "Cafe Free"\n'
    "inet autoconf\n"
    'join 0x414243 wpakey "pass word"  # trailing comment\n'
    "up\n"
)


class HostnameTest(unittest.TestCase):
    def test_parse_hostname(self):
        got = [(s.ssid, s.key) for s in ob.parse_hostname(HOSTNAME)]
        self.assertEqual(got, [(b"Home", "secret123"), (b"Cafe Free", None), (b"ABC", "pass word")])

    def test_parse_join_rejects_non_join(self):
        for l in ("# join Home\n", "inet autoconf\n", "join\n", 'join "unterminated\n'):
            self.assertIsNone(ob.parse_join(l), l)

    def test_add_inserts_after_last_join_and_preserves_rest(self):
        out = ob.add_join_line(HOSTNAME, "join New")
        self.assertEqual(out, HOSTNAME.replace("up\n", "join New\nup\n"))

    def test_add_without_trailing_newline(self):
        out = ob.add_join_line("join Home\ninet autoconf", "join New")
        self.assertEqual(out, "join Home\njoin New\ninet autoconf")
        self.assertEqual(ob.add_join_line("join Home", "join New"), "join Home\njoin New\n")

    def test_add_without_joins_goes_to_top(self):
        self.assertEqual(ob.add_join_line("inet autoconf\n", "join New"), "join New\ninet autoconf\n")

    def test_remove_only_matching_ssid(self):
        out = ob.remove_join_lines(HOSTNAME, b"Home")
        self.assertEqual(out, HOSTNAME.replace("join Home wpakey secret123\n", ""))
        self.assertEqual(ob.remove_join_lines(HOSTNAME, b"Nope"), HOSTNAME)


class EncodeTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(ob.encode_id(b"Home"), "Home")

    def test_hex_for_unsafe(self):
        for s in (b"Has Space", b'a"b', b"a'b", b"a#b", b"a$b", b"a\\b",
                  "Café".encode(), b"-flag", b"0xZZ", b"0x41"):
            self.assertEqual(ob.encode_id(s), "0x" + s.hex(), s)

    def test_round_trip(self):
        for s in (b"Home", b"Has Space", b'q"uote', "Café ☃".encode(), b"-x", b"0x41", b"\xff\x00"):
            self.assertEqual(ob.decode_ssid(ob.encode_id(s)), s)


class JoinLineTest(unittest.TestCase):
    """netstart eval()s hostname.if lines as root: nothing written may be shell-active."""

    def test_readable_forms(self):
        self.assertEqual(ob.join_line(b"Home", None), "join Home")
        self.assertEqual(ob.join_line(b"My Phone", "fakepass1"), 'join "My Phone" wpakey "fakepass1"')
        self.assertEqual(ob.join_line(b"AT&T", "P@ss&word1"), 'join "AT&T" wpakey "P@ss&word1"')
        self.assertEqual(ob.join_line(b"x;reboot", None), 'join "x;reboot"')  # ; is inert inside quotes

    def test_hostile_ssids_become_hex(self):
        for s in [b"a`reboot`", b"Free `reboot`", b"$(reboot)", b'a"b', b"a\\b", b"a#b", b"-x", "Café".encode()]:
            line = ob.join_line(s, None)
            self.assertEqual(line, "join 0x" + s.hex(), s)
            self.assertEqual(ob.parse_join(line).ssid, s)

    def test_unsafe_passphrases_refused(self):
        for p in ['a"b', "a$b", "a`b", "a\\b", "a#b"]:
            with self.assertRaises(ob.WifiError):
                ob.join_line(b"Home", p)

    def test_round_trip_through_parser(self):
        n = ob.parse_join(ob.join_line(b"My Net", "pass word;&|()<>'"))
        self.assertEqual((n.ssid, n.key), (b"My Net", "pass word;&|()<>'"))


if __name__ == "__main__":
    unittest.main()
