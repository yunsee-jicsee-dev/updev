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
