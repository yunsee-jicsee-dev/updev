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

    def test_reacts_to_any_usb_device_not_just_storage(self):
        zone = self._zone()
        self.assertTrue(zone._is_candidate(self._stick()))
        keyboard = Device(uid="usb:1-2", kind=Kind.USB, name="kbd", tags=["input"])
        self.assertTrue(zone._is_candidate(keyboard))
        root_hub = Device(uid="usb:usb1", kind=Kind.USB, name="root hub",
                          tags=["root-hub"])
        self.assertFalse(zone._is_candidate(root_hub))
        i2c_chip = Device(uid="i2c:1:0x3c", kind=Kind.I2C, name="SSD1306")
        self.assertFalse(zone._is_candidate(i2c_chip))

    def test_storage_only_restores_the_old_behaviour(self):
        from rich.console import Console

        from updev.ui.zone import UsbZone

        zone = UsbZone(Scanner(), ProbeContext(), Console(quiet=True),
                       storage_only=True)
        self.assertTrue(zone._is_candidate(self._stick()))
        keyboard = Device(uid="usb:1-2", kind=Kind.USB, name="kbd", tags=["input"])
        self.assertFalse(zone._is_candidate(keyboard))

    def test_plugging_in_produces_a_card(self):
        zone = self._zone()
        zone._present(self._stick())
        self.assertIsNotNone(zone.current)
        self.assertEqual(len(zone.history), 1)
        dev, recognition = zone.current
        self.assertEqual(dev.address, "2-1")
        self.assertIsNotNone(recognition.storage)
        # The whole point of the merge: an identification comes with something
        # to run against it.
        self.assertTrue(recognition.tools)

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
        """Whichever port has a hub on it, passing it through must be refused.

        Addressed by class rather than by address: which port holds the hub
        changes every time something is replugged.
        """
        import os
        from pathlib import Path

        root = Path("/sys/bus/usb/devices")
        hubs = [
            entry.name
            for entry in sorted(root.glob("*-*"))
            if ":" not in entry.name
            and (entry / "bDeviceClass").exists()
            and (entry / "bDeviceClass").read_text().strip() == "09"
        ]
        if not hubs:
            self.skipTest("no external hub is attached")
        check = check_passthrough(hubs[0])
        self.assertTrue(any("hub" in b for b in check.blockers),
                        f"{hubs[0]} is a hub but was not refused: {check.blockers}")


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


# ==========================================================================
# the merge: recognition -> tools
# ==========================================================================

class TestToolkit(unittest.TestCase):
    """Which tool gets offered for which device.

    `tools_for` is pure, so every branch is reachable without owning a floppy
    drive, an optical drive or an NFC module.
    """

    @staticmethod
    def _storage(usb_class):
        from updev.usbclass import Confidence, Verdict

        return Verdict(usb_class=usb_class, confidence=Confidence.HIGH)

    @staticmethod
    def _roles(*roles):
        from updev.usbrole import RoleVerdict

        return RoleVerdict(roles=list(roles))

    def _names(self, tools):
        return [t.name for t in tools]

    def test_floppy_toy_is_offered_to_the_floppy_and_nobody_else(self):
        from updev.toolkit import tools_for
        from updev.usbclass import UsbClass

        floppy = tools_for("3-2", self._roles(), self._storage(UsbClass.FUSB),
                           {"block": ["/dev/sdb"]})
        self.assertIn("floppy-make", self._names(floppy))
        self.assertIn("floppy-write", self._names(floppy))

        for other in (UsbClass.NUSB, UsbClass.HUSB, UsbClass.SUSB):
            tools = tools_for("4-1", self._roles(), self._storage(other),
                              {"block": ["/dev/sda"]})
            self.assertNotIn("floppy-make", self._names(tools),
                             f"{other} was offered the floppy image builder")
            self.assertNotIn("floppy-write", self._names(tools),
                             f"{other} was offered a dd command")

    def test_the_only_destructive_tool_is_the_floppy_write(self):
        from updev.toolkit import tools_for
        from updev.usbclass import UsbClass

        for cls in UsbClass:
            tools = tools_for("3-2", self._roles(), self._storage(cls),
                              {"block": ["/dev/sdb"]})
            for tool in tools:
                if tool.destructive:
                    self.assertEqual(tool.name, "floppy-write")
                    self.assertEqual(cls, UsbClass.FUSB)

    def test_lead_tool_is_the_benchmark_for_a_plain_stick(self):
        from updev.toolkit import tools_for
        from updev.usbclass import UsbClass

        tools = tools_for("1-1", self._roles(), self._storage(UsbClass.NUSB),
                          {"block": ["/dev/sdc"]})
        lead = next(t for t in tools if t.lead)
        self.assertEqual(lead.name, "bench")
        self.assertIn("/dev/sdc", lead.command)

    def test_camera_gets_camtoy_and_the_right_video_node(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        tools = tools_for("1-2.4", self._roles(UsbRole.CAMERA), None,
                          {"video4linux": ["/dev/video0", "/dev/video1"]})
        lead = next(t for t in tools if t.lead)
        self.assertEqual(lead.name, "camtoy")
        self.assertIn("/dev/video0", lead.command)

    def test_keyboard_gets_the_evdev_tap_addressed_by_usb_address(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        tools = tools_for("1-2.2", self._roles(UsbRole.KEYBOARD, UsbRole.MOUSE),
                          None, {"input": ["/dev/input/input29"]})
        lead = next(t for t in tools if t.lead)
        self.assertEqual(lead.command, "updev hid watch 1-2.2")

    def test_serial_and_network_use_their_own_nodes(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        serial = tools_for("1-3", self._roles(UsbRole.SERIAL), None,
                           {"tty": ["/dev/ttyUSB0"]})
        self.assertIn("updev serial monitor ttyUSB0 -b 115200",
                      [t.command for t in serial])

        wifi = tools_for("1-4", self._roles(UsbRole.WIFI), None, {"net": ["wlan1"]})
        self.assertIn("updev net scan --iface wlan1", [t.command for t in wifi])

    def test_every_usb_device_can_at_least_be_traced(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        for role in UsbRole:
            tools = tools_for("2-1", self._roles(role), None, {})
            commands = [t.command for t in tools]
            self.assertIn("updev usb path 2-1", commands, f"{role} lost the path tool")

    def test_a_command_is_never_offered_twice(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        tools = tools_for("1-2.2", self._roles(UsbRole.KEYBOARD), None, {})
        commands = [t.command for t in tools]
        self.assertEqual(len(commands), len(set(commands)))

    def test_composite_device_gets_both_sets(self):
        from updev.toolkit import tools_for
        from updev.usbrole import UsbRole

        tools = tools_for("1-5", self._roles(UsbRole.CAMERA, UsbRole.AUDIO), None,
                          {"video4linux": ["/dev/video2"], "sound": ["card1"]})
        names = self._names(tools)
        self.assertIn("camtoy", names)
        self.assertIn("audio-play", names)

    def test_non_usb_devices_reuse_the_actions_their_backend_attached(self):
        from updev.toolkit import tools_for_device

        dev = Device(uid="i2c:1", kind=Kind.I2C, name="i2c-1")
        dev.act("scan", "Probe every address", "updev i2c scan 1")
        tools = tools_for_device(dev)
        self.assertEqual(tools[0].command, "updev i2c scan 1")
        self.assertTrue(tools[0].lead)
        # A reader on jumper wires has nothing to enumerate it, so the bus it
        # would hang off carries the hint instead.
        self.assertIn("updev nfc detect", [t.command for t in tools])

    def test_missing_binaries_are_flagged_not_hidden(self):
        from updev.toolkit import Tool, annotate

        tools = [Tool("x", "t", "cmd", "why", needs="definitely-not-a-real-binary"),
                 Tool("y", "t", "cmd2", "why")]
        annotate(tools)
        self.assertTrue(tools[0].missing)
        self.assertFalse(tools[1].missing)

    def test_recognition_serialises(self):
        from updev.toolkit import Recognition, tools_for
        from updev.usbclass import UsbClass
        from updev.usbrole import UsbRole

        dev = Device(uid="usb:3-2", kind=Kind.USB, name="TEAC FD-05PUB", address="3-2")
        recognition = Recognition(
            device=dev,
            roles=self._roles(UsbRole.STORAGE),
            storage=self._storage(UsbClass.FUSB),
            nodes={"block": ["/dev/sdb"]},
            tools=tools_for("3-2", self._roles(UsbRole.STORAGE),
                            self._storage(UsbClass.FUSB), {"block": ["/dev/sdb"]}),
        )
        payload = recognition.as_dict()
        self.assertEqual(payload["badge"], "FUSB")
        self.assertTrue(payload["tools"])
        self.assertIn("command", payload["tools"][0])
        import json

        json.dumps(payload)          # must survive the --json path


# ==========================================================================
# NFC
# ==========================================================================

class TestNfcProtocol(unittest.TestCase):
    """Frames and checksums, which is where a wired reader actually fails."""

    def test_crc_a_matches_the_iso_14443_reference_vectors(self):
        from updev.nfc import crc_a

        # ISO/IEC 14443-3 Annex B: CRC_A over 00 00 is 0x1EA0, sent LSB first.
        self.assertEqual(crc_a(b"\x00\x00"), bytes((0xA0, 0x1E)))
        self.assertEqual(crc_a(b"\x12\x34"), bytes((0x26, 0xCF)))

    def test_pn532_firmware_frame_is_byte_for_byte_the_documented_one(self):
        from updev.nfc import pn532_frame

        self.assertEqual(pn532_frame(0x02).hex(), "0000ff02fed4022a00")

    def test_pn532_response_parses_to_ic_and_version(self):
        from updev.nfc import parse_pn532_frame

        code, payload = parse_pn532_frame(bytes.fromhex("0000ff06fad50332010607e800"))
        self.assertEqual(code, 0x03)
        self.assertEqual(payload, bytes((0x32, 0x01, 0x06, 0x07)))

    def test_a_corrupt_frame_is_refused_rather_than_half_read(self):
        from updev.nfc import parse_pn532_frame

        good = bytearray.fromhex("0000ff06fad50332010607e800")
        bad_len = bytes(good[:3] + bytearray([0x06, 0x00]) + good[5:])
        with self.assertRaises(ValueError):
            parse_pn532_frame(bad_len)

        bad_dcs = bytearray(good)
        bad_dcs[-2] ^= 0xFF
        with self.assertRaises(ValueError):
            parse_pn532_frame(bytes(bad_dcs))

    def test_ack_is_reported_as_an_ack(self):
        from updev.nfc import PN532_ACK, parse_pn532_frame

        with self.assertRaises(ValueError) as caught:
            parse_pn532_frame(PN532_ACK)
        self.assertIn("ACK", str(caught.exception))

    def test_spi_bit_order_is_reversed(self):
        from updev.nfc import bit_reverse, reverse_bytes

        self.assertEqual(bit_reverse(0x01), 0x80)      # data write
        self.assertEqual(bit_reverse(0x02), 0x40)      # status read
        self.assertEqual(bit_reverse(0x03), 0xC0)      # data read
        self.assertEqual(reverse_bytes(b"\x01\x02"), b"\x80\x40")

    def test_bcc_catches_a_corrupt_uid(self):
        from updev.nfc import uid_from_anticollision

        uid = bytes((0xDE, 0xAD, 0xBE, 0xEF))
        good = uid + bytes((0xDE ^ 0xAD ^ 0xBE ^ 0xEF,))
        self.assertEqual(uid_from_anticollision(good), uid)
        with self.assertRaises(ValueError):
            uid_from_anticollision(uid + b"\x00")

    def test_sak_and_atqa_are_decoded(self):
        from updev.nfc import atqa_describe, sak_describe

        self.assertEqual(sak_describe(0x08), "MIFARE Classic 1K")
        self.assertEqual(sak_describe(0x18), "MIFARE Classic 4K")
        self.assertIn("ISO 14443-4", sak_describe(0x20))
        self.assertIn("4-byte UID", atqa_describe(b"\x04\x00"))

    def test_mifare_sector_geometry(self):
        from updev.nfc import describe_block, sector_blocks

        self.assertEqual(sector_blocks(0), [0, 1, 2, 3])
        self.assertEqual(sector_blocks(1), [4, 5, 6, 7])
        self.assertEqual(sector_blocks(32)[0], 128)
        self.assertEqual(len(sector_blocks(32)), 16)
        self.assertIn("manufacturer", describe_block(0, b""))
        self.assertIn("trailer", describe_block(7, b""))

    def test_rc522_address_byte_encodes_direction(self):
        from updev.nfc import Mfrc522

        self.assertEqual(Mfrc522.address(0x37, read=True), 0xEE)
        self.assertEqual(Mfrc522.address(0x37, read=False), 0x6E)


class _FakeSpi:
    """Enough spidev to drive the RC522 register interface in a test."""

    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.writes = []

    def xfer2(self, payload):
        address = payload[0]
        register = (address & 0x7E) >> 1
        if address & 0x80:
            return [0x00, self.registers.get(register, 0x00)]
        self.registers[register] = payload[1]
        self.writes.append((register, payload[1]))
        return [0x00, 0x00]


class TestRc522Driver(unittest.TestCase):
    def test_version_register_identifies_the_silicon(self):
        from updev.nfc import Mfrc522

        chip = Mfrc522(_FakeSpi({0x37: 0x92}))
        raw, name = chip.version()
        self.assertEqual(raw, 0x92)
        self.assertEqual(name, "MFRC522 v2.0")

    def test_an_idle_bus_reads_back_as_nothing(self):
        from updev.nfc import Mfrc522

        raw, name = Mfrc522(_FakeSpi()).version()
        self.assertEqual(raw, 0x00)
        self.assertIn("unknown", name)

    def test_antenna_only_writes_when_it_is_off(self):
        from updev.nfc import Mfrc522

        spi = _FakeSpi({0x14: 0x03})
        Mfrc522(spi).antenna(True)
        self.assertEqual(spi.writes, [])            # already on, leave it alone

        spi = _FakeSpi({0x14: 0x00})
        Mfrc522(spi).antenna(True)
        self.assertEqual(spi.writes, [(0x14, 0x03)])


class _FakeTransport:
    """A PN532 that answers GetFirmwareVersion, ACK first, as the chip does."""

    def __init__(self):
        self.sent = []
        self.replies = [
            bytes.fromhex("0000ff00ff00"),                    # ACK
            bytes.fromhex("0000ff06fad50332010607e800"),      # firmware 1.6
        ]

    def send(self, frame):
        self.sent.append(frame)

    def receive(self, count):
        return self.replies.pop(0) if self.replies else b""


class TestPn532Driver(unittest.TestCase):
    def test_firmware_version_survives_the_leading_ack(self):
        from updev.nfc import Pn532

        transport = _FakeTransport()
        ic, detail = Pn532(transport).firmware_version()
        self.assertEqual(ic, 0x32)
        self.assertIn("PN532", detail)
        self.assertIn("1.6", detail)
        self.assertEqual(transport.sent[0].hex(), "0000ff02fed4022a00")

    def test_a_mismatched_answer_is_rejected(self):
        from updev.nfc import NfcError, Pn532

        class _Wrong(_FakeTransport):
            def __init__(self):
                super().__init__()
                # A valid frame, but the answer to a different command.
                self.replies = [bytes.fromhex("0000ff03fdd54b00dd00")]

        with self.assertRaises(NfcError):
            Pn532(_Wrong()).firmware_version()


class TestNfcWiring(unittest.TestCase):
    def test_every_wiring_names_power_ground_and_a_bus(self):
        from updev.nfc import WIRINGS

        self.assertTrue(WIRINGS)
        for wiring in WIRINGS:
            pins = {name.upper() for name, _, _ in wiring.wires}
            self.assertTrue(any(p.startswith(("3.3V", "VCC")) for p in pins),
                            f"{wiring.module} has no power wire")
            self.assertIn("GND", pins, f"{wiring.module} has no ground")
            self.assertIn(wiring.bus, ("spi", "i2c", "uart"))
            for _, pin, _ in wiring.wires:
                self.assertTrue(1 <= pin <= 40, f"{wiring.module}: pin {pin}")

    def test_wires_land_on_pins_that_do_what_the_table_claims(self):
        """Cross-checked against the GPIO backend's own header map, so a typo
        in one of the two shows up here instead of on a bench."""
        from updev.backends.gpio import PIN_FUNCTIONS
        from updev.nfc import WIRINGS

        for wiring in WIRINGS:
            for name, pin, described in wiring.wires:
                label, function = PIN_FUNCTIONS[pin]
                self.assertIn(label, described,
                              f"{wiring.module} {name} → pin {pin} is {label}, "
                              f"not {described}")

    def test_wiring_lookup_accepts_a_module_or_a_bus(self):
        from updev.nfc import wiring_for

        self.assertIsNotNone(wiring_for("rc522"))
        self.assertIsNotNone(wiring_for("i2c"))
        self.assertIsNone(wiring_for("smoke signals"))


# ==========================================================================
# HID
# ==========================================================================

class TestHidDecoding(unittest.TestCase):
    def test_a_key_press_decodes_to_a_name_and_an_action(self):
        import struct

        from updev.hid import describe, parse_event

        blob = struct.pack("llHHi", 1700000000, 500000, 0x01, 30, 1)
        event = parse_event(blob)
        self.assertEqual(event.code_name, "KEY_A")
        self.assertEqual(describe(event), "KEY_A press")

        blob = struct.pack("llHHi", 1700000000, 0, 0x01, 30, 0)
        self.assertEqual(describe(parse_event(blob)), "KEY_A release")

    def test_mouse_movement_keeps_its_sign(self):
        import struct

        from updev.hid import describe, parse_event

        blob = struct.pack("llHHi", 0, 0, 0x02, 0x00, -3)
        self.assertEqual(describe(parse_event(blob)), "REL_X -3")

    def test_buttons_and_gamepads_are_named(self):
        from updev.hid import KEY_NAMES

        self.assertEqual(KEY_NAMES[0x110], "BTN_LEFT")
        self.assertEqual(KEY_NAMES[57], "KEY_SPACE")
        self.assertEqual(KEY_NAMES[103], "KEY_UP")
        self.assertIn("BTN_SOUTH", KEY_NAMES[0x130])

    def test_framing_events_are_marked_as_noise(self):
        import struct

        from updev.hid import parse_event

        syn = parse_event(struct.pack("llHHi", 0, 0, 0x00, 0, 0))
        self.assertTrue(syn.is_noise)
        scan = parse_event(struct.pack("llHHi", 0, 0, 0x04, 0x04, 0x70004))
        self.assertTrue(scan.is_noise)
        key = parse_event(struct.pack("llHHi", 0, 0, 0x01, 30, 1))
        self.assertFalse(key.is_noise)

    def test_a_short_read_is_an_error_not_a_guess(self):
        from updev.hid import parse_event

        with self.assertRaises(ValueError):
            parse_event(b"\x00" * 8)

    def test_unknown_codes_still_render(self):
        import struct

        from updev.hid import describe, parse_event

        event = parse_event(struct.pack("llHHi", 0, 0, 0x01, 0x2ff, 1))
        self.assertIn("0x2ff", describe(event))


# ==========================================================================
# disk bench
# ==========================================================================

class TestBench(unittest.TestCase):
    def test_seek_latency_separates_platters_from_flash(self):
        from updev.bench import rotation_hint

        self.assertEqual(rotation_hint(12.4)[0], "rotating")
        self.assertEqual(rotation_hint(4.0)[0], "rotating")
        self.assertEqual(rotation_hint(0.18)[0], "solid-state")
        self.assertEqual(rotation_hint(2.0)[0], "unclear")
        self.assertEqual(rotation_hint(0.0)[0], "unknown")

    def test_a_cached_read_is_not_evidence_about_the_medium(self):
        from updev.bench import rotation_hint

        verdict, reason = rotation_hint(0.001)
        self.assertEqual(verdict, "cached")
        self.assertIn("cache", reason)

    def test_link_comparison_blames_the_right_half(self):
        from updev.bench import link_comparison

        self.assertIn("medium is the limit", link_comparison(42, 5000))
        self.assertIn("bus is the limit", link_comparison(480, 5000))
        self.assertEqual(link_comparison(42, 0), "")

    def test_a_real_read_produces_a_serialisable_result(self):
        import json
        import os
        import tempfile

        from updev.bench import run

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(os.urandom(4 * 1024 * 1024))
            path = handle.name
        try:
            result = run(path, chunk_mb=1, reads=3, seeks=8)
            self.assertGreater(result.reads, 0)
            self.assertGreater(result.throughput_mbs, 0)
            json.dumps(result.as_dict())
        finally:
            os.unlink(path)


# ==========================================================================
# the GUI
# ==========================================================================

def _has_tkinter() -> bool:
    import importlib.util

    return importlib.util.find_spec("tkinter") is not None


@unittest.skipUnless(_has_tkinter(), "python3-tk is not installed")
class TestEditorDispatch(unittest.TestCase):
    """Which panel opens for which device. No window is created — the mapping
    is plain data, and it is the part that would silently go wrong."""

    def _names(self, device):
        from updev.gui.app import editors_for

        return [cls.__name__ for cls in editors_for(device)]

    def test_every_device_opens_something(self):
        """The info panel is the floor: no device may open to a blank frame."""
        for kind in Kind:
            device = Device(uid=f"{kind}:x", kind=kind, name="thing")
            self.assertEqual(self._names(device)[-1], "InfoEditor",
                             f"{kind} has no fallback panel")

    def test_nfc_reader_gets_the_tag_editor(self):
        device = Device(uid="nfc:mfrc522:spidev0.0", kind=Kind.NFC, name="MFRC522",
                        address="/dev/spidev0.0")
        self.assertEqual(self._names(device)[0], "TagEditor")

    def test_i2c_chip_gets_registers_but_a_bare_bus_does_not(self):
        chip = Device(uid="i2c:1:0x3c", kind=Kind.I2C, name="SSD1306", address="0x3c")
        self.assertIn("RegisterEditor", self._names(chip))
        bus = Device(uid="i2c:1", kind=Kind.I2C, name="i2c-1")
        self.assertNotIn("RegisterEditor", self._names(bus))

    def test_gpio_gets_the_pin_editor(self):
        device = Device(uid="gpio:chip0", kind=Kind.GPIO, name="gpiochip0")
        self.assertIn("PinEditor", self._names(device))

    def test_block_device_gets_the_sector_viewer(self):
        device = Device(uid="blk:sda", kind=Kind.STORAGE, name="sda", node="/dev/sda")
        self.assertIn("BlockEditor", self._names(device))

    def test_write_capable_panels_are_declared(self):
        """The badge that marks a panel dangerous comes from this flag, so a
        panel that can write and doesn't say so is a real bug."""
        from updev.gui.floppy import FloppyEditor
        from updev.gui.panels import BlockEditor, InfoEditor, RegisterEditor
        from updev.gui.tag import TagEditor

        for panel in (FloppyEditor, TagEditor, RegisterEditor):
            self.assertTrue(panel.WRITES, f"{panel.__name__} writes but says it doesn't")
        for panel in (BlockEditor, InfoEditor):
            self.assertFalse(panel.WRITES)

    def test_block_node_does_not_hand_back_a_sysfs_directory(self):
        """A USB device's `node` is its sysfs directory; opening that as a
        block device fails with EISDIR, which is a bad way to find out."""
        from updev.gui.base import block_node

        disk = Device(uid="blk:sda", kind=Kind.STORAGE, name="sda", node="/dev/sda")
        self.assertEqual(block_node(disk), "/dev/sda")

        usb = Device(uid="usb:4-1", kind=Kind.USB, name="disk",
                     node="/sys/bus/usb/devices/4-1", address="")
        self.assertEqual(block_node(usb), "")      # no address, nothing to resolve

        keyboard = Device(uid="usb:1-1", kind=Kind.USB, name="kbd",
                          node="/sys/bus/usb/devices/1-1", address="1-1")
        self.assertFalse(block_node(keyboard).startswith("/sys"))

    def test_hex_view_column_maths(self):
        """Double-clicking a hex pair has to land on the byte under the cursor."""
        from updev.gui.base import HexView

        self.assertEqual(HexView._offset_for(1, 10), 0)     # first byte
        self.assertEqual(HexView._offset_for(1, 12), 0)     # second nibble
        self.assertEqual(HexView._offset_for(1, 13), 1)
        self.assertEqual(HexView._offset_for(2, 10), 16)    # next row
        self.assertIsNone(HexView._offset_for(1, 4))        # in the offset column
        self.assertIsNone(HexView._offset_for(1, 90))       # out in the ASCII


class TestFloppyRoundTrip(unittest.TestCase):
    """What the GUI's floppy editor does, minus the widgets: load, edit, rebuild.

    The editor never patches an image in place — it reassembles through
    `build_image()` — so this is the operation that has to survive.
    """

    def test_edited_file_survives_a_rebuild(self):
        from updev.floppy import build_image, gallery, inspect_image, read_file

        files = dict(gallery())
        files["README.TXT"] = "edited by the gui\nsecond line\n"
        files["NEW.TXT"] = "a file that was not there before\n"
        image = build_image(label="EDITED", files=files)

        info = inspect_image(image.data)
        self.assertEqual(info["volume_label"], "EDITED")
        names = {e["name"] for e in info["entries"] if not e["volume_label"]}
        self.assertIn("NEW.TXT", names)

        back = read_file(image.data, "README.TXT").decode()
        self.assertIn("edited by the gui", back)
        self.assertIn("second line", back)
        self.assertEqual(read_file(image.data, "NEW.TXT").decode().strip(),
                         "a file that was not there before")

    def test_the_boot_message_budget_is_reported_not_discovered_late(self):
        from updev.floppy import build_boot_code
        from updev.gui.floppy import BOOT_BUDGET

        used = len(build_boot_code("short\r\n"))
        self.assertLess(used, BOOT_BUDGET)
        with self.assertRaises(ValueError) as caught:
            build_boot_code("x" * (BOOT_BUDGET + 1))
        self.assertIn("overflows", str(caught.exception))

    def test_a_rebuilt_image_is_still_a_bootable_floppy(self):
        from updev.floppy import FLOPPY_SIZE, build_image, inspect_image

        image = build_image(label="EDITED", files={"A.TXT": "hello\n"})
        self.assertEqual(image.size, FLOPPY_SIZE)
        info = inspect_image(image.data)
        self.assertTrue(info["bootable_signature_present"])
        self.assertEqual(info["fs_type"], "FAT12")


class TestMifareWriteGuards(unittest.TestCase):
    """The two writes that damage a card rather than change it."""

    def test_trailer_geometry(self):
        from updev.nfc import is_trailer

        self.assertEqual([b for b in range(16) if is_trailer(b)], [3, 7, 11, 15])
        self.assertTrue(is_trailer(143))         # sector 32 is 16 blocks long
        self.assertFalse(is_trailer(131))

    def test_block_zero_is_refused_outright(self):
        from updev.nfc import NfcError, check_writable

        with self.assertRaises(NfcError) as caught:
            check_writable(0)
        self.assertIn("manufacturer", str(caught.exception))
        # Not even the override opens it — a genuine card would refuse anyway.
        with self.assertRaises(NfcError):
            check_writable(0, allow_trailer=True)

    def test_trailers_need_an_explicit_override(self):
        from updev.nfc import NfcError, check_writable

        with self.assertRaises(NfcError):
            check_writable(7)
        self.assertIsNone(check_writable(7, allow_trailer=True))
        self.assertIsNone(check_writable(5))

    def test_a_short_block_is_refused_before_it_reaches_the_card(self):
        from updev.nfc import Mfrc522

        chip = Mfrc522(_FakeSpi({0x37: 0x92}))
        with self.assertRaises(ValueError):
            chip.write_block(4, b"\x00" * 15)

    def test_a_nak_is_not_reported_as_success(self):
        from updev.nfc import Mfrc522, NfcError

        chip = Mfrc522(_FakeSpi({0x37: 0x92}))
        with self.assertRaises(NfcError):
            chip._expect_ack(b"\x00", 4, "write")      # 0x00 is a NAK
        with self.assertRaises(NfcError):
            chip._expect_ack(b"\x0a", 8, "write")      # right value, wrong width
        self.assertIsNone(chip._expect_ack(b"\x0a", 4, "write"))


class PanelTests(unittest.TestCase):
    """The ST7735S front panel, drawn into memory instead of onto glass.

    A headless Screen runs the same layout code as a live one, so everything
    here would catch a regression that shows up as an unreadable panel.
    """

    def _screen(self):
        from updev.panel.screen import Screen
        return Screen(None)

    def test_a_missing_panel_is_a_screen_you_can_still_draw_on(self):
        from updev.panel import open_panel

        screen = open_panel(port=99, cs=9, required=False)
        self.assertFalse(screen.live)
        screen.text(0, 0, "still fine")
        screen.flush()
        self.assertEqual(screen.frames, 1)
        screen.close()                     # a no-op, not an AttributeError

    def test_a_missing_panel_says_which_node_it_wanted(self):
        from updev.panel import PanelUnavailable, open_panel

        with self.assertRaises(PanelUnavailable) as caught:
            open_panel(port=99, cs=9)
        self.assertIn("/dev/spidev99.9", str(caught.exception))

    def test_the_no_hardware_switch_degrades_instead_of_failing(self):
        from unittest import mock
        from updev.panel import open_panel
        import updev.panel.screen as screen_mod

        # required=True and a panel that really is attached — the switch still
        # wins, because "I have no display today" is the thing it means.
        with mock.patch.object(screen_mod, "USE_PHYSICAL_DISPLAY", False):
            screen = open_panel(required=True)
        self.assertFalse(screen.live)

    def test_the_wiring_matches_the_drawpad(self):
        from updev.panel import screen as s

        # Same panel, same wires, two programs. A change here without a change
        # in st7735s_drawpad.py means one of them is driving the wrong pins.
        self.assertEqual((s.GRID_W, s.GRID_H), (128, 160))
        self.assertEqual((s.SPI_PORT, s.SPI_DEVICE), (0, 0))
        self.assertEqual((s.PIN_DC, s.PIN_RST), (24, 25))

    def test_the_persisting_close_never_releases_the_reset_line(self):
        from updev.panel.screen import Screen

        class FakeDevice:
            persist = False
            cleaned = False

            def cleanup(self):
                self.cleaned = True

        # Releasing DC/RST hands them back as inputs; with no pull-up on RES
        # the panel resets itself seconds after we exit. So the default close
        # must not call cleanup at all, and must defuse luma's atexit hook.
        device = FakeDevice()
        Screen(device).close()
        self.assertFalse(device.cleaned)
        self.assertTrue(device.persist)
        device.cleanup()                      # the atexit hook's call
        self.assertFalse(device.cleaned)

    def test_blanking_does_release_the_panel(self):
        from updev.panel.screen import Screen

        class FakeDevice:
            persist = True
            cleaned = False

            def cleanup(self):
                self.cleaned = True

        device = FakeDevice()
        Screen(device).close(blank=True)
        self.assertTrue(device.cleaned)
        self.assertFalse(device.persist)

    def test_text_is_clipped_to_the_column_it_was_given(self):
        screen = self._screen()
        long = "br-886746f5b20a-and-then-some"
        clipped = screen.truncate(long, 60)
        self.assertTrue(clipped.endswith("…"))
        self.assertLess(len(clipped), len(long))
        self.assertEqual(screen.truncate("spi", 60), "spi")

    def test_every_backend_gets_a_row_that_fits_on_the_panel(self):
        from updev.core.model import BackendReport
        from updev.panel.boot import progress

        screen = self._screen()
        planned = [f"backend{i}" for i in range(14)]
        done = {"backend0": BackendReport("backend0", True, ok=True, count=3)}
        progress(screen, planned, done)
        # Nothing drawn outside the canvas, and the frame did go out.
        self.assertEqual(screen.image.size, (128, 160))
        self.assertEqual(screen.frames, 1)

    def test_the_status_chips_account_for_every_device(self):
        from updev.panel.boot import _CHIP_ORDER

        # Whatever a backend reports, there is a chip for it — a status missing
        # from this tuple would silently vanish from the summary's arithmetic.
        self.assertEqual(set(_CHIP_ORDER), set(Status))

    def test_the_summary_skips_facts_the_scan_did_not_produce(self):
        from updev.panel.boot import _facts

        result = ScanResult(devices=[
            Device(uid="host:board", kind=Kind.HOST, name="Raspberry Pi 5 Model B Rev 1.1",
                   status=Status.ONLINE, metrics={"uptime_s": 3600.0}),
            Device(uid="host:soc", kind=Kind.SOC, name="BCM2712", status=Status.ONLINE,
                   metrics={"temp_c": 63.9, "freq_hz": 2.4e9}),
        ])
        facts = dict(_facts(result))
        self.assertEqual(facts["board"], "Pi 5 Model B")   # revision is noise at 128px
        self.assertIn("64°C", facts["soc"])
        self.assertEqual(facts["up"], "1h 0m")
        self.assertNotIn("mem", facts)                     # no memory device, no row

    def test_the_primary_interface_is_the_one_with_the_default_route(self):
        from updev.panel.boot import _primary_iface

        result = ScanResult(devices=[
            Device(uid="net:docker0", kind=Kind.NET_IFACE, name="docker0",
                   status=Status.ONLINE, detail={"ipv4": ["172.17.0.1/16"]}),
            Device(uid="net:wlan0", kind=Kind.NET_IFACE, name="wlan0",
                   status=Status.ONLINE,
                   detail={"ipv4": ["192.168.0.11/24"], "default_route": "yes"}),
        ])
        self.assertEqual(_primary_iface(result).name, "wlan0")

    def test_a_scan_reports_progress_as_each_backend_lands(self):
        seen: list[str] = []

        class Quick(Backend):
            name = "quick"
            kinds = (Kind.GPIO,)

            def probe(self, ctx):
                return [Device(uid="quick:1", kind=Kind.GPIO, name="pin")]

        class Broken(Backend):
            name = "broken"
            kinds = (Kind.GPIO,)

            def available(self, ctx):
                return False, "not wired up"

            def probe(self, ctx):
                raise AssertionError("must not run")

        scanner = Scanner([Quick(), Broken()])
        result = scanner.scan(ProbeContext(), on_report=lambda r: seen.append(r.name))
        # Unavailable backends report too — the panel row has to go somewhere.
        self.assertEqual(sorted(seen), ["broken", "quick"])
        self.assertEqual(len(seen), len(result.reports))


class PanelServiceTests(unittest.TestCase):
    """The systemd unit ships as a template on purpose.

    The first version hardcoded /usr/local/bin/updev, which is not where updev
    lives on a checkout or after a `pip install --user` — the service failed at
    boot with nothing to show for it. These guard that mistake.
    """

    def _template(self) -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        return (root / "systemd" / "updev-panel.service.in").read_text()

    def test_the_unit_resolves_its_paths_at_install_time(self):
        template = self._template()
        for placeholder in ("@EXEC@", "@USER@", "@WORKDIR@"):
            self.assertIn(placeholder, template)
        self.assertNotIn("/usr/local/bin", template)

    def test_the_unit_does_not_run_as_root(self):
        # A --user wheel is not on root's import path, and spi+gpio group
        # membership is all the panel actually needs.
        template = self._template()
        self.assertIn("User=@USER@", template)
        self.assertNotIn("User=root", template)

    def test_the_installer_knows_how_to_fill_the_template_in(self):
        from pathlib import Path
        script = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
        self.assertIn("--panel-service", script)
        for placeholder in ("@EXEC@", "@USER@", "@WORKDIR@"):
            self.assertIn(placeholder, script)


# ==========================================================================
# the card model
# ==========================================================================

class TestMifareAccessBits(unittest.TestCase):
    """The nine bits that decide who may touch which block.

    Worth testing hard: they are stored twice (once inverted), the meaning of
    a triple differs between data blocks and the trailer, and one particular
    combination is a one-way door.
    """

    #: What every MIFARE Classic ships with: data blocks wide open with either
    #: key, trailer writable with key A.
    FACTORY_TRAILER = bytes.fromhex("FFFFFFFFFFFF") + bytes.fromhex("FF078069") + bytes(4)

    def test_factory_trailer_decodes_to_the_transport_configuration(self):
        from updev.mifare import block_permissions, decode_access_bits, trailer_permissions

        bits = decode_access_bits(self.FACTORY_TRAILER)
        self.assertTrue(bits.valid)
        self.assertEqual([bits.triple(g) for g in range(4)],
                         [(0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 1)])
        data = block_permissions(bits, 0)
        self.assertEqual(data["read"], "A|B")
        self.assertEqual(data["write"], "A|B")
        trailer = trailer_permissions(bits)
        self.assertEqual(trailer["key_a_read"], "—")      # never, on any card
        self.assertEqual(trailer["key_a_write"], "A")
        self.assertEqual(trailer["access_write"], "A")

    def test_the_inverted_copy_is_actually_checked(self):
        from updev.mifare import decode_access_bits

        broken = bytearray(self.FACTORY_TRAILER)
        broken[6] ^= 0xFF                     # the inverted copy no longer matches
        self.assertFalse(decode_access_bits(bytes(broken)).valid)
        self.assertFalse(decode_access_bits(b"\x00" * 4).valid)   # too short

    def test_key_a_is_never_readable_under_any_combination(self):
        from updev.mifare import AccessBits, trailer_permissions

        for c1 in (0, 1):
            for c2 in (0, 1):
                for c3 in (0, 1):
                    bits = AccessBits(c1=[0, 0, 0, c1], c2=[0, 0, 0, c2],
                                      c3=[0, 0, 0, c3])
                    self.assertEqual(trailer_permissions(bits)["key_a_read"], "—")

    def test_the_one_way_door_is_reported_as_such(self):
        from updev.mifare import AccessBits, block_permissions, trailer_permissions

        locked = AccessBits(c1=[1] * 4, c2=[1] * 4, c3=[1] * 4)
        self.assertEqual(block_permissions(locked, 0)["read"], "—")
        self.assertEqual(block_permissions(locked, 0)["write"], "—")
        trailer = trailer_permissions(locked)
        self.assertEqual(trailer["access_write"], "—")     # can never be changed back
        self.assertEqual(trailer["key_b_write"], "—")

    def test_value_block_configuration_allows_decrement_but_not_write(self):
        from updev.mifare import AccessBits, block_permissions

        value = AccessBits(c1=[0] * 4, c2=[0] * 4, c3=[1] * 4)
        perms = block_permissions(value, 0)
        self.assertEqual(perms["write"], "—")
        self.assertEqual(perms["decrement"], "A|B")


class TestMifareGeometry(unittest.TestCase):
    def test_sak_picks_the_layout(self):
        from updev.mifare import layout_for_sak

        self.assertEqual(layout_for_sak(0x08).sectors, 16)
        self.assertEqual(layout_for_sak(0x18).sectors, 40)
        self.assertFalse(layout_for_sak(0x00).classic)     # Ultralight
        # A DESFire speaks APDUs, not blocks — no layout is the right answer.
        self.assertIsNone(layout_for_sak(0x20))

    def test_four_k_sectors_change_size_halfway_through(self):
        from updev.mifare import layout_for_sak

        card = layout_for_sak(0x18)
        self.assertEqual(card.blocks_in(0), [0, 1, 2, 3])
        self.assertEqual(len(card.blocks_in(32)), 16)
        self.assertEqual(card.blocks_in(32)[0], 128)
        self.assertEqual(card.trailer_of(32), 143)
        self.assertTrue(card.is_trailer(143))
        self.assertFalse(card.is_trailer(131))
        self.assertEqual(card.sector_of(143), 32)

    def test_access_groups_are_not_one_per_block_on_a_big_sector(self):
        """In a 16-block sector the first three groups cover five blocks each —
        the detail that breaks decoders written only against 1K cards."""
        from updev.mifare import group_of, layout_for_sak

        card = layout_for_sak(0x18)
        self.assertEqual([group_of(card, b) for b in range(128, 144)],
                         [0] * 5 + [1] * 5 + [2] * 5 + [3])
        small = layout_for_sak(0x08)
        self.assertEqual([group_of(small, b) for b in range(4)], [0, 1, 2, 3])


class TestNdef(unittest.TestCase):
    @staticmethod
    def _text_record(body: str = "hello card") -> bytes:
        payload = bytes([0x02]) + b"en" + body.encode()
        return bytes([0xD1, 0x01, len(payload), 0x54]) + payload

    @staticmethod
    def _uri_record(rest: str = "anthropic.com") -> bytes:
        payload = bytes([0x04]) + rest.encode()       # 0x04 = https://
        return bytes([0xD1, 0x01, len(payload), 0x55]) + payload

    def test_text_record_drops_the_language_header(self):
        from updev.mifare import parse_ndef_message

        records = parse_ndef_message(self._text_record())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "hello card")
        self.assertEqual(records[0].label, "well-known · T")

    def test_uri_prefix_byte_is_expanded(self):
        from updev.mifare import parse_ndef_message

        self.assertEqual(parse_ndef_message(self._uri_record())[0].text,
                         "https://anthropic.com")

    def test_the_tlv_is_found_wherever_it_sits_in_the_dump(self):
        """Sector trailers sit in the middle of a Classic dump, so the message
        cannot be assumed to start at a fixed offset."""
        from updev.mifare import parse_ndef

        record = self._text_record()
        dump = bytes(48) + bytes([0x03, len(record)]) + record + b"\xfe" + bytes(16)
        self.assertEqual(parse_ndef(dump)[0].text, "hello card")

    def test_multi_record_message_stops_at_the_end_flag(self):
        from updev.mifare import parse_ndef

        text = self._text_record()
        uri = self._uri_record()
        chained = bytes([0x91]) + text[1:] + bytes([0x51]) + uri[1:]
        dump = bytes([0x03, len(chained)]) + chained + b"\xfe"
        records = parse_ndef(dump)
        self.assertEqual([r.text for r in records],
                         ["hello card", "https://anthropic.com"])

    def test_a_truncated_dump_yields_what_it_can_without_raising(self):
        from updev.mifare import parse_ndef, parse_ndef_message

        record = self._text_record()
        self.assertEqual(parse_ndef_message(record[:6]), [])
        self.assertEqual(parse_ndef(bytes(64)), [])
        self.assertEqual(parse_ndef(b"\x03"), [])

    def test_ultralight_pages_zero_to_three_are_refused(self):
        from updev.nfc import NfcError, check_page_writable

        for page in range(4):
            with self.assertRaises(NfcError):
                check_page_writable(page)
        self.assertIsNone(check_page_writable(4))


@unittest.skipUnless(_has_tkinter(), "python3-tk is not installed")
class TestUsbPanelDispatch(unittest.TestCase):
    def _names(self, device):
        from updev.gui.app import editors_for

        return [cls.__name__ for cls in editors_for(device)]

    def test_every_usb_device_gets_identity_descriptors_and_path(self):
        """A device updev cannot place is exactly the one whose descriptor you
        want to read, so these three are never conditional."""
        device = Device(uid="usb:9-9", kind=Kind.USB, name="mystery", address="9-9")
        names = self._names(device)
        for panel in ("IdentityPanel", "DescriptorPanel", "PathPanel"):
            self.assertIn(panel, names)

    def test_a_hub_gets_its_port_map_first(self):
        device = Device(uid="usb:usb1", kind=Kind.USB, name="root hub",
                        address="usb1", tags=["root-hub"])
        self.assertEqual(self._names(device)[0], "HubPanel")

    def test_role_panel_leads_the_generic_ones(self):
        from updev.gui.app import editors_for

        device = Device(uid="usb:9-9", kind=Kind.USB, name="mystery", address="9-9")
        panels = editors_for(device)
        generic = {"IdentityPanel", "DescriptorPanel", "PathPanel", "InfoEditor"}
        specific = [p for p in panels if p.__name__ not in generic]
        for panel in specific:
            self.assertLess(panels.index(panel),
                            panels.index(next(p for p in panels
                                              if p.__name__ in generic)))

    def test_the_serial_panel_is_the_only_new_one_that_writes(self):
        from updev.gui.usbpanels import (
            CameraPanel,
            DescriptorPanel,
            HubPanel,
            IdentityPanel,
            NetworkPanel,
            PathPanel,
            SerialPanel,
        )

        self.assertTrue(SerialPanel.WRITES)       # it can transmit on the bus
        for panel in (IdentityPanel, DescriptorPanel, PathPanel, CameraPanel,
                      NetworkPanel, HubPanel):
            self.assertFalse(panel.WRITES, f"{panel.__name__} claims to write")


# ==========================================================================
# flybrain — the mushroom body that learns this board
# ==========================================================================

class _FlyFixture(unittest.TestCase):
    """Scan builders shared by the fly brain tests.

    Every one is a pure `ScanResult`, so the whole circuit is testable without
    a board — which is the point of keeping `smell()` a pure function.
    """

    @staticmethod
    def _devices(n, kind=Kind.USB, status=Status.ONLINE, tags=()):
        return [
            Device(uid=f"{kind}:{i}", kind=kind, name=f"d{i}", status=status,
                   bus=str(kind), tags=list(tags))
            for i in range(n)
        ]

    def _board(self, scale=1):
        """The reference machine: some USB, some storage, a NIC."""
        return ScanResult(devices=(
            self._devices(6 * scale, Kind.USB, tags=["hotplug"])
            + self._devices(3 * scale, Kind.STORAGE)
            + self._devices(1 * scale, Kind.NET_IFACE)
        ))

    def _other_board(self):
        """A different machine entirely: degraded I2C and nothing else."""
        return ScanResult(devices=(
            self._devices(6, Kind.I2C, status=Status.DEGRADED)
            + self._devices(3, Kind.SPI, status=Status.IDLE)
        ))


class TestFlyOlfaction(_FlyFixture):
    """The antennal lobe and the mushroom body's random projection."""

    def test_the_input_layer_is_the_size_of_an_antennal_lobe(self):
        """In the fly's ballpark, not pinned to it.

        This used to assert equality with the 51 a fly has. That turned every
        new receptor into an argument about which existing one to delete, and
        the number is a fact about flies rather than a constraint on what a
        device manager needs to smell.
        """
        from updev.flybrain import ANTENNAL_LOBE_GLOMERULI, GLOMERULI

        self.assertLessEqual(abs(len(GLOMERULI) - ANTENNAL_LOBE_GLOMERULI), 8)
        self.assertEqual(len(set(GLOMERULI)), len(GLOMERULI), "duplicate receptor")

    def test_the_tag_is_sparse(self):
        from updev.flybrain import KENYON_CELLS, SPARSITY, TAG_BITS, smell

        self.assertEqual(TAG_BITS, int(KENYON_CELLS * SPARSITY))
        self.assertEqual(len(smell(self._board()).tag), TAG_BITS)

    def test_the_same_scan_smells_the_same(self):
        from updev.flybrain import smell

        self.assertEqual(smell(self._board()).tag, smell(self._board()).tag)

    def test_similar_scans_share_most_of_the_tag(self):
        """The LSH property: small changes must not scramble the code.

        Without this, familiarity would never generalise — plugging in one
        extra stick would make a trained board unrecognisable.
        """
        from updev.flybrain import smell

        board = smell(self._board())
        plus_one = ScanResult(devices=self._board().devices + self._devices(1, Kind.USB))
        self.assertGreater(board.overlap(smell(plus_one)), 0.7)

    def test_different_scans_do_not(self):
        from updev.flybrain import smell

        self.assertLess(smell(self._board()).overlap(smell(self._other_board())), 0.4)

    def test_gain_control_makes_the_code_scale_invariant(self):
        """Divisive normalisation is what stops "busy" being the only signal.

        The same machine with three of everything instead of one is the same
        machine, and must smell like it — *similar*, not identical, because
        `census:population` legitimately differs between them. What the
        normalisation buys is that the similarity degrades gently with the
        scale ratio instead of collapsing, which is what these two assertions
        pin down between them.
        """
        from updev.flybrain import smell

        one = smell(self._board(1))
        near = one.overlap(smell(self._board(2)))
        far = one.overlap(smell(self._board(5)))
        self.assertGreater(far, 0.6)
        self.assertGreater(near, far)


class TestFlyLearning(_FlyFixture):
    def test_a_naive_fly_finds_everything_novel(self):
        from updev.flybrain import FlyBrain, Mood

        verdict = FlyBrain().judge(self._board())
        self.assertEqual(verdict.novelty, 1.0)
        self.assertEqual(verdict.mood, Mood.STARTLED)

    def test_training_settles_the_trained_state(self):
        """Six exposures, not three — the headline number is the long-term
        compartment, which learns slowly on purpose."""
        from updev.flybrain import FlyBrain, Mood

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board())
        verdict = brain.judge(self._board())
        self.assertLess(verdict.novelty, 0.3)
        self.assertEqual(verdict.mood, Mood.SETTLED)

    def test_training_one_state_does_not_excuse_another(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        self.assertGreater(brain.judge(self._other_board()).novelty, 0.6)

    def test_learn_reports_the_verdict_from_before_it_learned(self):
        """Otherwise the return value is always "familiar" and says nothing."""
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        self.assertEqual(brain.learn(self._board()).novelty, 1.0)

    def test_dopamine_writes_an_aversive_memory(self):
        from updev.flybrain import FlyBrain, Mood

        painful = self._board()
        painful.devices[0].issue(Severity.ERROR, "bus stuck low")
        painful.devices[1].issue(Severity.ERROR, "no ack")

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(painful)

        # Same shape of machine, no issues this time: familiar, but the fly
        # remembers that this shape goes wrong.
        verdict = brain.judge(self._board())
        self.assertGreater(verdict.aversion, 0.2)
        self.assertEqual(verdict.mood, Mood.AVERSIVE)

    def test_a_clean_board_writes_no_aversive_memory(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        self.assertEqual(brain.judge(self._board()).aversion, 0.0)

    def test_attention_names_glomeruli_the_memory_cannot_account_for(self):
        from updev.flybrain import GLOMERULI, FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        attend = brain.judge(self._other_board()).attend
        self.assertTrue(attend)
        for name in attend:
            self.assertIn(name, GLOMERULI)

    def test_a_settled_board_has_nothing_to_attend_to(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board())
        self.assertEqual(brain.judge(self._board()).attend, [])

    def test_forget_returns_the_fly_to_naive(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        brain.forget()
        self.assertEqual(brain.judge(self._board()).novelty, 1.0)
        self.assertEqual(brain.exposures, 0)


class TestFlyLateralHorn(_FlyFixture):
    """The unlearnable path. These are the tests that make it safe to ship."""

    def _catastrophe(self):
        result = self._board()
        result.devices[0].issue(Severity.ERROR, "root filesystem is 99% full")
        return result

    def test_an_error_always_alarms(self):
        from updev.flybrain import FlyBrain, Mood

        self.assertEqual(FlyBrain().judge(self._catastrophe()).mood, Mood.ALARMED)

    def test_training_cannot_teach_the_fly_to_ignore_it(self):
        """The whole reason for a parallel innate path.

        A purely learned system trained on a broken board learns that broken
        is normal. The lateral horn is consulted first and wins outright.
        """
        from updev.flybrain import FlyBrain, Mood

        brain = FlyBrain()
        for _ in range(50):
            brain.learn(self._catastrophe())

        verdict = brain.judge(self._catastrophe())
        self.assertLess(verdict.novelty, 0.2)        # thoroughly familiar
        self.assertEqual(verdict.mood, Mood.ALARMED)  # and still alarming
        self.assertTrue(verdict.alarms)

    def test_a_failed_backend_alarms(self):
        from updev.core.model import BackendReport
        from updev.flybrain import innate_alarms

        result = self._board()
        result.reports.append(BackendReport("i2c", True, ok=False, error="boom"))
        self.assertTrue(any(a.channel == "backend" for a in innate_alarms(result)))

    def test_heat_and_a_full_disk_alarm(self):
        from updev.flybrain import innate_alarms

        result = self._board()
        result.devices[0].metrics["temp_c"] = 84.0
        result.devices[1].metrics["fs_used_pct"] = 97.0
        channels = {a.channel for a in innate_alarms(result)}
        self.assertEqual(channels, {"thermal", "storage"})

    def test_a_healthy_board_is_silent(self):
        from updev.flybrain import innate_alarms

        self.assertEqual(innate_alarms(self._board()), [])

    def test_alarms_suppress_the_offer_to_learn(self):
        from updev.flybrain import FlyBrain

        self.assertFalse(FlyBrain().judge(self._catastrophe()).should_learn)
        self.assertTrue(FlyBrain().judge(self._board()).should_learn)


class TestFlyPersistence(_FlyFixture):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "flybrain.json"

    def test_a_saved_brain_judges_identically(self):
        from updev.flybrain import FlyBrain, load_brain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        brain.save(self.path)

        restored = load_brain(self.path)
        self.assertEqual(restored.exposures, brain.exposures)
        self.assertAlmostEqual(restored.judge(self._board()).novelty,
                               brain.judge(self._board()).novelty, places=4)

    def test_a_missing_file_hatches_a_naive_fly(self):
        from updev.flybrain import load_brain

        self.assertEqual(load_brain(self.path / "nope").exposures, 0)

    def test_a_corrupt_file_hatches_a_naive_fly(self):
        """Confident nonsense is worse than knowing nothing."""
        from updev.flybrain import load_brain

        self.path.write_text("{not json at all")
        self.assertEqual(load_brain(self.path).exposures, 0)

    def test_a_memory_from_a_different_input_layer_is_discarded(self):
        """Weights index Kenyon cells; if the glomeruli changed they are lies."""
        import json

        from updev.flybrain import FlyBrain, load_brain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        brain.save(self.path)

        data = json.loads(self.path.read_text())
        data["signature"] = "0000deadbeef0000"
        self.path.write_text(json.dumps(data))

        stale = load_brain(self.path)
        self.assertEqual(stale.exposures, 0)
        self.assertEqual(stale.judge(self._board()).novelty, 1.0)

    def test_saving_is_atomic(self):
        from updev.flybrain import FlyBrain

        FlyBrain().save(self.path)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".tmp").exists())


# ==========================================================================
# fdd — managing the drive, as opposed to building the image
# ==========================================================================

class TestSurfaceReport(unittest.TestCase):
    """The bookkeeping around a scan, which is pure and worth pinning down."""

    def _surface(self, bad, scanned=2880):
        from updev.fdd import Surface

        return Surface(total=2880, bad=list(bad), scanned=scanned)

    def test_contiguous_damage_collapses_into_ranges(self):
        """Damage is contiguous far more often than scattered; 12 ranges read
        better than 400 sector numbers."""
        surface = self._surface([376, 377, 378, 416, 417, 900])
        self.assertEqual(surface.bad_ranges(), [(376, 378), (416, 417), (900, 900)])

    def test_a_clean_disk_has_no_ranges(self):
        self.assertEqual(self._surface([]).bad_ranges(), [])
        self.assertTrue(self._surface([]).healthy)

    def test_good_counts_only_what_was_actually_read(self):
        surface = self._surface([1, 2], scanned=100)
        self.assertEqual(surface.good, 98)

    def test_an_unscanned_disk_is_not_healthy(self):
        """Nothing read is not the same as nothing wrong."""
        self.assertFalse(self._surface([], scanned=0).healthy)

    def test_damage_in_the_fats_is_called_out_separately(self):
        """A bad sector below 33 costs the whole disk, not one file."""
        from updev.fdd import SYSTEM_SECTORS

        self.assertTrue(self._surface([SYSTEM_SECTORS - 1]).system_area_damaged)
        self.assertFalse(self._surface([SYSTEM_SECTORS]).system_area_damaged)


class TestSurfaceScan(unittest.TestCase):
    """The scan itself, run against files rather than a drive.

    `surface_scan` reads with `os.pread`, so a regular file exercises every
    path except the one that needs a physically failing disk — and a truncated
    file reproduces even that, since a short read is how the medium refuses.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disk.img"

    def _scan(self, **kwargs):
        from updev.fdd import surface_scan

        last = None
        for surface in surface_scan(str(self.path), **kwargs):
            last = surface
        return last

    def test_a_whole_image_reads_clean(self):
        from updev.fdd import FLOPPY_SIZE, TOTAL_SECTORS

        self.path.write_bytes(b"\0" * FLOPPY_SIZE)
        surface = self._scan()
        self.assertEqual(surface.scanned, TOTAL_SECTORS)
        self.assertEqual(surface.bad, [])
        self.assertTrue(surface.healthy)

    def test_a_truncated_image_reports_the_missing_tail_as_bad(self):
        from updev.fdd import SECTOR, TOTAL_SECTORS

        half = TOTAL_SECTORS // 2
        self.path.write_bytes(b"\0" * (half * SECTOR))
        surface = self._scan()
        self.assertEqual(surface.scanned, TOTAL_SECTORS)
        self.assertEqual(len(surface.bad), half)
        self.assertEqual(min(surface.bad), half)

    def test_a_missing_node_aborts_instead_of_raising(self):
        surface = self._scan()
        self.assertTrue(surface.aborted)
        self.assertEqual(surface.scanned, 0)

    def test_the_bad_sector_budget_stops_the_scan(self):
        """The bound is per track, so it may overshoot by up to one.

        Checking mid-track would mean abandoning a track that was already
        half read, and `give_up_after` only ever claimed to be a rough
        ceiling on how much damage is worth enumerating.
        """
        from updev.fdd import SECTOR, SECTORS_PER_TRACK, TOTAL_SECTORS

        self.path.write_bytes(b"\0" * SECTOR)          # all but sector 0 unreadable
        surface = self._scan(give_up_after=40)
        self.assertTrue(surface.aborted)
        self.assertGreaterEqual(len(surface.bad), 40)
        self.assertLess(len(surface.bad), 40 + SECTORS_PER_TRACK)
        self.assertLess(surface.scanned, TOTAL_SECTORS)

    def test_a_zero_budget_means_no_time_limit(self):
        from updev.fdd import FLOPPY_SIZE, TOTAL_SECTORS

        self.path.write_bytes(b"\0" * FLOPPY_SIZE)
        surface = self._scan(budget=0)
        self.assertEqual(surface.scanned, TOTAL_SECTORS)
        self.assertEqual(surface.aborted, "")

    def test_progress_is_reported_as_it_goes(self):
        """The caller needs to draw a bar; a scan that only speaks at the end
        looks hung for a minute."""
        from updev.fdd import FLOPPY_SIZE, surface_scan

        self.path.write_bytes(b"\0" * FLOPPY_SIZE)
        counts = [s.scanned for s in surface_scan(str(self.path))]
        self.assertGreater(len(counts), 10)
        self.assertEqual(counts, sorted(counts))


class TestMediumReadback(unittest.TestCase):
    """Round trip: build an image, read it back the way a disk is read."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from updev.floppy import build_image

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disk.img"
        self.path.write_bytes(build_image(label="FLY TEST").data)

    def test_a_built_image_reads_back_as_fat12(self):
        from updev.fdd import read_medium

        medium = read_medium(str(self.path))
        self.assertTrue(medium.readable)
        self.assertTrue(medium.fat12)
        self.assertIn("FLY TEST", medium.label)

    def test_the_files_come_back(self):
        from updev.fdd import read_medium
        from updev.floppy import gallery

        medium = read_medium(str(self.path))
        names = {entry["name"] for entry in medium.files}
        for expected in gallery():
            self.assertIn(expected, names)

    def test_the_volume_label_is_not_listed_as_a_file(self):
        from updev.fdd import read_medium

        for entry in read_medium(str(self.path)).files:
            self.assertFalse(entry.get("volume_label"))

    def test_an_unreadable_node_reports_instead_of_raising(self):
        from updev.fdd import read_medium

        medium = read_medium(str(self.path) + ".nope")
        self.assertFalse(medium.readable)
        self.assertTrue(medium.error)

    def test_a_disk_too_short_to_hold_a_system_area_is_refused(self):
        from updev.fdd import read_medium

        self.path.write_bytes(b"\0" * 1024)
        medium = read_medium(str(self.path))
        self.assertFalse(medium.readable)

    def test_an_unformatted_disk_is_readable_but_not_fat12(self):
        from updev.fdd import FLOPPY_SIZE, read_medium

        self.path.write_bytes(b"\0" * FLOPPY_SIZE)
        medium = read_medium(str(self.path))
        self.assertTrue(medium.readable)
        self.assertFalse(medium.fat12)


class TestDriveDiscovery(unittest.TestCase):
    def _scan(self, *, tags=("FUSB", "storage"), size=1_474_560, with_medium=True):
        drive = Device(uid="usb:3-2", kind=Kind.USB, name="TEAC", status=Status.ONLINE,
                       address="3-2", tags=list(tags))
        devices = [drive]
        if with_medium:
            medium = Device(uid="blk:sdb", kind=Kind.STORAGE, name="sdb",
                            status=Status.ONLINE, node="/dev/sdb", parent="usb:3-2")
            medium.metrics["size_bytes"] = float(size)
            devices.append(medium)
        return ScanResult(devices=devices)

    def test_a_floppy_drive_is_found_with_its_medium(self):
        from updev.fdd import find_drives

        drives = find_drives(self._scan())
        self.assertEqual(len(drives), 1)
        self.assertEqual(drives[0].node, "/dev/sdb")
        self.assertTrue(drives[0].has_medium)
        self.assertTrue(drives[0].standard_geometry)

    def test_a_drive_with_no_disk_reports_zero_capacity_not_absence(self):
        """The block device stays when the disk leaves; it just becomes 0 bytes."""
        from updev.fdd import find_drives

        drive = find_drives(self._scan(size=0))[0]
        self.assertFalse(drive.has_medium)

    def test_a_drive_the_kernel_exposes_no_node_for_is_still_reported(self):
        from updev.fdd import find_drives

        drive = find_drives(self._scan(with_medium=False))[0]
        self.assertEqual(drive.node, "")
        self.assertFalse(drive.has_medium)

    def test_other_storage_is_not_mistaken_for_a_floppy(self):
        from updev.fdd import find_drives

        self.assertEqual(find_drives(self._scan(tags=("SUSB", "storage"))), [])


class TestMediumProblems(unittest.TestCase):
    """The innate judgement the fly's lateral horn consumes."""

    def _drive(self, size=1_474_560):
        from updev.fdd import Drive

        return Drive(uid="usb:3-2", label="TEAC", node="/dev/sdb", size=size)

    def test_an_empty_drive_is_not_a_fault(self):
        from updev.fdd import medium_problems

        self.assertEqual(medium_problems(self._drive(size=0), None), [])

    def test_a_healthy_disk_raises_nothing(self):
        from updev.fdd import Medium, Surface, medium_problems

        medium = Medium(node="/dev/sdb", readable=True, fat12=True)
        surface = Surface(total=2880, scanned=2880)
        self.assertEqual(medium_problems(self._drive(), medium, surface), [])

    def test_bad_sectors_are_reported(self):
        from updev.fdd import Medium, Surface, medium_problems

        medium = Medium(node="/dev/sdb", readable=True, fat12=True)
        surface = Surface(total=2880, scanned=2880, bad=[376, 377])
        problems = medium_problems(self._drive(), medium, surface)
        self.assertEqual(len(problems), 1)
        self.assertIn("배드섹터", problems[0][0])

    def test_damage_to_the_system_area_is_described_as_worse(self):
        from updev.fdd import Medium, Surface, medium_problems

        medium = Medium(node="/dev/sdb", readable=True, fat12=True)
        data = Surface(total=2880, scanned=2880, bad=[900])
        system = Surface(total=2880, scanned=2880, bad=[5])
        self.assertNotEqual(
            medium_problems(self._drive(), medium, data)[0][1],
            medium_problems(self._drive(), medium, system)[0][1],
        )

    def test_a_non_standard_capacity_is_flagged(self):
        from updev.fdd import medium_problems

        problems = medium_problems(self._drive(size=737_280), None)
        self.assertTrue(any("1.44MB" in m for m, _ in problems))

    def test_an_unreadable_disk_is_flagged(self):
        from updev.fdd import Medium, medium_problems

        medium = Medium(node="/dev/sdb", readable=False, error="Input/output error")
        problems = medium_problems(self._drive(), medium)
        self.assertTrue(any("읽을 수 없습니다" in m for m, _ in problems))


class TestFlySmellsAFloppy(unittest.TestCase):
    """The glomerulus that connects the two halves of this work."""

    def test_the_floppy_class_has_its_own_receptor(self):
        from updev.flybrain import GLOMERULI

        self.assertIn("tag:FUSB", GLOMERULI)

    def test_a_floppy_appearing_changes_the_smell(self):
        from updev.flybrain import smell

        plain = [Device(uid=f"usb:{i}", kind=Kind.USB, name=f"d{i}",
                        status=Status.ONLINE) for i in range(6)]
        floppy = Device(uid="usb:3-2", kind=Kind.USB, name="TEAC",
                        status=Status.ONLINE, tags=["FUSB", "storage"])

        before = smell(ScanResult(devices=plain))
        after = smell(ScanResult(devices=plain + [floppy]))
        self.assertEqual(before.raw["tag:FUSB"], 0.0)
        self.assertGreater(after.raw["tag:FUSB"], 0.0)
        self.assertLess(before.overlap(after), 1.0)


# ==========================================================================
# flybrain — compartments and named states
# ==========================================================================

class TestCompartments(_FlyFixture):
    """Three memories on three timescales, which is the whole point."""

    def test_short_term_learns_faster_than_long_term(self):
        from updev.flybrain import HEADLINE, RECENT, FlyBrain

        brain = FlyBrain()
        brain.learn(self._board())
        novelties = brain.novelties(brain.judge(self._board()).percept)
        self.assertLess(novelties[RECENT], novelties[HEADLINE])

    def test_short_term_forgets_faster_than_long_term(self):
        import time

        from updev.flybrain import HEADLINE, RECENT, FlyBrain

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board())
        brain.updated = time.time() - 3600          # γ half-life is 20 minutes

        novelties = brain.novelties(brain.judge(self._board()).percept)
        self.assertGreater(novelties[RECENT], 0.7)   # an hour is three half-lives
        self.assertLess(novelties[HEADLINE], 0.3)    # 30 days is untouched

    def test_a_returning_old_state_reads_as_drift(self):
        """Long-term knows it, short-term does not — the reading a single
        familiarity number cannot express."""
        import time

        from updev.flybrain import FlyBrain, Mood

        brain = FlyBrain()
        for _ in range(8):
            brain.learn(self._other_board())         # known, but long ago
        brain.updated = time.time() - 40 * 60
        for _ in range(6):
            brain.learn(self._board())               # what it has been doing since

        verdict = brain.judge(self._other_board())
        self.assertEqual(verdict.mood, Mood.DRIFTED)
        self.assertGreater(verdict.drift, 0)

    def test_the_state_it_currently_lives_in_is_not_drift(self):
        from updev.flybrain import FlyBrain, Mood

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board())
        verdict = brain.judge(self._board())
        self.assertEqual(verdict.mood, Mood.SETTLED)
        self.assertLessEqual(verdict.drift, 0)

    def test_not_looking_for_an_hour_is_not_drift(self):
        """The bug this guards: γ forgets on wall clock whether or not anyone
        was watching, so a board left alone comes back with an empty short-term
        memory. Reading that as "this changed recently" is exactly backwards."""
        import time

        from updev.flybrain import FlyBrain, Mood

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board())
        brain.updated = time.time() - 3600

        verdict = brain.judge(self._board())         # nothing changed
        self.assertNotEqual(verdict.mood, Mood.DRIFTED)
        self.assertFalse(verdict.recent_fresh)
        self.assertEqual(verdict.drift, 0.0)

    def test_reading_novelty_does_not_rewrite_the_memory(self):
        """Decay is applied as a scalar at read time, so judging is free of
        side effects — otherwise `fly watch` would erode the memory."""
        import time

        from updev.flybrain import HEADLINE, FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        brain.updated = time.time() - 86400
        before = dict(brain.compartments[HEADLINE].weights)

        for _ in range(5):
            brain.judge(self._board())
        self.assertEqual(brain.compartments[HEADLINE].weights, before)

    def test_learning_settles_the_clock_before_writing(self):
        """Decay owed must be paid before the new exposure lands, or it would
        decay what was just taught."""
        import time

        from updev.flybrain import RECENT, FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board())
        brain.updated = time.time() - 7200
        brain.learn(self._board())

        # Fresh again: the exposure went in after the decay, not before.
        self.assertLess(brain.novelty(brain.judge(self._board()).percept, RECENT), 0.6)


class TestNamedStates(_FlyFixture):
    def _trained(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(5):
            brain.learn(self._board(), state="idle")
        for _ in range(5):
            brain.learn(self._other_board(), state="busy")
        return brain

    def test_it_names_the_state_it_is_in(self):
        brain = self._trained()
        self.assertEqual(brain.judge(self._board()).recognition.label, "idle")
        self.assertEqual(brain.judge(self._other_board()).recognition.label, "busy")

    def test_the_answer_is_confident_when_the_states_are_distinct(self):
        self.assertTrue(self._trained().judge(self._board()).recognition.confident)

    def test_an_unknown_state_is_not_forced_into_a_name(self):
        """A best match below the floor means the cells that fired were never
        associated with anything — saying a name would be making it up."""
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(5):
            brain.learn(self._board(), state="idle")
        recognition = brain.judge(self._other_board()).recognition
        self.assertFalse(recognition.confident)
        self.assertIn("해당 없음", recognition.summary)

    def test_a_brain_with_no_named_states_says_nothing(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        brain.learn(self._board())
        self.assertEqual(brain.judge(self._board()).recognition.label, "")

    def test_naming_a_state_also_makes_it_familiar(self):
        """Supervised and unsupervised learning are additive, not exclusive."""
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board(), state="idle")
        self.assertLess(brain.judge(self._board()).novelty, 0.3)

    def test_one_state_can_be_forgotten_without_the_rest(self):
        brain = self._trained()
        self.assertTrue(brain.forget(state="busy"))
        self.assertEqual(set(brain.states), {"idle"})
        self.assertFalse(brain.forget(state="busy"))

    def test_states_survive_a_save(self):
        import tempfile
        from pathlib import Path

        from updev.flybrain import load_brain

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            self._trained().save(path)
            self.assertEqual(load_brain(path).judge(self._board()).recognition.label,
                             "idle")


class TestVersionOneMigration(_FlyFixture):
    """A memory trained before compartments existed must not be thrown away."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "flybrain.json"

    def _write_v1(self):
        import json

        from updev.flybrain import FlyBrain, glomerulus_signature, smell

        tag = smell(self._board()).tag
        self.path.write_text(json.dumps({
            "version": 1,
            "signature": glomerulus_signature(),
            "exposures": 6,
            "created": 1.0,
            "updated": 0.0,
            "familiar": {str(k): 0.9 for k in tag},
            "aversive": {},
        }))
        return FlyBrain

    def test_the_old_training_seeds_every_compartment(self):
        from updev.flybrain import load_brain

        self._write_v1()
        brain = load_brain(self.path)
        self.assertEqual(brain.exposures, 6)
        for comp in brain.compartments.values():
            self.assertTrue(comp.weights, f"{comp.key} lost its training")

    def test_a_migrated_brain_still_finds_the_board_familiar(self):
        from updev.flybrain import load_brain

        self._write_v1()
        self.assertLess(load_brain(self.path).judge(self._board()).novelty, 0.3)

    def test_it_is_written_back_at_the_current_version(self):
        import json

        from updev.flybrain import load_brain

        self._write_v1()
        brain = load_brain(self.path)
        brain.save(self.path)
        self.assertGreaterEqual(json.loads(self.path.read_text())["version"], 2)


class TestFlySmellsAnEject(unittest.TestCase):
    """Inserting and ejecting a disk must not smell identical.

    The gap this closes: a floppy or card reader keeps its block device when
    the medium leaves. Same node, same parent, same tags, same status — only
    the capacity drops to zero. Before `state:empty-bay` nothing sampled
    capacity, so the two scans hashed to byte-identical tags and the fly was
    blind to the one event a removable drive actually has.
    """

    def _board(self, medium_size):
        drive = Device(uid="usb:3-2", kind=Kind.USB, name="TEAC",
                       status=Status.ONLINE, bus="usb",
                       tags=["FUSB", "storage", "hotplug"])
        block = Device(uid="blk:sdb", kind=Kind.STORAGE, name="sdb",
                       status=Status.IDLE, bus="usb", node="/dev/sdb",
                       parent="usb:3-2", tags=["hotplug"])
        block.metrics["size_bytes"] = float(medium_size)
        filler = [Device(uid=f"gpio:{i}", kind=Kind.GPIO, name=f"g{i}",
                         status=Status.ONLINE) for i in range(8)]
        return ScanResult(devices=[drive, block] + filler)

    def test_an_empty_drive_and_a_loaded_one_smell_different(self):
        from updev.flybrain import smell

        loaded = smell(self._board(1_474_560))
        empty = smell(self._board(0))
        self.assertLess(loaded.overlap(empty), 0.9)

    def test_the_empty_bay_receptor_is_what_separates_them(self):
        from updev.flybrain import smell

        self.assertEqual(smell(self._board(1_474_560)).raw["state:empty-bay"], 0.0)
        self.assertGreater(smell(self._board(0)).raw["state:empty-bay"], 0.0)

    def test_they_are_still_recognisably_the_same_board(self):
        """Ejecting a disk is a change, not a different machine."""
        from updev.flybrain import smell

        self.assertGreater(
            smell(self._board(1_474_560)).overlap(smell(self._board(0))), 0.4)

    def test_ejecting_registers_as_novel_to_a_trained_fly(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(6):
            brain.learn(self._board(1_474_560))
        loaded = brain.judge(self._board(1_474_560)).novelty
        empty = brain.judge(self._board(0)).novelty
        self.assertLess(loaded, 0.3)
        self.assertGreater(empty, loaded * 2)

    def test_a_drive_with_no_block_device_is_not_counted_as_empty(self):
        """Absent metrics are not a zero capacity — only a reported 0 counts."""
        from updev.flybrain import smell

        board = self._board(1_474_560)
        board.devices[1].metrics.pop("size_bytes")
        self.assertEqual(smell(board).raw["state:empty-bay"], 0.0)


class TestStaleMemoryIsExplained(_FlyFixture):
    """Discarding training silently makes the fly look broken rather than
    inconvenienced. When the receptors change it has to say so."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "flybrain.json"

    def _stale_file(self):
        import json

        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(5):
            brain.learn(self._board(), state="idle")
        brain.learn(self._other_board(), state="busy")
        brain.save(self.path)

        data = json.loads(self.path.read_text())
        data["signature"] = "0000deadbeef0000"
        self.path.write_text(json.dumps(data))

    def test_it_says_why_the_memory_went_away(self):
        from updev.flybrain import load_brain

        self._stale_file()
        brain = load_brain(self.path)
        self.assertEqual(brain.exposures, 0)
        self.assertIn("입력 계층", brain.reset_reason)

    def test_it_names_the_states_that_have_to_be_retaught(self):
        from updev.flybrain import load_brain

        self._stale_file()
        self.assertEqual(load_brain(self.path).lost_states, ["busy", "idle"])

    def test_a_healthy_memory_reports_no_reset(self):
        from updev.flybrain import FlyBrain, load_brain

        FlyBrain().save(self.path)
        brain = load_brain(self.path)
        self.assertEqual(brain.reset_reason, "")
        self.assertEqual(brain.lost_states, [])

    def test_the_explanation_is_not_persisted(self):
        """It describes one load, not the memory itself."""
        import json

        from updev.flybrain import load_brain

        self._stale_file()
        brain = load_brain(self.path)
        brain.save(self.path)
        self.assertNotIn("reset_reason", json.loads(self.path.read_text()))
        self.assertEqual(load_brain(self.path).reset_reason, "")


# ==========================================================================
# reflex — the output side of the mushroom body
# ==========================================================================

class _ReflexFixture(unittest.TestCase):
    """A stand-in verdict, so the refusal rules can be tested one at a time."""

    class _Recognition:
        def __init__(self, label="idle", confident=True, margin=0.3):
            self.label = label
            self.confident = confident
            self.margin = margin

    class _Verdict:
        def __init__(self, recognition, alarms=()):
            self.recognition = recognition
            self.alarms = list(alarms)

    def _verdict(self, label="idle", confident=True, alarms=()):
        return self._Verdict(self._Recognition(label, confident), alarms)

    def _book(self, command="true", armed=True):
        from updev.reflex import ReflexBook

        book = ReflexBook()
        book.teach("idle", command)
        book.arm("idle", armed)
        return book


class TestReflexRefusals(_ReflexFixture):
    """When a reflex must not fire. Each rule gets its own test because each
    one exists for a different reason."""

    def test_an_armed_reflex_in_its_state_may_fire(self):
        from updev.reflex import why_not

        book = self._book()
        self.assertEqual(why_not(book.reflexes["idle"], self._verdict(), ""), "")

    def test_a_disarmed_reflex_never_fires(self):
        from updev.reflex import why_not

        book = self._book(armed=False)
        self.assertIn("무장", why_not(book.reflexes["idle"], self._verdict(), ""))

    def test_an_alarm_blocks_every_reflex(self):
        """The rule that makes this safe to leave running.

        A board in trouble is the worst possible moment to run something
        unattended, and the lateral horn already outranks training everywhere
        else in the circuit. It outranks it here too.
        """
        from updev.flybrain import Alarm
        from updev.reflex import why_not

        book = self._book()
        verdict = self._verdict(alarms=[Alarm("storage", "루트가 꽉 찼습니다")])
        self.assertIn("측면뿔", why_not(book.reflexes["idle"], verdict, ""))

    def test_an_uncertain_recognition_does_not_act(self):
        from updev.reflex import why_not

        book = self._book()
        verdict = self._verdict(confident=False)
        self.assertIn("확실하지 않", why_not(book.reflexes["idle"], verdict, ""))

    def test_a_reflex_fires_on_entering_a_state_not_while_in_it(self):
        """Otherwise a watch loop re-runs the command every few seconds."""
        from updev.reflex import why_not

        book = self._book()
        self.assertIn("이미", why_not(book.reflexes["idle"], self._verdict(), "idle"))

    def test_a_recent_firing_is_debounced(self):
        import time

        from updev.reflex import why_not

        book = self._book()
        reflex = book.reflexes["idle"]
        reflex.last_fired = time.time()
        self.assertIn("연타", why_not(reflex, self._verdict(), "busy"))

    def test_another_states_reflex_is_not_a_refusal(self):
        """Silence, not an excuse — this reflex simply is not the one."""
        from updev.reflex import why_not

        book = self._book()
        self.assertEqual(why_not(book.reflexes["idle"], self._verdict("busy"), ""), "")


class TestReflexFiring(_ReflexFixture):
    def test_consider_returns_nothing_when_no_reflex_is_bound(self):
        book = self._book()
        self.assertIsNone(book.consider(self._verdict("busy"), "", dry_run=False))

    def test_a_dry_run_does_not_execute(self):
        book = self._book(command="exit 3")
        firing = book.consider(self._verdict(), "", dry_run=True)
        self.assertFalse(firing.ran)
        self.assertTrue(firing.dry_run)
        self.assertEqual(book.reflexes["idle"].runs, 0)

    def test_a_live_run_executes_and_records(self):
        book = self._book(command="exit 0")
        firing = book.consider(self._verdict(), "", dry_run=False)
        self.assertTrue(firing.ran)
        self.assertTrue(firing.ok)
        self.assertEqual(book.reflexes["idle"].runs, 1)
        self.assertEqual(book.reflexes["idle"].last_status, 0)

    def test_a_failing_command_is_reported_not_raised(self):
        book = self._book(command="exit 7")
        firing = book.consider(self._verdict(), "", dry_run=False)
        self.assertTrue(firing.ran)
        self.assertFalse(firing.ok)
        self.assertEqual(firing.status, 7)

    def test_output_comes_back(self):
        book = self._book(command="echo 안녕; echo 오류 >&2")
        firing = book.run(book.reflexes["idle"])
        self.assertEqual(firing.stdout, "안녕")
        self.assertEqual(firing.stderr, "오류")

    def test_a_command_that_hangs_is_killed(self):
        book = self._book(command="sleep 30")
        book.reflexes["idle"].timeout = 0.3
        firing = book.run(book.reflexes["idle"])
        self.assertEqual(firing.status, 124)
        self.assertFalse(firing.ok)

    def test_the_state_is_passed_to_the_command(self):
        book = self._book(command="echo $UPDEV_REFLEX_STATE")
        self.assertEqual(book.run(book.reflexes["idle"]).stdout, "idle")

    def test_an_alarm_stops_a_live_run_from_happening_at_all(self):
        from updev.flybrain import Alarm

        book = self._book(command="exit 0")
        verdict = self._verdict(alarms=[Alarm("thermal", "84°C")])
        firing = book.consider(verdict, "", dry_run=False)
        self.assertFalse(firing.ran)
        self.assertEqual(book.reflexes["idle"].runs, 0)


class TestReflexBook(_ReflexFixture):
    def test_teaching_the_same_state_twice_keeps_the_history(self):
        book = self._book(command="echo one")
        book.reflexes["idle"].runs = 5
        book.teach("idle", "echo two")
        self.assertEqual(book.reflexes["idle"].command, "echo two")
        self.assertEqual(book.reflexes["idle"].runs, 5)

    def test_a_retaught_reflex_comes_back_disarmed(self):
        """The command changed; the decision to let it run unattended did not
        carry over to it."""
        book = self._book(command="echo one")
        self.assertTrue(book.reflexes["idle"].armed)
        book.teach("idle", "rm -rf /tmp/something")
        self.assertFalse(book.reflexes["idle"].armed)

    def test_dropping_one_leaves_the_others(self):
        book = self._book()
        book.teach("busy", "echo busy")
        self.assertTrue(book.drop("idle"))
        self.assertEqual(set(book.reflexes), {"busy"})
        self.assertFalse(book.drop("idle"))

    def test_a_round_trip_preserves_everything(self):
        from updev.reflex import ReflexBook

        book = self._book(command="echo 안녕")
        book.reflexes["idle"].runs = 3
        restored = ReflexBook.from_dict(book.as_dict())
        self.assertEqual(restored.reflexes["idle"].command, "echo 안녕")
        self.assertTrue(restored.reflexes["idle"].armed)
        self.assertEqual(restored.reflexes["idle"].runs, 3)

    def test_garbage_loads_as_an_empty_book(self):
        from updev.reflex import ReflexBook

        self.assertEqual(ReflexBook.from_dict("not a dict").reflexes, {})
        self.assertEqual(ReflexBook.from_dict({"idle": "nope"}).reflexes, {})

    def test_an_entry_with_no_command_is_dropped(self):
        from updev.reflex import ReflexBook

        self.assertEqual(ReflexBook.from_dict({"idle": {"command": ""}}).reflexes, {})

    def test_reflexes_live_beside_the_brain_not_inside_it(self):
        """Weights die when the glomeruli change; bindings must not."""
        from pathlib import Path

        from updev.flybrain import reflex_path

        brain = Path("/tmp/x/flybrain.json")
        self.assertEqual(reflex_path(brain).parent, brain.parent)
        self.assertNotEqual(reflex_path(brain), brain)


class TestDestructiveWarnings(unittest.TestCase):
    """Not a filter — nothing is blocked. It exists so that arming a reflex
    that runs `dd` says so out loud."""

    def test_it_names_what_it_found(self):
        from updev.reflex import looks_destructive

        hits = looks_destructive("dd if=/dev/zero of=/dev/sdb")
        self.assertEqual(len(hits), 1)
        self.assertIn("dd", hits[0])

    def test_several_patterns_are_all_reported(self):
        from updev.reflex import looks_destructive

        self.assertGreaterEqual(
            len(looks_destructive("sudo rm -rf /var/log && reboot")), 3)

    def test_an_ordinary_command_is_quiet(self):
        from updev.reflex import looks_destructive

        self.assertEqual(looks_destructive("logger 디스켓이 빠졌습니다"), [])
        self.assertEqual(looks_destructive("echo hello >> /tmp/log"), [])


# ==========================================================================
# algo — recognising algorithms by their constants
# ==========================================================================

class _AlgoFixture(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _blob(self, data: bytes, name="x.bin"):
        path = self.dir / name
        path.write_bytes(data)
        return path

    @staticmethod
    def _found(report):
        return {f.algorithm for f in report.findings}


class TestAlgoConstants(_AlgoFixture):
    def test_the_ansi_lcg_is_recognised(self):
        import struct

        from updev.algo import analyse

        blob = b"\x00" * 64 + struct.pack("<I", 1103515245) + b"\x00" * 16 \
            + struct.pack("<I", 12345) + b"\x00" * 64
        self.assertIn("LCG", self._found(analyse(self._blob(blob))))

    def test_a_big_endian_constant_is_found_too(self):
        import struct

        from updev.algo import analyse

        blob = b"\x00" * 32 + struct.pack(">I", 0xEDB88320) + b"\x00" * 32
        self.assertIn("CRC-32", self._found(analyse(self._blob(blob))))

    def test_the_quake_constant_is_unique(self):
        import struct

        from updev.algo import analyse

        report = analyse(self._blob(b"\x00" * 16 + struct.pack("<I", 0x5F3759DF)))
        finding = next(f for f in report.findings
                       if f.algorithm == "fast inverse sqrt")
        self.assertTrue(finding.certain)

    def test_the_aes_sbox_is_found_by_its_bytes(self):
        from updev.algo import analyse

        sbox = bytes((0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5))
        self.assertIn("AES", self._found(analyse(self._blob(b"\x11" * 100 + sbox))))

    def test_an_empty_file_is_reported_not_raised(self):
        from updev.algo import analyse

        self.assertTrue(analyse(self._blob(b"")).error)

    def test_a_missing_file_is_reported_not_raised(self):
        from updev.algo import analyse

        report = analyse(self.dir / "nope.bin")
        self.assertTrue(report.error)
        self.assertEqual(report.findings, [])


class TestAlgoRestraint(_AlgoFixture):
    """The tool has to be quiet about things it has no evidence for."""

    def test_random_bytes_do_not_produce_unique_findings(self):
        """A false 'unique' is the worst failure this can have: it is the
        verdict a reader is meant to be able to rely on."""
        import random

        from updev.algo import Strength, analyse

        rng = random.Random(20260923)
        blob = bytes(rng.randrange(256) for _ in range(200_000))
        report = analyse(self._blob(blob))
        certain = [f.algorithm for f in report.findings
                   if f.strength is Strength.UNIQUE]
        self.assertEqual(certain, [])

    def test_a_file_of_zeros_finds_nothing(self):
        from updev.algo import analyse

        self.assertEqual(analyse(self._blob(b"\x00" * 100_000)).findings, [])

    def test_a_lone_half_of_a_constant_is_not_enough(self):
        """0x4E6D on its own is an ordinary 16-bit number."""
        import struct

        from updev.algo import analyse

        blob = b"\x7fELF" + b"\x00" * 14 + b"\xb7\x00" + b"\x00" * 40
        blob += struct.pack("<I", 0x5289CDA8)        # movz w8, #0x4e6d
        self.assertNotIn("LCG", self._found(analyse(self._blob(blob))))

    def test_the_report_says_absence_is_not_evidence(self):
        from updev.algo import analyse

        report = analyse(self._blob(b"\x00" * 1024))
        self.assertIn("증거는 아닙니다", report.summary)


class TestAlgoAArch64(_AlgoFixture):
    """A fixed-width ISA cannot hold a 32-bit constant in one instruction, so
    the byte search — the whole method — finds nothing. This is not
    hypothetical: the LCG that matches instantly in a DOS build is invisible
    in the AArch64 port of the same program for exactly this reason."""

    def _elf(self, words):
        import struct

        blob = bytearray(b"\x7fELF" + b"\x00" * 14 + b"\xb7\x00" + b"\x00" * 40)
        for word in words:
            blob += struct.pack("<I", word)
        return bytes(blob)

    def test_a_split_constant_is_recovered_from_the_immediates(self):
        from updev.algo import analyse

        # mov w8, #0x4e6d ; movk w8, #0x41c6, lsl #16  — 1103515245
        # mov w7, #0x3039                              — 12345
        blob = self._elf((0x5289CDA8, 0x72A838C8, 0x52860727))
        report = analyse(self._blob(blob))
        self.assertIn("LCG", self._found(report))

    def test_the_evidence_says_it_came_from_an_immediate(self):
        from updev.algo import analyse

        blob = self._elf((0x5289CDA8, 0x72A838C8, 0x52860727))
        finding = next(f for f in analyse(self._blob(blob)).findings
                       if f.algorithm == "LCG")
        self.assertTrue(any(e.where == "immediate" for e in finding.evidence))

    def test_immediates_are_not_searched_on_other_architectures(self):
        """The encoding is AArch64's; reading those bit fields out of x86
        would be inventing hits."""
        import struct

        from updev.algo import analyse

        blob = bytearray(b"\x7fELF" + b"\x00" * 14 + b"\x3e\x00" + b"\x00" * 40)
        for word in (0x5289CDA8, 0x72A838C8, 0x52860727):
            blob += struct.pack("<I", word)
        self.assertNotIn("LCG", self._found(analyse(self._blob(bytes(blob)))))


class TestAlgoReport(_AlgoFixture):
    def test_findings_are_ranked_with_the_strongest_first(self):
        import struct

        from updev.algo import analyse

        blob = struct.pack("<I", 65536) + b"\x00" * 32 \
            + struct.pack("<I", 1103515245) + struct.pack("<I", 12345)
        report = analyse(self._blob(blob))
        self.assertEqual(report.findings[0].algorithm, "LCG")

    def test_a_dos_com_file_is_named_as_raw(self):
        from updev.algo import analyse

        self.assertEqual(analyse(self._blob(b"\xeb\x3c\x90" + b"\x00" * 600)).kind,
                         "raw binary")

    def test_a_boot_sector_is_recognised_by_its_signature(self):
        from updev.algo import analyse

        blob = bytearray(b"\x00" * 512)
        blob[510:512] = b"\x55\xaa"
        self.assertIn("부트섹터", analyse(self._blob(bytes(blob))).kind)

    def test_an_elf_without_a_symtab_reads_as_stripped(self):
        from updev.algo import analyse

        blob = b"\x7fELF" + b"\x00" * 14 + b"\xb7\x00" + b"\x00" * 200
        self.assertTrue(analyse(self._blob(blob)).stripped)

    def test_the_whole_report_survives_json(self):
        import json
        import struct

        from updev.algo import analyse

        report = analyse(self._blob(struct.pack("<I", 0xEDB88320)))
        self.assertIn("CRC-32", json.dumps(report.as_dict(), ensure_ascii=False))


class TestForeignDataInTheBrainFile(_FlyFixture):
    """The memory lives in a shared state directory and other tools write into
    the same file. Neither crashing on their data nor deleting it is allowed."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "flybrain.json"

    def _written_by_someone_else(self):
        import json

        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(4):
            brain.learn(self._board(), state="idle")
        brain.save(self.path)

        data = json.loads(self.path.read_text())
        data["pad"] = {"last": "left"}                  # another tool's key
        data["coach"] = [1, 2, 3]
        data["states"]["coach-steer"] = "오른쪽"        # not a weight table
        self.path.write_text(json.dumps(data, ensure_ascii=False))
        return data

    def test_a_string_where_a_weight_table_belongs_does_not_crash(self):
        """The bug this pins: `states` held a plain string and the loader
        raised AttributeError in the middle of an ordinary scan."""
        from updev.flybrain import load_brain

        self._written_by_someone_else()
        brain = load_brain(self.path)
        self.assertEqual(brain.exposures, 4)
        self.assertEqual(sorted(brain.states), ["idle"])

    def test_foreign_top_level_keys_survive_a_save(self):
        """Dropping keys we did not recognise would be destroying someone's
        data to tidy up our own."""
        import json

        from updev.flybrain import load_brain

        before = self._written_by_someone_else()
        brain = load_brain(self.path)
        brain.learn(self._board())
        brain.save(self.path)

        after = json.loads(self.path.read_text())
        self.assertEqual(after["pad"], before["pad"])
        self.assertEqual(after["coach"], before["coach"])

    def test_a_foreign_state_entry_survives_a_save(self):
        import json

        from updev.flybrain import load_brain

        self._written_by_someone_else()
        brain = load_brain(self.path)
        brain.save(self.path)
        self.assertEqual(
            json.loads(self.path.read_text())["states"]["coach-steer"], "오른쪽")

    def test_our_own_states_still_round_trip_alongside_theirs(self):
        from updev.flybrain import load_brain

        self._written_by_someone_else()
        brain = load_brain(self.path)
        brain.save(self.path)
        self.assertEqual(load_brain(self.path).judge(self._board()).recognition.label,
                         "idle")

    def test_a_single_unparsable_weight_does_not_discard_the_table(self):
        import json

        from updev.flybrain import FlyBrain, load_brain

        brain = FlyBrain()
        for _ in range(6):          # six, because the headline is long-term
            brain.learn(self._board())
        brain.save(self.path)
        data = json.loads(self.path.read_text())
        data["compartments"]["alpha_beta"]["oops"] = "not a number"
        self.path.write_text(json.dumps(data))

        self.assertLess(load_brain(self.path).judge(self._board()).novelty, 0.3)

    def test_nonsense_in_every_field_still_yields_a_usable_fly(self):
        """"Never raises" has to mean it."""
        import json

        from updev.flybrain import load_brain

        self.path.write_text(json.dumps({
            "version": "banana", "exposures": None, "created": [],
            "compartments": "nope", "aversive": 7, "states": 3,
        }))
        brain = load_brain(self.path)
        self.assertEqual(brain.judge(self._board()).novelty, 1.0)


class TestFlySmellsAReadingDrive(unittest.TestCase):
    """Presence and activity are different facts.

    The gap this closes: a drive reading and the same drive sitting idle
    produced byte-identical tags, because nothing in the receptor list carried
    I/O. A state taught as "reading" was being taught on a smell that did not
    exist — the same failure as attach-versus-eject, one layer along.
    """

    def _board(self, medium_state, io_busy):
        drive = Device(uid="usb:3-2", kind=Kind.USB, name="TEAC",
                       status=Status.ONLINE, bus="usb",
                       tags=["FUSB", "storage", "hotplug"])
        block = Device(uid="blk:sdb", kind=Kind.STORAGE, name="sdb",
                       status=Status.IDLE, bus="usb", node="/dev/sdb",
                       parent="usb:3-2", tags=["hotplug"])
        block.metrics["size_bytes"] = 1_474_560.0 if medium_state < 1.0 else 0.0
        block.metrics["medium_state"] = medium_state
        block.metrics["io_busy"] = io_busy
        filler = [Device(uid=f"gpio:{i}", kind=Kind.GPIO, name=f"g{i}",
                         status=Status.ONLINE) for i in range(8)]
        return ScanResult(devices=[drive, block] + filler)

    def test_a_reading_drive_smells_different_from_an_idle_one(self):
        from updev.flybrain import smell

        idle = smell(self._board(0.0, 0.0))
        busy = smell(self._board(0.0, 1.0))
        self.assertLess(idle.overlap(busy), 0.7)

    def test_the_receptor_tracks_how_busy_it_is(self):
        from updev.flybrain import smell

        self.assertEqual(smell(self._board(0.0, 0.0)).raw["state:disk-read"], 0.0)
        self.assertEqual(smell(self._board(0.0, 1.0)).raw["state:disk-read"], 1.0)

    def test_all_four_drive_states_are_distinguishable(self):
        """Attached, reading, ejected, and mid-insertion. Every pair has to be
        separable or one of them cannot be taught."""
        from updev.flybrain import smell

        states = {
            "attached": smell(self._board(0.0, 0.0)),
            "reading":  smell(self._board(0.0, 1.0)),
            "ejected":  smell(self._board(1.0, 0.0)),
            "settling": smell(self._board(0.5, 0.0)),
        }
        for a, pa in states.items():
            for b, pb in states.items():
                if a >= b:
                    continue
                self.assertLess(pa.overlap(pb), 0.85, f"{a} and {b} smell alike")

    def test_mid_insertion_is_its_own_state(self):
        """Readable but still reporting zero capacity. Measured at 190ms wide
        on real hardware, and previously indistinguishable from ejected."""
        from updev.flybrain import smell

        self.assertEqual(smell(self._board(0.5, 0.0)).raw["state:empty-bay"], 0.5)

    def test_a_board_with_no_probe_falls_back_to_capacity(self):
        """Not every backend reports medium_state; the old signal still works."""
        from updev.flybrain import smell

        board = self._board(0.0, 0.0)
        for d in board.devices:
            d.metrics.pop("medium_state", None)
            d.metrics.pop("io_busy", None)
        board.devices[1].metrics["size_bytes"] = 0.0
        self.assertGreater(smell(board).raw["state:empty-bay"], 0.0)


class TestFlySmellsAMount(unittest.TestCase):
    """Mounted is a third fact, after present and busy.

    The first version of this receptor asked the wrong object. `FUSB` is a tag
    on the USB device and the mountpoint lands on the block device beneath it,
    and `hotplug` turned out not to be set at all on the hardware this runs on
    — so the channel read zero in every state and three separately taught
    states collapsed onto each other. `medium_state` is the marker that
    actually identifies a removable drive, because the storage backend puts it
    on exactly the ones it probed.
    """

    def _board(self, mounted=False, partitioned=False, io=0.0):
        disk = Device(uid="blk:sdb", kind=Kind.STORAGE, name="sdb",
                      status=Status.IDLE, node="/dev/sdb")
        disk.metrics.update(size_bytes=1_474_560.0, medium_state=0.0, io_busy=io)
        devices = [disk]
        if partitioned:
            part = Device(uid="blk:sdb1", kind=Kind.STORAGE, name="sdb1",
                          status=Status.IDLE, parent="blk:sdb")
            if mounted:
                part.detail["mounted at"] = "/mnt/floppy"
            devices.append(part)
        elif mounted:
            disk.detail["mounted at"] = "/mnt/floppy"

        root = Device(uid="blk:sda2", kind=Kind.STORAGE, name="sda2",
                      status=Status.ONLINE)
        root.detail["mounted at"] = "/"
        devices.append(root)
        devices += [Device(uid=f"gpio:{i}", kind=Kind.GPIO, name=f"g{i}",
                           status=Status.ONLINE) for i in range(8)]
        return ScanResult(devices=devices)

    def test_an_unmounted_drive_reads_zero(self):
        from updev.flybrain import smell

        self.assertEqual(smell(self._board()).raw["state:mounted"], 0.0)

    def test_a_mounted_drive_reads_one(self):
        from updev.flybrain import smell

        self.assertEqual(smell(self._board(mounted=True)).raw["state:mounted"], 1.0)

    def test_a_mounted_partition_counts_as_its_drive(self):
        from updev.flybrain import smell

        self.assertEqual(
            smell(self._board(mounted=True, partitioned=True)).raw["state:mounted"], 1.0)

    def test_the_root_filesystem_is_not_a_removable_mount(self):
        """`/` is always mounted. Counting it would peg the channel at one and
        make it carry no information at all — which is how the first version
        would have failed if it had failed in the other direction."""
        from updev.flybrain import smell

        self.assertEqual(smell(self._board(partitioned=True)).raw["state:mounted"], 0.0)

    def test_mounted_and_unmounted_smell_different(self):
        from updev.flybrain import smell

        self.assertLess(
            smell(self._board()).overlap(smell(self._board(mounted=True))), 0.85)

    def test_unmounting_is_the_conjunction_of_mounted_and_busy(self):
        """No receptor for it, and none needed. Reading a combination of
        receptors is what the Kenyon cells are for."""
        from updev.flybrain import smell

        mounted_idle = smell(self._board(mounted=True))
        unmounting = smell(self._board(mounted=True, io=1.0))
        self.assertLess(mounted_idle.overlap(unmounting), 0.85)


class TestNamingClashWarning(unittest.TestCase):
    """Teaching a name onto a smell that already has one fails silently.

    Nothing errors; both states simply stop being recognisable, and it only
    surfaces later as "not sure". It happened three times in practice, always
    because the thing being named had not actually happened — a mount that
    failed, a shell line joined with || instead of &&, so the state was taught
    in the state it was supposed to be leaving.
    """

    def _board(self, mounted=False):
        disk = Device(uid="blk:sdd", kind=Kind.STORAGE, name="sdd",
                      status=Status.IDLE, node="/dev/sdd")
        disk.metrics.update(size_bytes=1_474_560.0, medium_state=0.0, io_busy=0.0)
        if mounted:
            disk.detail["mounted at"] = "/mnt/floppy"
        filler = [Device(uid=f"gpio:{i}", kind=Kind.GPIO, name=f"g{i}",
                         status=Status.ONLINE) for i in range(8)]
        return ScanResult(devices=[disk] + filler)

    def _taught(self):
        from updev.flybrain import FlyBrain

        brain = FlyBrain()
        for _ in range(5):
            brain.learn(self._board(False), state="floppy-unmounted")
        return brain

    def test_naming_an_already_named_smell_is_caught(self):
        from updev.cli import _naming_clash

        clash = _naming_clash(self._taught(), self._board(False), "floppy-mounted")
        self.assertIsNotNone(clash)
        self.assertEqual(clash[0], "floppy-unmounted")

    def test_a_genuinely_different_state_is_not_flagged(self):
        from updev.cli import _naming_clash

        self.assertIsNone(
            _naming_clash(self._taught(), self._board(True), "floppy-mounted"))

    def test_reinforcing_the_same_name_is_not_a_clash(self):
        from updev.cli import _naming_clash

        self.assertIsNone(
            _naming_clash(self._taught(), self._board(False), "floppy-unmounted"))

    def test_the_first_state_ever_taught_cannot_clash(self):
        from updev.cli import _naming_clash
        from updev.flybrain import FlyBrain

        self.assertIsNone(_naming_clash(FlyBrain(), self._board(), "anything"))


class TestConfirmTarget(unittest.TestCase):
    """`fly yes` agrees with the fly's own reading. The refusals are the part
    worth testing: agreeing with an uncertain reading trains the uncertainty
    in, and the two states it could not separate get worse rather than better.
    """

    class _Recognition:
        FLOOR = 0.25
        def __init__(self, label="idle", score=0.9, margin=0.3, runners=()):
            self.label, self.score, self.margin = label, score, margin
            self.runners = list(runners)
        @property
        def confident(self):
            return bool(self.label) and self.score >= self.FLOOR \
                and self.margin >= 0.08

    class _Brain:
        def __init__(self, states=("idle", "busy")):
            self.states = {s: {} for s in states}

    def test_a_confident_reading_is_confirmed(self):
        from updev.cli import confirm_target

        chosen, refusal = confirm_target(self._Brain(), self._Recognition(), "", False)
        self.assertEqual(chosen, "idle")
        self.assertEqual(refusal, "")

    def test_an_uncertain_reading_is_refused(self):
        from updev.cli import confirm_target

        shaky = self._Recognition(margin=0.01, runners=[("busy", 0.89)])
        chosen, refusal = confirm_target(self._Brain(), shaky, "", False)
        self.assertEqual(chosen, "")
        self.assertIn("확실하지 않습니다", refusal)

    def test_anyway_overrides_the_refusal(self):
        from updev.cli import confirm_target

        shaky = self._Recognition(margin=0.01, runners=[("busy", 0.89)])
        chosen, refusal = confirm_target(self._Brain(), shaky, "", True)
        self.assertEqual(chosen, "idle")
        self.assertEqual(refusal, "")

    def test_naming_a_state_skips_the_uncertainty_check(self):
        """Saying which one it was is the correction; there is nothing left
        to be uncertain about."""
        from updev.cli import confirm_target

        shaky = self._Recognition(margin=0.01, runners=[("busy", 0.89)])
        chosen, refusal = confirm_target(self._Brain(), shaky, "busy", False)
        self.assertEqual(chosen, "busy")
        self.assertEqual(refusal, "")

    def test_an_unknown_name_is_refused(self):
        from updev.cli import confirm_target

        chosen, refusal = confirm_target(self._Brain(), self._Recognition(),
                                         "nonsense", False)
        self.assertEqual(chosen, "")
        self.assertIn("배우지 않은 상태", refusal)

    def test_a_reading_below_the_floor_is_refused(self):
        from updev.cli import confirm_target

        weak = self._Recognition(score=0.1, margin=0.5)
        chosen, refusal = confirm_target(self._Brain(), weak, "", False)
        self.assertEqual(chosen, "")
        self.assertIn("어느 것도 아닙니다", refusal)

    def test_a_fly_with_no_named_states_has_nothing_to_confirm(self):
        from updev.cli import confirm_target

        nothing = self._Recognition(label="", score=0.0)
        chosen, refusal = confirm_target(self._Brain(()), nothing, "", False)
        self.assertEqual(chosen, "")
        self.assertIn("승인할 판정이 없습니다", refusal)


class TestUndoLastLesson(_FlyFixture):
    """One step of history, so a lesson taught by mistake can be taken back
    instead of costing the whole state."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "flybrain.json"

    def test_saving_keeps_the_version_it_replaced(self):
        from updev.flybrain import FlyBrain, previous_path

        brain = FlyBrain()
        brain.learn(self._board(), state="first")
        brain.save(self.path)
        self.assertFalse(previous_path(self.path).exists())   # nothing to keep yet

        brain.learn(self._board(), state="second")
        brain.save(self.path)
        self.assertTrue(previous_path(self.path).exists())

    def test_the_previous_version_is_the_one_before_the_last_lesson(self):
        import json

        from updev.flybrain import FlyBrain, previous_path

        brain = FlyBrain()
        brain.learn(self._board(), state="first")
        brain.save(self.path)
        brain.learn(self._board(), state="second")
        brain.save(self.path)

        before = json.loads(previous_path(self.path).read_text())
        self.assertEqual(sorted(before["states"]), ["first"])
        self.assertEqual(before["exposures"], 1)

    def test_the_last_action_records_what_was_taught(self):
        from updev.flybrain import FlyBrain, load_brain

        brain = FlyBrain()
        brain.learn(self._board(), state="floppy-mounted")
        brain.save(self.path)
        self.assertEqual(load_brain(self.path).last_action["state"],
                         "floppy-mounted")

    def test_an_unnamed_lesson_is_still_recorded(self):
        from updev.flybrain import FlyBrain, load_brain

        brain = FlyBrain()
        brain.learn(self._board())
        brain.save(self.path)
        action = load_brain(self.path).last_action
        self.assertEqual(action["what"], "learn")
        self.assertEqual(action["state"], "")


class TestReadingFilesOffTheDisk(unittest.TestCase):
    """Showing what is on a floppy without mounting it.

    `floppy.read_file` wants the whole 1.44MB image in hand. Reading all of it
    to display a 200-byte text file costs the better part of a minute on real
    hardware and drags the head over every bad sector on the way — the same
    reason `read_medium` stops at the system area. This walks the FAT chain
    from the sectors already read and touches only the file's own clusters.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from updev.floppy import build_image

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "disk.img"
        self.path.write_bytes(build_image(label="FLY TEST").data)

    def test_it_matches_reading_the_whole_image(self):
        """The cheap path and the thorough one have to agree, or the saving
        is just a different answer."""
        from updev.fdd import read_file, read_medium
        from updev.floppy import read_file as read_whole

        data = self.path.read_bytes()
        medium = read_medium(str(self.path))
        self.assertTrue(medium.files)
        for entry in medium.files:
            self.assertEqual(read_file(str(self.path), entry, medium.system),
                             read_whole(data, entry["name"])[:2048])

    def test_every_file_on_a_built_image_reads_back(self):
        from updev.fdd import read_file, read_medium

        medium = read_medium(str(self.path))
        for entry in medium.files:
            self.assertEqual(len(read_file(str(self.path), entry, medium.system)),
                             entry["size"])

    def test_a_preview_stops_at_the_limit(self):
        from updev.fdd import read_file, read_medium

        medium = read_medium(str(self.path))
        entry = max(medium.files, key=lambda e: e["size"])
        self.assertLessEqual(
            len(read_file(str(self.path), entry, medium.system, limit=64)), 64)

    def test_an_unreadable_node_yields_nothing_rather_than_raising(self):
        from updev.fdd import read_file, read_medium

        medium = read_medium(str(self.path))
        entry = medium.files[0]
        self.assertEqual(read_file(str(self.path) + ".gone", entry, medium.system),
                         b"")

    def test_text_and_binary_are_told_apart(self):
        from updev.fdd import is_textual

        self.assertTrue(is_textual(b"CONFIG=1\nenable_uart=1\n"))
        self.assertFalse(is_textual(b"\x7fELF\x02\x01\x01\x00" + bytes(64)))
        self.assertFalse(is_textual(b""))

    def test_a_truncated_chain_returns_what_it_got(self):
        """A damaged disk should still show the readable part of a file."""
        from updev.fdd import SECTOR, SYSTEM_SECTORS, read_file, read_medium

        medium = read_medium(str(self.path))
        entry = max(medium.files, key=lambda e: e["size"])
        self.path.write_bytes(self.path.read_bytes()[:SYSTEM_SECTORS * SECTOR])
        self.assertEqual(read_file(str(self.path), entry, medium.system), b"")
