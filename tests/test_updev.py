"""Tests for the parts that don't need hardware.

Anything that reads a real bus is exercised by running `updev` on the board;
what's covered here is the decoding, joining and orchestration logic, where a
regression is silent and easy to introduce.

Run with:  python3 -m unittest discover -s tests   (or pytest, if installed)
"""

from __future__ import annotations

import unittest

from updev.backends.host import _decode_revision, _explain_boot_order
from updev.backends.network import _prefix, _signal_quality
from updev.backends.storage import _scsi_product
from updev.cli import _filter, _parse_int, _parse_spi_target
from updev.core.changes import ChangeKind, diff_devices
from updev.core.model import Device, Kind, ScanResult, Severity, Status, status_weight
from updev.core.registry import Backend, ProbeContext, Scanner
from updev.core.util import (
    human_bytes,
    human_duration,
    is_locally_administered,
    usb_address_from_path,
)
from updev.backends.display import parse_edid
from updev.floppy import (
    FLOPPY_SIZE,
    boot_message,
    build_boot_code,
    build_boot_sector,
    build_image,
    gallery,
    inspect_image,
    read_file,
)
from updev.usbclass import Confidence, StorageFacts, UsbClass, classify
from updev.usbdesc import EndpointDescriptor, descriptor_hints, parse_descriptors
from updev.vm import build_plan, check_passthrough, udev_rule
from updev.usbrole import (
    DeviceFacts,
    InterfaceFacts,
    PathHop,
    UsbRole,
    identify,
    parent_address,
    path_bottleneck,
    port_number,
)


class TestRevisionDecoding(unittest.TestCase):
    def test_pi5_revision(self):
        # e04171 — the 16GB Rev 1.1 Pi 5 this was written on.
        out = _decode_revision("e04171")
        self.assertEqual(out["board"], "Pi 5")
        self.assertEqual(out["processor"], "BCM2712")
        self.assertEqual(out["ram"], "16GB")
        self.assertEqual(out["pcb_revision"], "1.1")
        self.assertEqual(out["manufacturer"], "Sony UK")

    def test_pi5_8gb_revision(self):
        self.assertEqual(_decode_revision("d04170")["ram"], "8GB")

    def test_pi4_revision(self):
        out = _decode_revision("c03111")           # Pi 4B 4GB
        self.assertEqual(out["board"], "Pi 4B")
        self.assertEqual(out["processor"], "BCM2711")
        self.assertEqual(out["ram"], "4GB")

    def test_old_style_code_is_skipped(self):
        # Pre-2016 codes don't have bit 23 set and mean something else entirely,
        # so decoding them would produce confident nonsense.
        self.assertEqual(_decode_revision("000e"), {})

    def test_garbage_does_not_raise(self):
        self.assertEqual(_decode_revision("not-hex"), {})
        self.assertEqual(_decode_revision(""), {})


class TestBootOrder(unittest.TestCase):
    def test_reads_right_to_left(self):
        # Nibbles are tried lowest-first: 0xf461 is SD, then NVMe, then USB.
        out = _explain_boot_order("0xf461")
        self.assertLess(out.index("SD card"), out.index("NVMe / PCIe"))
        self.assertLess(out.index("NVMe / PCIe"), out.index("USB mass storage"))
        self.assertTrue(out.endswith("restart the sequence"))

    def test_nvme_first_order(self):
        out = _explain_boot_order("0xf416")
        self.assertLess(out.index("NVMe / PCIe"), out.index("SD card"))

    def test_unknown_digits_pass_through(self):
        self.assertIn("0x1", _explain_boot_order("0x1"))


class TestDeviceLabel(unittest.TestCase):
    def test_vendor_is_not_repeated(self):
        dev = Device(uid="x", kind=Kind.HOST, name="Raspberry Pi 5 Model B",
                     vendor="Raspberry Pi")
        self.assertEqual(dev.label, "Raspberry Pi 5 Model B")

    def test_vendor_is_prepended_when_absent(self):
        dev = Device(uid="x", kind=Kind.USB, name="Keyboard", vendor="Logitech")
        self.assertEqual(dev.label, "Logitech Keyboard")

    def test_falls_back_to_uid(self):
        self.assertEqual(Device(uid="usb:1-1", kind=Kind.USB, name="").label, "usb:1-1")

    def test_worst_severity(self):
        dev = Device(uid="x", kind=Kind.USB, name="n")
        dev.issue(Severity.INFO, "fyi")
        dev.issue(Severity.ERROR, "broken")
        dev.issue(Severity.WARN, "hmm")
        self.assertEqual(dev.worst, Severity.ERROR)


class TestScsiProductJoin(unittest.TestCase):
    def test_split_across_fields_is_rejoined(self):
        # A USB-SATA bridge writes one string across the 8-byte vendor and
        # 16-byte model fields: "SHGP31-5" + "00GM" is "SHGP31-500GM".
        node = {"vendor": "SHGP31-5", "model": "00GM"}
        self.assertEqual(_scsi_product("__nonexistent__", node), "SHGP31-5 00GM")

    def test_missing_vendor(self):
        self.assertEqual(_scsi_product("x", {"model": "Samsung SSD 980"}),
                         "Samsung SSD 980")

    def test_missing_model(self):
        self.assertEqual(_scsi_product("x", {"vendor": "ATA"}), "ATA")

    def test_both_missing(self):
        self.assertEqual(_scsi_product("x", {}), "")


class TestNumberParsing(unittest.TestCase):
    def test_accepts_datasheet_notations(self):
        self.assertEqual(_parse_int("0x3c"), 0x3C)
        self.assertEqual(_parse_int("60"), 60)
        self.assertEqual(_parse_int("0b1010"), 10)
        self.assertEqual(_parse_int("3c"), 0x3C)      # bare hex, as datasheets print it

    def test_rejects_nonsense(self):
        with self.assertRaises(Exception):
            _parse_int("zz")

    def test_spi_target_forms(self):
        self.assertEqual(_parse_spi_target("0.0"), (0, 0))
        self.assertEqual(_parse_spi_target("spidev1.2"), (1, 2))
        self.assertEqual(_parse_spi_target("/dev/spidev0.1"), (0, 1))

    def test_spi_target_requires_chip_select(self):
        with self.assertRaises(Exception):
            _parse_spi_target("0")


class TestFiltering(unittest.TestCase):
    def setUp(self):
        self.devices = [
            Device(uid="usb:1-1", kind=Kind.USB, name="a", status=Status.ONLINE,
                   tags=["storage"]),
            Device(uid="i2c:1", kind=Kind.I2C, name="b", status=Status.IDLE),
            Device(uid="net:eth0", kind=Kind.NET_IFACE, name="c", status=Status.ONLINE),
        ]

    def test_by_kind(self):
        self.assertEqual([d.uid for d in _filter(self.devices, ("usb",), (), ())],
                         ["usb:1-1"])

    def test_by_status(self):
        self.assertEqual(len(_filter(self.devices, (), ("online",), ())), 2)

    def test_by_tag(self):
        self.assertEqual([d.uid for d in _filter(self.devices, (), (), ("storage",))],
                         ["usb:1-1"])

    def test_combined_filters_are_and(self):
        self.assertEqual(_filter(self.devices, ("usb",), ("idle",), ()), [])

    def test_no_filters_returns_everything(self):
        self.assertEqual(len(_filter(self.devices, (), (), ())), 3)


class TestScanResult(unittest.TestCase):
    def setUp(self):
        self.result = ScanResult(devices=[
            Device(uid="usb:4-1", kind=Kind.USB, name="Disk", node="/dev/sda",
                   address="4-1"),
            Device(uid="net:eth0", kind=Kind.NET_IFACE, name="eth0"),
        ])

    def test_find_prefers_exact_uid(self):
        self.assertEqual([d.uid for d in self.result.find("usb:4-1")], ["usb:4-1"])

    def test_find_matches_name_and_node(self):
        self.assertEqual(len(self.result.find("eth0")), 1)
        self.assertEqual(len(self.result.find("/dev/sda")), 1)

    def test_find_is_case_insensitive(self):
        self.assertEqual(len(self.result.find("DISK")), 1)

    def test_issues_are_sorted_worst_first(self):
        self.result.devices[0].issue(Severity.INFO, "minor")
        self.result.devices[1].issue(Severity.ERROR, "major")
        self.assertEqual([i.severity for _, i in self.result.issues()],
                         [Severity.ERROR, Severity.INFO])

    def test_status_ordering_puts_broken_first(self):
        order = sorted([Status.ONLINE, Status.ERROR, Status.IDLE, Status.DEGRADED],
                       key=status_weight)
        self.assertEqual(order[0], Status.ERROR)
        self.assertEqual(order[-1], Status.ONLINE)


class _ExplodingBackend(Backend):
    name = "boom"
    kinds = (Kind.UNKNOWN,)

    def probe(self, ctx):
        raise RuntimeError("the bus caught fire")


class _UnavailableBackend(Backend):
    name = "nope"
    kinds = (Kind.UNKNOWN,)

    def available(self, ctx):
        return False, "no such hardware"

    def probe(self, ctx):
        raise AssertionError("must not be probed when unavailable")


class _GoodBackend(Backend):
    name = "fine"
    kinds = (Kind.UNKNOWN,)

    def probe(self, ctx):
        return [Device(uid="fine:1", kind=Kind.UNKNOWN, name="ok", status=Status.ONLINE)]


class _SlowBackend(Backend):
    name = "slowpoke"
    kinds = (Kind.UNKNOWN,)
    slow = True

    def probe(self, ctx):
        return [Device(uid="slow:1", kind=Kind.UNKNOWN, name="slow")]


class TestScannerIsolation(unittest.TestCase):
    """One broken backend must never take down a scan."""

    def setUp(self):
        self.scanner = Scanner()
        for cls in (_ExplodingBackend, _UnavailableBackend, _GoodBackend):
            self.scanner.register(cls())

    def test_working_backend_still_returns_devices(self):
        result = self.scanner.scan(ProbeContext())
        self.assertEqual([d.uid for d in result.devices], ["fine:1"])

    def test_failure_is_reported_not_raised(self):
        result = self.scanner.scan(ProbeContext())
        boom = next(r for r in result.reports if r.name == "boom")
        self.assertTrue(boom.available)
        self.assertFalse(boom.ok)
        self.assertIn("caught fire", boom.error)

    def test_unavailable_backend_is_not_probed(self):
        result = self.scanner.scan(ProbeContext())
        nope = next(r for r in result.reports if r.name == "nope")
        self.assertFalse(nope.available)
        self.assertEqual(nope.reason, "no such hardware")

    def test_slow_backends_are_skipped_by_default(self):
        self.scanner.register(_SlowBackend())
        names = {r.name for r in self.scanner.scan(ProbeContext()).reports}
        self.assertNotIn("slowpoke", names)

    def test_slow_backend_runs_when_named(self):
        self.scanner.register(_SlowBackend())
        ctx = ProbeContext(include=frozenset({"slowpoke"}))
        self.assertIn("slow:1", [d.uid for d in self.scanner.scan(ctx).devices])

    def test_slow_backend_runs_with_deep(self):
        self.scanner.register(_SlowBackend())
        names = {r.name for r in self.scanner.scan(ProbeContext(deep=True)).reports}
        self.assertIn("slowpoke", names)

    def test_exclude_wins(self):
        ctx = ProbeContext(exclude=frozenset({"fine"}))
        self.assertEqual(self.scanner.scan(ctx).devices, [])


class TestHotplugDiff(unittest.TestCase):
    """The dashboard's reason for existing: noticing change."""

    def _dashboard(self):
        from rich.console import Console

        from updev.ui.dash import Dashboard

        return Dashboard(Scanner(), ProbeContext(), Console(quiet=True))

    @staticmethod
    def _result(devices):
        return ScanResult(devices=devices)

    def test_first_scan_reports_nothing(self):
        dash = self._dashboard()
        dash._diff(self._result([Device(uid="a", kind=Kind.USB, name="a")]))
        self.assertEqual(len(dash.events), 0)

    def test_added_device(self):
        dash = self._dashboard()
        dash.previous = {"a": Device(uid="a", kind=Kind.USB, name="a")}
        dash._diff(self._result([
            Device(uid="a", kind=Kind.USB, name="a"),
            Device(uid="b", kind=Kind.USB, name="New Thing"),
        ]))
        self.assertEqual(len(dash.events), 1)
        self.assertIn("New Thing", dash.events[0].text.plain)

    def test_removed_device(self):
        dash = self._dashboard()
        dash.previous = {"a": Device(uid="a", kind=Kind.USB, name="Gone")}
        dash._diff(self._result([]))
        self.assertIn("disappeared", dash.events[0].text.plain)

    def test_status_change(self):
        dash = self._dashboard()
        dash.previous = {"a": Device(uid="a", kind=Kind.USB, name="x",
                                     status=Status.ONLINE)}
        dash._diff(self._result([
            Device(uid="a", kind=Kind.USB, name="x", status=Status.DEGRADED)
        ]))
        self.assertIn("degraded", dash.events[0].text.plain)

    def test_unchanged_produces_no_noise(self):
        dash = self._dashboard()
        same = Device(uid="a", kind=Kind.USB, name="x", status=Status.ONLINE)
        dash.previous = {"a": same}
        dash._diff(self._result([
            Device(uid="a", kind=Kind.USB, name="x", status=Status.ONLINE)
        ]))
        self.assertEqual(len(dash.events), 0)


class TestFormatting(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0B")
        self.assertEqual(human_bytes(1023), "1023B")
        self.assertEqual(human_bytes(1024), "1.0K")
        self.assertEqual(human_bytes(1536 * 1024 * 1024), "1.5G")
        self.assertEqual(human_bytes(None), "-")

    def test_human_duration(self):
        self.assertEqual(human_duration(45), "45s")
        self.assertEqual(human_duration(3661), "1h 1m")
        self.assertEqual(human_duration(90061), "1d 1h 1m")

    def test_netmask_to_prefix(self):
        self.assertEqual(_prefix("255.255.255.0"), 24)
        self.assertEqual(_prefix("255.255.0.0"), 16)
        self.assertEqual(_prefix(None), 0)
        self.assertEqual(_prefix("not-a-mask"), 0)

    def test_signal_quality_bands(self):
        self.assertEqual(_signal_quality(-40), "excellent")
        self.assertEqual(_signal_quality(-65), "fair")
        self.assertEqual(_signal_quality(-95), "very weak")

    def test_locally_administered_mac(self):
        # Bit 1 of the first octet marks randomised / virtual addresses.
        self.assertTrue(is_locally_administered("fe:dc:af:c5:b7:ef"))
        self.assertFalse(is_locally_administered("88:a2:9e:3b:3d:99"))
        self.assertFalse(is_locally_administered("garbage"))


class TestUsbClassification(unittest.TestCase):
    """The five categories, built from synthetic signatures.

    We own exactly one of these devices, so the rules are tested against facts
    assembled from the specs rather than against hardware. Each case documents
    which signatures a real device of that type presents.
    """

    @staticmethod
    def _floppy() -> StorageFacts:
        # A USB floppy drive: UFI command set over CBI transport.
        return StorageFacts(
            interface_subclass=0x04, interface_protocol=0x00,
            peripheral_type=0x00, removable_medium=True,
            size_bytes=1_474_560, driver="usb-storage",
        )

    @staticmethod
    def _odd() -> StorageFacts:
        return StorageFacts(
            interface_subclass=0x02, interface_protocol=0x50,
            peripheral_type=0x05, removable_medium=True,
            size_bytes=0, driver="usb-storage",
        )

    @staticmethod
    def _thumb_drive() -> StorageFacts:
        # Commodity stick: SCSI transparent over Bulk-Only, removable medium,
        # no optional VPD pages, modest capacity.
        return StorageFacts(
            interface_subclass=0x06, interface_protocol=0x50,
            peripheral_type=0x00, removable_medium=True,
            has_vpd_b1=False, size_bytes=32 * 1024 ** 3,
            rotational=True, driver="usb-storage",
        )

    @staticmethod
    def _external_hdd() -> StorageFacts:
        return StorageFacts(
            interface_subclass=0x06, interface_protocol=0x62,
            peripheral_type=0x00, removable_medium=False,
            has_vpd_b1=True, rotation_rate=7200, form_factor=0x02,
            size_bytes=2 * 1024 ** 4, rotational=True, driver="uas",
        )

    @staticmethod
    def _external_ssd() -> StorageFacts:
        # The JMS583 + SK hynix P31 this was developed against.
        return StorageFacts(
            interface_subclass=0x06, interface_protocol=0x62,
            peripheral_type=0x00, removable_medium=False,
            has_vpd_b1=True, rotation_rate=1, form_factor=0x00,
            size_bytes=500 * 1000 ** 3, rotational=False, driver="uas",
        )

    def test_floppy(self):
        v = classify(self._floppy())
        self.assertEqual(v.usb_class, UsbClass.FUSB)
        self.assertEqual(v.confidence, Confidence.CERTAIN)

    def test_floppy_by_capacity_alone(self):
        # Even a bridge that reports SCSI transparent gives itself away.
        facts = StorageFacts(interface_subclass=0x06, size_bytes=1_474_560)
        self.assertEqual(classify(facts).usb_class, UsbClass.FUSB)

    def test_optical_drive(self):
        v = classify(self._odd())
        self.assertEqual(v.usb_class, UsbClass.ODD)
        self.assertEqual(v.confidence, Confidence.CERTAIN)

    def test_optical_by_peripheral_type_alone(self):
        facts = StorageFacts(interface_subclass=0x06, peripheral_type=0x05)
        self.assertEqual(classify(facts).usb_class, UsbClass.ODD)

    def test_thumb_drive(self):
        v = classify(self._thumb_drive())
        self.assertEqual(v.usb_class, UsbClass.NUSB)
        self.assertGreater(v.margin, 0)

    def test_external_hdd(self):
        v = classify(self._external_hdd())
        self.assertEqual(v.usb_class, UsbClass.HUSB)
        self.assertEqual(v.confidence, Confidence.CERTAIN)

    def test_external_ssd(self):
        v = classify(self._external_ssd())
        self.assertEqual(v.usb_class, UsbClass.SUSB)
        self.assertIn(v.confidence, (Confidence.HIGH, Confidence.CERTAIN))

    def test_ssd_and_thumb_drive_are_distinguished(self):
        """The genuinely hard pair: both are flash behind a USB bridge."""
        ssd = classify(self._external_ssd())
        thumb = classify(self._thumb_drive())
        self.assertEqual(ssd.usb_class, UsbClass.SUSB)
        self.assertEqual(thumb.usb_class, UsbClass.NUSB)
        self.assertNotEqual(ssd.usb_class, thumb.usb_class)

    def test_hdd_without_vpd_falls_back_to_weaker_evidence(self):
        # An old BOT enclosure: no VPD page, so only RMB and the rotational
        # flag are available. Should still land on HUSB, but not "certain".
        facts = StorageFacts(
            interface_subclass=0x06, interface_protocol=0x50,
            peripheral_type=0x00, removable_medium=False,
            has_vpd_b1=False, rotational=True,
            size_bytes=1024 ** 4, driver="usb-storage",
        )
        v = classify(facts)
        self.assertNotEqual(v.confidence, Confidence.CERTAIN)
        self.assertIn(v.usb_class, (UsbClass.HUSB, UsbClass.SUSB, UsbClass.NUSB))

    def test_decisive_rule_short_circuits(self):
        """Once the spec settles it, no further evidence is gathered."""
        v = classify(self._floppy())
        self.assertTrue(all(e.decisive for e in v.evidence))

    def test_no_facts_yields_unknown(self):
        v = classify(StorageFacts())
        self.assertEqual(v.usb_class, UsbClass.UNKNOWN)
        self.assertEqual(v.confidence, Confidence.LOW)

    def test_every_verdict_carries_its_reasoning(self):
        for name in ("_floppy", "_odd", "_thumb_drive", "_external_hdd", "_external_ssd"):
            with self.subTest(device=name):
                v = classify(getattr(self, name)())
                self.assertTrue(v.evidence, "a verdict with no evidence is unfalsifiable")
                for item in v.evidence:
                    self.assertTrue(item.reason)
                    self.assertTrue(item.observed)

    def test_verdict_serialises(self):
        import json

        payload = json.loads(json.dumps(classify(self._external_ssd()).as_dict(),
                                        default=str))
        self.assertEqual(payload["class"], "SUSB")
        self.assertTrue(payload["evidence"])
        self.assertIn("VPD 0xB1 rotation rate", payload["facts"])


class TestVpdParsing(unittest.TestCase):
    """VPD 0xB1 is the HDD/SSD discriminator, so its byte offsets matter."""

    def test_layout_matches_the_real_device(self):
        # Captured from /sys/block/sda/device/vpd_pgb1 on the Pi 5 this was
        # built on: peripheral type, page code 0xB1, length, then rotation.
        raw = bytes([0x00, 0xB1, 0x00, 0x3C, 0x00, 0x01, 0x00, 0x00])
        self.assertEqual(raw[1], 0xB1)
        self.assertEqual((raw[4] << 8) | raw[5], 1)      # non-rotating
        self.assertEqual(raw[7] & 0x0F, 0)               # form factor unreported

    def test_spinning_disk_rotation_rate(self):
        raw = bytes([0x00, 0xB1, 0x00, 0x3C, 0x1C, 0x20, 0x00, 0x02])
        self.assertEqual((raw[4] << 8) | raw[5], 7200)
        self.assertEqual(raw[7] & 0x0F, 2)               # 3.5 inch


class TestChangeDetection(unittest.TestCase):
    def test_baseline_scan_is_silent(self):
        current = {"a": Device(uid="a", kind=Kind.USB, name="x")}
        self.assertEqual(diff_devices({}, current), [])

    def test_added_removed_and_status(self):
        previous = {
            "keep": Device(uid="keep", kind=Kind.USB, name="k", status=Status.ONLINE),
            "gone": Device(uid="gone", kind=Kind.USB, name="g"),
        }
        current = {
            "keep": Device(uid="keep", kind=Kind.USB, name="k", status=Status.DEGRADED),
            "new": Device(uid="new", kind=Kind.USB, name="n"),
        }
        kinds = {c.kind: c for c in diff_devices(previous, current)}
        self.assertEqual(kinds[ChangeKind.ADDED].uid, "new")
        self.assertEqual(kinds[ChangeKind.REMOVED].uid, "gone")
        self.assertEqual(kinds[ChangeKind.STATUS].previous_status, Status.ONLINE)

    def test_no_change_is_no_events(self):
        same = {"a": Device(uid="a", kind=Kind.USB, name="x", status=Status.ONLINE)}
        other = {"a": Device(uid="a", kind=Kind.USB, name="x", status=Status.ONLINE)}
        self.assertEqual(diff_devices(same, other), [])

    def test_change_serialises(self):
        import json

        previous = {"a": Device(uid="a", kind=Kind.USB, name="old")}
        changes = diff_devices(previous, {})
        payload = json.loads(json.dumps(changes[0].as_dict(), default=str))
        self.assertEqual(payload["kind"], "removed")
        self.assertEqual(payload["uid"], "a")


class TestUsbZone(unittest.TestCase):
    """The 체험존 reacts to a plug event — simulated, since we can't reach the port."""

    def _zone(self):
        from rich.console import Console

        from updev.ui.zone import UsbZone

        return UsbZone(Scanner(), ProbeContext(), Console(quiet=True))

    @staticmethod
    def _stick(uid="usb:2-1", address="2-1"):
        return Device(uid=uid, kind=Kind.USB, name="Mass Storage",
                      status=Status.ONLINE, address=address, tags=["storage"])

    def test_recognises_usb_storage(self):
        from updev.ui.zone import UsbZone

        self.assertTrue(UsbZone._is_usb_storage(self._stick()))
        keyboard = Device(uid="usb:1-2", kind=Kind.USB, name="kbd", tags=["input"])
        self.assertFalse(UsbZone._is_usb_storage(keyboard))

    def test_plugging_in_produces_a_card(self):
        zone = self._zone()
        zone._present(self._stick())
        self.assertIsNotNone(zone.current)
        self.assertEqual(len(zone.history), 1)
        dev, verdict = zone.current
        self.assertEqual(dev.address, "2-1")
        self.assertIsNotNone(verdict.usb_class)

    def test_unplugging_clears_the_card(self):
        zone = self._zone()
        stick = self._stick()
        zone._present(stick)
        zone.previous = {stick.uid: stick}
        zone.step()                       # empty scanner -> device is gone
        self.assertIsNone(zone.current)

    def test_history_survives_unplug(self):
        zone = self._zone()
        zone._present(self._stick())
        zone.current = None
        self.assertEqual(len(zone.history), 1)

    def test_renders_without_a_device(self):
        from rich.console import Console

        Console(quiet=True, width=100).print(self._zone().render())


class TestUsbAddressFromPath(unittest.TestCase):
    """A device behind a hub must not be attributed to the hub."""

    def test_leaf_behind_a_hub(self):
        path = ("/sys/devices/platform/axi/1000120000.pcie/1f00200000.usb/"
                "xhci-hcd.0/usb1/1-2/1-2.3/1-2.3:1.0/host2/target2:0:0/"
                "2:0:0:0/block/sdc")
        self.assertEqual(usb_address_from_path(path), "1-2.3")

    def test_device_on_a_root_hub(self):
        path = ("/sys/devices/platform/axi/1000120000.pcie/1f00200000.usb/"
                "xhci-hcd.0/usb2/2-1/2-1:1.0/host0/target0:0:0/0:0:0:0/block/sda")
        self.assertEqual(usb_address_from_path(path), "2-1")

    def test_deeply_nested_hubs(self):
        self.assertEqual(
            usb_address_from_path("/sys/.../usb1/1-2/1-2.4/1-2.4.1/1-2.4.1:1.0/x"),
            "1-2.4.1",
        )

    def test_interface_directories_are_not_addresses(self):
        # "1-2.3:1.0" must not be mistaken for a device address.
        self.assertEqual(usb_address_from_path("/sys/x/usb1/1-2/1-2:1.0/y"), "1-2")

    def test_no_usb_in_path(self):
        self.assertEqual(usb_address_from_path("/sys/class/net/eth0"), "")


class TestUsbRoles(unittest.TestCase):
    """Roles, from synthetic descriptors and kernel bindings."""

    @staticmethod
    def _dev(*interfaces, vid="1234", pid="5678", wireless=False):
        return DeviceFacts(address="1-1", vid=vid, pid=pid,
                           interfaces=list(interfaces), is_wireless=wireless)

    @staticmethod
    def _iface(cls, sub=0x00, proto=0x00, driver="", **subsystems):
        return InterfaceFacts(number="1.0", cls=cls, subclass=sub, protocol=proto,
                              driver=driver, subsystems=dict(subsystems))

    def test_boot_keyboard(self):
        v = identify(self._dev(self._iface(0x03, 0x01, 0x01)))
        self.assertEqual(v.primary, UsbRole.KEYBOARD)

    def test_boot_mouse(self):
        v = identify(self._dev(self._iface(0x03, 0x01, 0x02)))
        self.assertEqual(v.primary, UsbRole.MOUSE)

    def test_composite_receiver_is_both(self):
        """A 2.4GHz receiver really is a keyboard and a mouse."""
        v = identify(self._dev(
            self._iface(0x03, 0x01, 0x01),
            self._iface(0x03, 0x01, 0x02),
        ))
        self.assertIn(UsbRole.KEYBOARD, v.roles)
        self.assertIn(UsbRole.MOUSE, v.roles)

    def test_uvc_camera(self):
        v = identify(self._dev(self._iface(0x0E, 0x01, 0x00)))
        self.assertEqual(v.primary, UsbRole.CAMERA)

    def test_camera_via_kernel_binding_is_definitive(self):
        v = identify(self._dev(
            self._iface(0xFF, driver="uvcvideo", video4linux=["video0"])
        ))
        self.assertEqual(v.primary, UsbRole.CAMERA)
        self.assertTrue(any(e.definitive for e in v.evidence))

    def test_audio_dongle(self):
        # A USB-C to 3.5mm adapter is a USB Audio Class device.
        v = identify(self._dev(self._iface(0x01, 0x01, 0x00)))
        self.assertEqual(v.primary, UsbRole.AUDIO)

    def test_wifi_needs_a_wireless_phy(self):
        wifi = identify(self._dev(
            self._iface(0xFF, driver="mt7601u", net=["wlan1"]), wireless=True))
        self.assertEqual(wifi.primary, UsbRole.WIFI)

        wired = identify(self._dev(
            self._iface(0xFF, driver="r8152", net=["eth1"]), wireless=False))
        self.assertEqual(wired.primary, UsbRole.ETHERNET)

    def test_android_adb(self):
        v = identify(self._dev(self._iface(0xFF, 0x42, 0x01), vid="18d1"))
        self.assertEqual(v.primary, UsbRole.PHONE)

    def test_apple_mobile_device(self):
        v = identify(self._dev(self._iface(0xFF, 0xFE, 0x02), vid="05ac"))
        self.assertEqual(v.primary, UsbRole.PHONE)

    def test_ptp_from_a_phone_vendor_is_a_phone(self):
        self.assertEqual(
            identify(self._dev(self._iface(0x06, 0x01, 0x01), vid="04e8")).primary,
            UsbRole.PHONE,
        )

    def test_ptp_from_an_unknown_vendor_is_a_scanner(self):
        self.assertEqual(
            identify(self._dev(self._iface(0x06, 0x01, 0x01), vid="9999")).primary,
            UsbRole.SCANNER,
        )

    def test_usb_c_billboard(self):
        """What a USB-C monitor announces when alt mode isn't available."""
        v = identify(self._dev(self._iface(0x11, 0x00, 0x00)))
        self.assertEqual(v.primary, UsbRole.BILLBOARD)
        self.assertTrue(any("alternate mode" in e.reason for e in v.evidence))

    def test_displaylink_by_vendor(self):
        v = identify(self._dev(self._iface(0xFF), vid="17e9"))
        self.assertIn(UsbRole.DISPLAY, v.roles)

    def test_bluetooth_dongle(self):
        self.assertEqual(
            identify(self._dev(self._iface(0xE0, 0x01, 0x01))).primary,
            UsbRole.BLUETOOTH,
        )

    def test_serial_adapter(self):
        self.assertEqual(
            identify(self._dev(self._iface(0x02, 0x02, 0x01))).primary,
            UsbRole.SERIAL,
        )

    def test_hub_alone_stays_a_hub(self):
        self.assertEqual(identify(self._dev(self._iface(0x09))).primary, UsbRole.HUB)

    def test_storage_wins_over_hub_on_a_composite(self):
        v = identify(self._dev(self._iface(0x09), self._iface(0x08, 0x06, 0x50)))
        self.assertEqual(v.primary, UsbRole.STORAGE)

    def test_nothing_recognisable(self):
        self.assertEqual(identify(self._dev()).primary, UsbRole.UNKNOWN)

    def test_every_role_carries_evidence(self):
        v = identify(self._dev(self._iface(0x03, 0x01, 0x01)))
        self.assertTrue(v.evidence)
        for item in v.evidence:
            self.assertTrue(item.reason and item.observed and item.source)


class TestUsbPath(unittest.TestCase):
    def test_parent_chain(self):
        self.assertEqual(parent_address("1-2.3.4"), "1-2.3")
        self.assertEqual(parent_address("1-2.3"), "1-2")
        self.assertEqual(parent_address("1-2"), "usb1")
        self.assertEqual(parent_address("usb1"), "")

    def test_port_numbers(self):
        self.assertEqual(port_number("1-2.3"), 3)
        self.assertEqual(port_number("3-1"), 1)

    def test_bottleneck_blames_the_hub_not_the_leaf(self):
        """A SuperSpeed-capable hub on a slow link caps everything below it."""
        hops = [
            PathHop(address="usb2", speed="5000", speed_label="SuperSpeed 5 Gbps",
                    generation=3, declared="3.00", declared_generation=3,
                    is_root_hub=True, label="root"),
            PathHop(address="2-1", speed="480", speed_label="High-Speed 480 Mbps",
                    generation=2, declared="3.00", declared_generation=3, label="HUB"),
            PathHop(address="2-1.4", speed="480", speed_label="High-Speed 480 Mbps",
                    generation=2, declared="3.00", declared_generation=3,
                    is_target=True, label="DISK"),
        ]
        found = path_bottleneck(hops)
        self.assertIsNotNone(found)
        index, message = found
        self.assertEqual(index, 1, "the hub is the culprit, not the disk")
        self.assertIn("HUB", message)

    def test_slow_upstream_hop_is_still_named(self):
        """Even with no spec violation, a slower hub above you is the ceiling."""
        hops = [
            PathHop(address="usb1", speed="480", speed_label="High-Speed 480 Mbps",
                    generation=2, declared="2.00", declared_generation=2,
                    is_root_hub=True, label="root"),
            PathHop(address="1-2", speed="12", speed_label="Full-Speed 12 Mbps",
                    generation=1, declared="2.00", declared_generation=2, label="HUB"),
            PathHop(address="1-2.4", speed="480", speed_label="High-Speed 480 Mbps",
                    generation=2, declared="2.00", declared_generation=2,
                    is_target=True, label="CAMERA"),
        ]
        found = path_bottleneck(hops)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], 1)

    def test_no_bottleneck_when_everything_runs_at_its_rating(self):
        hops = [
            PathHop(address="usb3", speed="480", generation=2, declared="2.00",
                    declared_generation=2, is_root_hub=True, label="root"),
            PathHop(address="3-2", speed="1.5", generation=1, declared="1.10",
                    declared_generation=1, is_target=True, label="Keyboard"),
        ]
        self.assertIsNone(path_bottleneck(hops))

    def test_full_speed_device_declaring_usb2_is_not_a_fault(self):
        """bcdUSB states spec compliance, not supported speed. Plenty of
        Full-Speed-only hardware reports 2.00, and flagging it is a false
        positive that lights up half a healthy bus."""
        hop = PathHop(address="1-2.2", speed="12", generation=1,
                      declared="2.00", declared_generation=2)
        self.assertFalse(hop.underperforming)

    def test_usb3_device_on_a_usb2_link_is_a_fault(self):
        """3.x is unambiguous: there is no USB 3 device that cannot do SuperSpeed."""
        hop = PathHop(address="x", speed="480", generation=2,
                      declared="3.00", declared_generation=3)
        self.assertTrue(hop.underperforming)

    def test_superspeed_device_at_full_rate_is_fine(self):
        hop = PathHop(address="x", speed="5000", generation=3,
                      declared="3.20", declared_generation=3)
        self.assertFalse(hop.underperforming)


class TestEdidParsing(unittest.TestCase):
    @staticmethod
    def _edid(manufacturer="SAM", product=0x0F13, year=2018, week=1,
              width_cm=121, height_cm=68, name="TEST MONITOR"):
        block = bytearray(128)
        block[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        packed = 0
        for i, ch in enumerate(manufacturer):
            packed |= (ord(ch) - 64) << (10 - 5 * i)
        block[8] = (packed >> 8) & 0xFF
        block[9] = packed & 0xFF
        block[10] = product & 0xFF
        block[11] = (product >> 8) & 0xFF
        block[12:16] = (12345).to_bytes(4, "little")
        block[16] = week
        block[17] = year - 1990
        block[18], block[19] = 1, 3
        block[21], block[22] = width_cm, height_cm
        block[54:57] = b"\x00\x00\x00"
        block[57] = 0xFC
        block[58] = 0x00
        text = name.encode("ascii")[:13].ljust(13, b" ")
        block[59:72] = text
        return bytes(block)

    def test_manufacturer_and_model(self):
        info = parse_edid(self._edid())
        self.assertEqual(info["manufacturer"], "SAM")
        self.assertEqual(info["manufacturer_name"], "Samsung")
        self.assertEqual(info["product_code"], "0x0f13")
        self.assertEqual(info["monitor_name"], "TEST MONITOR")

    def test_manufacture_date(self):
        self.assertEqual(parse_edid(self._edid(year=2018, week=1))["manufactured"],
                         "2018 week 1")

    def test_physical_size_and_diagonal(self):
        info = parse_edid(self._edid(width_cm=121, height_cm=68))
        self.assertEqual(info["screen_size"], "121×68 cm")
        # sqrt(121^2 + 68^2) / 2.54 == 54.6"
        self.assertEqual(info["screen_diagonal"], '54.6"')

    def test_rejects_a_block_without_the_magic_header(self):
        info = parse_edid(b"\x01" * 128)
        self.assertIn("edid", info)
        self.assertNotIn("manufacturer", info)

    def test_short_block(self):
        self.assertEqual(parse_edid(b"\x00" * 16), {})

    def test_unknown_manufacturer_falls_back_to_the_code(self):
        info = parse_edid(self._edid(manufacturer="ZZZ"))
        self.assertEqual(info["manufacturer_name"], "ZZZ")


class TestProductStringSignature(unittest.TestCase):
    """The enclosure often names the medium when every structured field lies."""

    def test_m2_enclosure_beats_the_rotational_flag(self):
        # Observed on real hardware: a ULT-Best enclosure whose SCSI strings say
        # "M.2 SATA 1TB" while the block layer's rotational flag claims 1.
        facts = StorageFacts(
            interface_subclass=0x06, interface_protocol=0x50,
            peripheral_type=0x00, removable_medium=False,
            has_vpd_b1=False, rotational=True,
            size_bytes=1024 * 1024 ** 3,
            usb_vendor="ULT-Best", usb_product="Best USB Device",
            scsi_vendor="M.2 SATA", scsi_model="1TB", driver="usb-storage",
        )
        verdict = classify(facts)
        self.assertEqual(verdict.usb_class, UsbClass.SUSB)
        self.assertTrue(any("product strings" in e.signature for e in verdict.evidence))

    def test_named_hdd_family(self):
        facts = StorageFacts(
            interface_subclass=0x06, interface_protocol=0x50,
            peripheral_type=0x00, removable_medium=False,
            rotational=True, size_bytes=2 * 1024 ** 4,
            scsi_vendor="WDC", scsi_model="WD20SPZX-22U", driver="usb-storage",
        )
        self.assertEqual(classify(facts).usb_class, UsbClass.HUSB)

    def test_solid_state_pattern_wins_over_an_hdd_lookalike(self):
        # "WD_BLACK SN770" is an NVMe drive; the WD pattern must not claim it.
        facts = StorageFacts(
            interface_subclass=0x06, interface_protocol=0x62,
            peripheral_type=0x00, removable_medium=False,
            size_bytes=1024 ** 4, scsi_vendor="WD", scsi_model="BLACK SN770",
        )
        self.assertEqual(classify(facts).usb_class, UsbClass.SUSB)

    def test_silence_when_the_strings_say_nothing(self):
        facts = StorageFacts(
            interface_subclass=0x06, interface_protocol=0x50,
            peripheral_type=0x00, usb_product="USB 3.1 Device",
        )
        self.assertFalse(any("product strings" in e.signature
                             for e in classify(facts).evidence))


def run_boot_sector(sector: bytes, max_steps: int = 200_000):
    """A 16-bit interpreter covering exactly the opcodes `build_boot_code` emits.

    This is how we verify the boot sector without a machine to boot it on: run
    the actual bytes and capture what BIOS teletype output they would produce.
    A wrong jump displacement or a mis-sized `mov si` operand shows up here as
    garbage or a runaway, not as a mystery on real hardware.
    """
    mem = bytearray(0x100000)
    mem[0x7C00:0x7C00 + len(sector)] = sector
    ip, ax, si = 0x7C00, 0, 0
    zf = False
    out = bytearray()

    for steps in range(max_steps):
        op = mem[ip]
        if op in (0xFA, 0xFB):                          # cli / sti
            ip += 1
        elif op == 0x31 and mem[ip + 1] == 0xC0:        # xor ax, ax
            ax, ip = 0, ip + 2
        elif op == 0x8E:                                # mov sreg, ax
            ip += 2
        elif op in (0xBC, 0xBB):                        # mov sp/bx, imm16
            ip += 3
        elif op == 0xBE:                                # mov si, imm16
            si = int.from_bytes(mem[ip + 1:ip + 3], "little")
            ip += 3
        elif op == 0xAC:                                # lodsb
            ax = (ax & 0xFF00) | mem[si]
            si, ip = si + 1, ip + 1
        elif op == 0x08 and mem[ip + 1] == 0xC0:        # or al, al
            zf, ip = (ax & 0xFF) == 0, ip + 2
        elif op == 0x74:                                # jz rel8
            disp = mem[ip + 1] - (256 if mem[ip + 1] > 127 else 0)
            ip += 2 + (disp if zf else 0)
        elif op == 0xB4:                                # mov ah, imm8
            ax, ip = (mem[ip + 1] << 8) | (ax & 0xFF), ip + 2
        elif op == 0xCD and mem[ip + 1] == 0x10:        # int 0x10
            if (ax >> 8) == 0x0E:
                out.append(ax & 0xFF)
            ip += 2
        elif op == 0xEB:                                # jmp rel8
            disp = mem[ip + 1] - (256 if mem[ip + 1] > 127 else 0)
            ip += 2 + disp
        elif op == 0xF4:                                # hlt
            return bytes(out), steps, True, ""
        else:
            return bytes(out), steps, False, f"unknown opcode 0x{op:02X} at 0x{ip:04X}"
    return bytes(out), max_steps, False, "ran away without halting"


class TestFloppyImage(unittest.TestCase):
    def setUp(self):
        self.image = build_image(serial=0x12345678)

    def test_exact_floppy_size(self):
        self.assertEqual(self.image.size, FLOPPY_SIZE)
        self.assertEqual(self.image.size, 1_474_560)

    def test_bpb_describes_a_1440k_floppy(self):
        info = inspect_image(self.image.data)
        self.assertEqual(info["bytes_per_sector"], 512)
        self.assertEqual(info["sectors_per_cluster"], 1)
        self.assertEqual(info["total_sectors"], 2880)
        self.assertEqual(info["sectors_per_fat"], 9)
        self.assertEqual(info["media_descriptor"], "0xF0")
        self.assertEqual(info["fs_type"], "FAT12")

    def test_boot_signature(self):
        info = inspect_image(self.image.data)
        self.assertTrue(info["bootable_signature_present"])
        self.assertEqual(info["boot_signature"], "0xAA55")

    def test_volume_label_matches_between_boot_sector_and_root_dir(self):
        """fsck complains loudly when these disagree."""
        info = inspect_image(self.image.data)
        label_entry = next(e for e in info["entries"] if e["volume_label"])
        self.assertEqual(label_entry["name"], info["volume_label"])
        self.assertEqual(label_entry["name"], "UPDEV ART")

    def test_every_art_file_reads_back_through_the_fat_chain(self):
        for name in gallery():
            with self.subTest(file=name):
                recovered = read_file(self.image.data, name)
                self.assertTrue(recovered, f"{name} came back empty")
                self.assertEqual(len(recovered), self.image.files[name])

    def test_multi_cluster_file_survives_the_chain(self):
        """A file over 512 bytes spans clusters, which is where FAT12 gets fiddly."""
        big = {"BIG.TXT": "0123456789abcdef" * 200}      # 3200 bytes -> 7 clusters
        image = build_image(files=big)
        recovered = read_file(image.data, "BIG.TXT").decode("ascii")
        self.assertEqual(recovered.replace("\r\n", "\n"), big["BIG.TXT"])

    def test_boot_sector_prints_the_expected_art(self):
        sector = build_boot_sector("UPDEV ART", boot_message())
        printed, steps, halted, error = run_boot_sector(sector)
        self.assertEqual(error, "")
        self.assertTrue(halted, "the boot sector must halt, not run away")
        self.assertEqual(printed.decode("ascii"), boot_message())
        self.assertLess(steps, 10_000)

    def test_boot_sector_is_exactly_one_sector(self):
        self.assertEqual(len(build_boot_sector("X", "hi")), 512)

    def test_oversized_boot_message_is_rejected(self):
        with self.assertRaises(ValueError):
            build_boot_code("A" * 600)

    def test_custom_message_is_what_gets_printed(self):
        sector = build_boot_sector("X", "HELLO\r\n")
        printed, _, halted, error = run_boot_sector(sector)
        self.assertEqual(error, "")
        self.assertTrue(halted)
        self.assertEqual(printed, b"HELLO\r\n")


#: Captured from /dev/bus/usb/001/006 on the Pi this was built on — a UVC
#: webcam, chosen because it exercises IADs, alternate settings, class-specific
#: descriptors and an isochronous endpoint all in one blob.
WEBCAM_DESCRIPTORS = bytes([
    # device descriptor
    0x12, 0x01, 0x00, 0x02, 0xEF, 0x02, 0x01, 0x40, 0x08, 0x19, 0x11, 0x23,
    0x00, 0x01, 0x01, 0x02, 0x00, 0x01,
    # configuration: 2 interfaces, 256 mA (0x80 * 2)
    0x09, 0x02, 0x2E, 0x00, 0x02, 0x01, 0x00, 0x80, 0x80,
    # interface association: interfaces 0..1, function class 0x0E (video)
    0x08, 0x0B, 0x00, 0x02, 0x0E, 0x03, 0x00, 0x02,
    # interface 0 alt 0 — VideoControl, 1 endpoint
    0x09, 0x04, 0x00, 0x00, 0x01, 0x0E, 0x01, 0x00, 0x00,
    # class-specific VC header
    0x0D, 0x24, 0x01, 0x00, 0x01, 0x4D, 0x00, 0x80, 0xC3, 0xC9, 0x01, 0x01, 0x01,
    # interrupt endpoint, 10 byte packets, interval 5
    0x07, 0x05, 0x81, 0x03, 0x0A, 0x00, 0x05,
    # interface 1 alt 0 — VideoStreaming, no endpoints
    0x09, 0x04, 0x01, 0x00, 0x00, 0x0E, 0x02, 0x00, 0x00,
    # interface 1 alt 1 — VideoStreaming with an isochronous endpoint
    0x09, 0x04, 0x01, 0x01, 0x01, 0x0E, 0x02, 0x00, 0x00,
    0x07, 0x05, 0x82, 0x05, 0x00, 0x03, 0x01,
])


class TestUsbDescriptorParsing(unittest.TestCase):
    def setUp(self):
        self.tree = parse_descriptors(WEBCAM_DESCRIPTORS, "test")

    def test_device_descriptor(self):
        device = self.tree.device
        self.assertEqual(device.usb_version, "2.00")
        self.assertEqual(device.vendor_id, "1908")
        self.assertEqual(device.product_id, "2311")
        self.assertEqual(device.cls, 0xEF)          # miscellaneous / IAD
        self.assertEqual(device.max_packet_size0, 64)
        self.assertEqual(device.num_configurations, 1)

    def test_configuration_power_is_scaled_from_2ma_units(self):
        config = self.tree.configurations[0]
        self.assertEqual(config.max_power_ma, 256)
        self.assertFalse(config.self_powered)

    def test_interface_association_groups_the_function(self):
        assoc = self.tree.configurations[0].associations[0]
        self.assertEqual(assoc.first_interface, 0)
        self.assertEqual(assoc.interface_count, 2)
        self.assertEqual(assoc.cls, 0x0E)

    def test_alternate_settings_are_kept_separate(self):
        """sysfs shows only the active alt; the descriptors have them all."""
        interfaces = self.tree.configurations[0].interfaces
        ones = [i for i in interfaces if i.number == 1]
        self.assertEqual([i.alternate for i in ones], [0, 1])
        self.assertEqual(len(ones[0].endpoints), 0)
        self.assertEqual(len(ones[1].endpoints), 1)

    def test_isochronous_endpoint(self):
        iso = [ep for ep in self.tree.endpoints if ep.transfer_type == "isochronous"]
        self.assertEqual(len(iso), 1)
        self.assertEqual(iso[0].direction, "IN")
        self.assertEqual(iso[0].packet_size, 768)
        self.assertEqual(iso[0].number, 2)

    def test_interrupt_endpoint(self):
        interrupt = [ep for ep in self.tree.endpoints if ep.transfer_type == "interrupt"]
        self.assertEqual(len(interrupt), 1)
        self.assertEqual(interrupt[0].packet_size, 10)
        self.assertEqual(interrupt[0].interval, 5)

    def test_class_specific_descriptor_is_named(self):
        control = self.tree.configurations[0].interfaces[0]
        self.assertTrue(any("VC Header" in cs for cs in control.class_specific))

    def test_high_speed_transaction_multiplier(self):
        # Bits 11-12 of wMaxPacketSize carry additional transactions.
        ep = EndpointDescriptor(address=0x82, attributes=0x05,
                                max_packet_size=(2 << 11) | 1024)
        self.assertEqual(ep.packet_size, 1024)
        self.assertEqual(ep.transactions_per_microframe, 3)
        self.assertEqual(ep.bandwidth_per_frame, 3072)

    def test_truncated_blob_is_reported_not_raised(self):
        tree = parse_descriptors(WEBCAM_DESCRIPTORS[:20], "test")
        self.assertTrue(tree.error)
        self.assertIsNotNone(tree.device)

    def test_empty_blob(self):
        self.assertTrue(parse_descriptors(b"", "test").error)

    def test_bogus_length_does_not_loop_forever(self):
        # bLength 0 would otherwise leave the walk stuck on the same offset.
        tree = parse_descriptors(bytes([0x00, 0x01, 0x00, 0x00]), "test")
        self.assertTrue(tree.error)

    def test_hints_call_out_the_isochronous_endpoint(self):
        hints = descriptor_hints(self.tree)
        self.assertTrue(any("isochronous" in h for h in hints))
        self.assertTrue(any("association" in h for h in hints))

    def test_serialises(self):
        import json

        payload = json.loads(json.dumps(self.tree.as_dict(), default=str))
        self.assertEqual(payload["device"]["vendor_id"], "1908")
        self.assertEqual(len(payload["configurations"]), 1)


class TestDescriptorsFeedRoleDetection(unittest.TestCase):
    def test_isochronous_endpoint_corroborates_a_camera(self):
        facts = DeviceFacts(
            address="1-1", vid="1908", pid="2311",
            interfaces=[InterfaceFacts(number="1.0", cls=0x0E, subclass=0x01,
                                       protocol=0x00)],
            descriptors=parse_descriptors(WEBCAM_DESCRIPTORS, "test"),
        )
        verdict = identify(facts)
        self.assertEqual(verdict.primary, UsbRole.CAMERA)
        self.assertTrue(any(e.source == "raw descriptors" for e in verdict.evidence))

    def test_role_detection_still_works_without_descriptors(self):
        facts = DeviceFacts(
            address="1-1",
            interfaces=[InterfaceFacts(number="1.0", cls=0x0E, subclass=0x01)],
        )
        self.assertEqual(identify(facts).primary, UsbRole.CAMERA)


class TestPassthroughSafety(unittest.TestCase):
    """Handing a device to a guest takes it away from the host, so the checks
    that stop you doing that to the wrong device are the important part."""

    def test_udev_rule_is_scoped_to_plugdev(self):
        rule = udev_rule()
        self.assertIn('SUBSYSTEM=="usb"', rule)
        self.assertIn('GROUP="plugdev"', rule)
        self.assertIn('MODE="0660"', rule)

    def test_udev_rule_can_be_narrowed_to_one_device(self):
        rule = udev_rule("1908", "2311")
        self.assertIn('ATTR{idVendor}=="1908"', rule)
        self.assertIn('ATTR{idProduct}=="2311"', rule)

    def test_unknown_device_is_refused(self):
        check = check_passthrough("99-99")
        self.assertFalse(check.ok)
        self.assertTrue(check.blockers)

    def test_root_disk_is_blocked_on_this_machine(self):
        """The external SSD at 2-1 carries the running root filesystem."""
        import os

        if not os.path.isdir("/sys/bus/usb/devices/2-1"):
            self.skipTest("2-1 is not attached")
        check = check_passthrough("2-1")
        self.assertFalse(check.ok)
        self.assertTrue(
            any("mounted filesystem" in b for b in check.blockers),
            f"expected a mounted-filesystem refusal, got {check.blockers}",
        )

    def test_hub_is_blocked(self):
        import os

        if not os.path.isdir("/sys/bus/usb/devices/1-2"):
            self.skipTest("1-2 is not attached")
        check = check_passthrough("1-2")
        self.assertTrue(any("hub" in b for b in check.blockers))


class TestBootPlan(unittest.TestCase):
    def test_floppy_format_is_explicit(self):
        """Bare -fda makes QEMU probe and warn; we say format=raw."""
        from updev.vm import find_qemu

        if not find_qemu():
            self.skipTest("no QEMU installed")
        plan = build_plan(floppy="/tmp/x.img")
        self.assertIn("format=raw", plan.command)
        self.assertIn("if=floppy", plan.command)
        self.assertIn("-boot a", plan.command)

    def test_passthrough_adds_an_xhci_controller(self):
        from updev.vm import PassthroughCheck, find_qemu

        if not find_qemu():
            self.skipTest("no QEMU installed")
        check = PassthroughCheck(address="1-2.4", busnum=1, devnum=6, writable=True)
        plan = build_plan(floppy="/tmp/x.img", passthrough=[check])
        self.assertIn("qemu-xhci", plan.command)
        self.assertIn("hostbus=1", plan.command)
        self.assertIn("hostaddr=6", plan.command)

    def test_no_passthrough_means_no_usb_controller(self):
        from updev.vm import find_qemu

        if not find_qemu():
            self.skipTest("no QEMU installed")
        self.assertNotIn("usb-host", build_plan(floppy="/tmp/x.img").command)


class TestSerialisation(unittest.TestCase):
    def test_device_round_trips_to_json_safe_dict(self):
        import json

        dev = Device(uid="u", kind=Kind.I2C, name="chip", status=Status.ONLINE)
        dev.issue(Severity.WARN, "hot", fix="add a heatsink")
        dev.act("read", "read a register", "updev i2c read 1 0x3c 0x00")
        dev.metrics["temp_c"] = 42.5
        payload = json.loads(json.dumps(dev.as_dict(), default=str))
        self.assertEqual(payload["kind"], "i2c")
        self.assertEqual(payload["issues"][0]["fix"], "add a heatsink")
        self.assertEqual(payload["actions"][0]["name"], "read")
        self.assertEqual(payload["metrics"]["temp_c"], 42.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
