"""Network interfaces, and the neighbours you can reach from them.

Two backends live here:

  `net`  — always runs. Interfaces, addresses, link state, Wi-Fi quality,
           error counters, the default route.
  `lan`  — slow, so it only runs with --deep or `updev net scan`. Sweeps the
           local subnet with concurrent pings, then reads the kernel's own ARP
           table for the MACs. No raw sockets, no root, no scapy.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from ..core.model import Device, Kind, Severity, Status
from ..core.registry import Backend, ProbeContext
from ..core.util import (
    glob,
    have,
    human_bytes,
    is_locally_administered,
    mac_vendor,
    read_int,
    read_text,
    run,
    run_ok,
)

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:                                   # pragma: no cover
    psutil = None                                     # type: ignore[assignment]
    HAVE_PSUTIL = False

_WELL_KNOWN_PORTS = {
    22: "ssh", 80: "http", 443: "https", 445: "smb", 139: "netbios",
    548: "afp", 631: "ipp", 3389: "rdp", 5000: "upnp/http", 8080: "http-alt",
    9100: "printer", 1883: "mqtt", 5432: "postgres", 3306: "mysql",
    6379: "redis", 8123: "home-assistant", 32400: "plex", 53: "dns",
}


# ==========================================================================
# interfaces
# ==========================================================================

class NetworkBackend(Backend):
    name = "net"
    title = "Network"
    kinds = (Kind.NET_IFACE,)

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not glob("/sys/class/net/*"):
            return False, "no network interfaces in sysfs"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        devices: list[Device] = []
        stats = psutil.net_if_stats() if HAVE_PSUTIL else {}
        addrs = psutil.net_if_addrs() if HAVE_PSUTIL else {}
        io = psutil.net_io_counters(pernic=True) if HAVE_PSUTIL else {}
        default_ifaces = _default_routes()

        for path in sorted(glob("/sys/class/net/*")):
            iface = path.name
            devices.append(
                self._interface(
                    iface, path, stats.get(iface), addrs.get(iface, []),
                    io.get(iface), iface in default_ifaces,
                )
            )

        resolv = self._dns()
        if resolv:
            devices.append(resolv)
        return devices

    def _interface(self, iface, path, stat, addrs, counters, is_default) -> Device:
        operstate = read_text(path / "operstate")
        carrier = read_int(path / "carrier")
        mac = read_text(path / "address")
        kind = _iface_kind(iface, path)

        dev = Device(
            uid=f"net:{iface}",
            kind=Kind.NET_IFACE,
            name=iface,
            bus="net",
            address=mac,
            node=str(path),
            parent="host:board",
        )
        dev.detail["type"] = kind
        dev.detail["state"] = operstate
        dev.detail["mac"] = mac
        if mac and not is_locally_administered(mac):
            vendor = mac_vendor(mac)
            if vendor:
                # Detail only, not `vendor` — the label should stay "eth0",
                # which is what you type, not "Raspberry Pi (Trading) Ltd eth0".
                dev.detail["mac_vendor"] = vendor
        mtu = read_int(path / "mtu")
        if mtu:
            dev.detail["mtu"] = mtu

        v4 = [a.address for a in addrs if getattr(a, "family", None) == socket.AF_INET]
        v6 = [
            a.address.split("%")[0]
            for a in addrs
            if getattr(a, "family", None) == socket.AF_INET6
        ]
        netmasks = {
            a.address: a.netmask
            for a in addrs
            if getattr(a, "family", None) == socket.AF_INET and a.netmask
        }
        if v4:
            dev.detail["ipv4"] = [
                f"{ip}/{_prefix(netmasks.get(ip))}" if netmasks.get(ip) else ip for ip in v4
            ]
        if v6:
            dev.detail["ipv6"] = v6[:4]

        speed = read_int(path / "speed")
        if speed and speed > 0:
            dev.detail["link_speed"] = f"{speed} Mbps"
            dev.metrics["link_mbps"] = float(speed)
        duplex = read_text(path / "duplex")
        if duplex:
            dev.detail["duplex"] = duplex

        if kind == "wireless":
            self._wifi(dev, iface)
        if kind in ("bridge",):
            members = [p.name for p in glob(f"/sys/class/net/{iface}/brif/*")]
            if members:
                dev.detail["bridge_members"] = members

        if counters:
            dev.detail["rx"] = human_bytes(counters.bytes_recv)
            dev.detail["tx"] = human_bytes(counters.bytes_sent)
            dev.metrics["rx_bytes"] = float(counters.bytes_recv)
            dev.metrics["tx_bytes"] = float(counters.bytes_sent)
            errs = counters.errin + counters.errout
            drops = counters.dropin + counters.dropout
            if errs:
                dev.detail["errors"] = errs
                dev.issue(Severity.WARN, f"{errs} interface errors since boot")
            if drops > 1000:
                dev.detail["drops"] = drops
                dev.issue(Severity.INFO, f"{drops} dropped packets since boot")

        if is_default:
            dev.tags.append("default-route")
            dev.detail["default_route"] = "yes"

        # status
        if iface == "lo":
            dev.status = Status.ONLINE
            dev.tags.append("loopback")
        elif operstate == "up":
            dev.status = Status.ONLINE if v4 or v6 else Status.DEGRADED
            if not (v4 or v6):
                dev.issue(Severity.WARN, "link is up but has no IP address")
        elif operstate == "down":
            dev.status = Status.IDLE if carrier == 0 else Status.DEGRADED
        elif operstate == "unknown":
            dev.status = Status.ONLINE if (v4 or v6) else Status.IDLE
        else:
            dev.status = Status.UNKNOWN

        bits = [kind, operstate]
        if v4:
            bits.append(dev.detail["ipv4"][0])
        if dev.detail.get("ssid"):
            bits.append(f"“{dev.detail['ssid']}”")
        if dev.detail.get("signal_dbm"):
            bits.append(f"{dev.detail['signal_dbm']}")
        if dev.detail.get("link_speed"):
            bits.append(dev.detail["link_speed"])
        dev.summary = " · ".join(b for b in bits if b)

        if v4 and dev.status == Status.ONLINE and kind in ("ethernet", "wireless"):
            dev.act("scan", "Sweep this subnet for neighbours", f"updev net scan --iface {iface}")
        return dev

    def _wifi(self, dev: Device, iface: str) -> None:
        dev.tags.append("wireless")
        if not have("iw"):
            return
        link = run_ok(["iw", "dev", iface, "link"], timeout=4)
        if not link or "Not connected" in link:
            dev.detail["wifi"] = "not associated"
            return
        for key, pattern in (
            ("ssid", r"SSID:\s*(.+)"),
            ("bssid", r"Connected to ([0-9a-f:]{17})"),
            ("freq", r"freq:\s*(\d+)"),
            ("tx_bitrate", r"tx bitrate:\s*(.+)"),
            ("rx_bitrate", r"rx bitrate:\s*(.+)"),
        ):
            m = re.search(pattern, link)
            if m:
                dev.detail[key] = m.group(1).strip()
        m = re.search(r"signal:\s*(-?\d+)\s*dBm", link)
        if m:
            dbm = int(m.group(1))
            dev.metrics["signal_dbm"] = float(dbm)
            dev.detail["signal_dbm"] = f"{dbm} dBm ({_signal_quality(dbm)})"
            if dbm <= -75:
                dev.issue(
                    Severity.WARN,
                    f"weak Wi-Fi signal ({dbm} dBm)",
                    fix="Move closer to the AP, or use the 2.4GHz band for range.",
                )
        freq = dev.detail.get("freq")
        if freq:
            band = "5 GHz" if int(freq) > 3000 else "2.4 GHz"
            dev.detail["band"] = band
        if dev.detail.get("bssid"):
            vendor = mac_vendor(dev.detail["bssid"])
            if vendor:
                dev.detail["ap_vendor"] = vendor

    def _dns(self) -> Device | None:
        servers = re.findall(r"^nameserver\s+(\S+)", read_text("/etc/resolv.conf"), re.M)
        if not servers:
            return None
        dev = Device(
            uid="net:dns",
            kind=Kind.NET_IFACE,
            name="DNS resolvers",
            status=Status.ONLINE,
            bus="net",
            parent="host:board",
        )
        dev.detail["nameservers"] = servers
        search = re.findall(r"^search\s+(.+)", read_text("/etc/resolv.conf"), re.M)
        if search:
            dev.detail["search"] = search
        dev.summary = ", ".join(servers)
        return dev


# ==========================================================================
# LAN sweep
# ==========================================================================

class LanBackend(Backend):
    name = "lan"
    title = "LAN neighbours"
    kinds = (Kind.NET_HOST,)
    slow = True

    def available(self, ctx: ProbeContext) -> tuple[bool, str]:
        if not have("ping"):
            return False, "the `ping` binary is required for the sweep"
        return True, ""

    def probe(self, ctx: ProbeContext) -> list[Device]:
        target, iface = self._target(ctx)
        if not target:
            return []
        hosts = list(target.hosts())
        if len(hosts) > 4096:
            raise RuntimeError(
                f"{target} has {len(hosts)} addresses — refusing to sweep. "
                "Pass a smaller --cidr."
            )

        t0 = time.perf_counter()
        alive = asyncio.run(self._sweep(hosts, ctx))
        # ARP entries are populated as a side effect of the sweep, so read after.
        arp = _arp_table()
        elapsed = time.perf_counter() - t0

        self_addrs = _own_addresses()
        found = sorted(set(alive) | {ip for ip in arp if ipaddress.ip_address(ip) in target})

        names = self._resolve(found, ctx) if ctx.resolve_names else {}
        ports = asyncio.run(self._port_scan(found, ctx)) if ctx.deep else {}

        devices = [
            self._neighbour(ip, arp.get(ip, ""), names.get(ip, ""), ports.get(ip, []),
                            iface, ip in self_addrs, ip in alive)
            for ip in found
        ]

        summary = Device(
            uid="lan:subnet",
            kind=Kind.NET_HOST,
            name=str(target),
            status=Status.ONLINE,
            bus="lan",
            address=str(target),
            parent=f"net:{iface}" if iface else "host:board",
            summary=f"{len(devices)} host(s) on {target} · swept in {elapsed:.1f}s",
        )
        summary.detail["subnet"] = str(target)
        summary.detail["addresses_probed"] = len(hosts)
        summary.detail["responded"] = len(alive)
        summary.detail["from_arp_cache"] = len(arp)
        summary.detail["interface"] = iface
        summary.metrics["hosts"] = float(len(devices))
        devices.insert(0, summary)
        return devices

    # ----------------------------------------------------------------------

    def _target(self, ctx: ProbeContext) -> tuple[ipaddress.IPv4Network | None, str]:
        if ctx.lan_cidr:
            try:
                return ipaddress.ip_network(ctx.lan_cidr, strict=False), ""
            except ValueError as e:
                raise RuntimeError(f"bad --cidr {ctx.lan_cidr!r}: {e}") from e

        # Prefer the interface carrying the default route.
        preferred = _default_routes()
        best: tuple[ipaddress.IPv4Network, str] | None = None
        if HAVE_PSUTIL:
            for iface, addrs in psutil.net_if_addrs().items():
                if iface == "lo" or iface.startswith(("docker", "br-", "veth")):
                    continue
                for a in addrs:
                    if getattr(a, "family", None) != socket.AF_INET or not a.netmask:
                        continue
                    try:
                        net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                    except ValueError:
                        continue
                    if net.num_addresses > 4096:
                        continue
                    if iface in preferred:
                        return net, iface
                    best = best or (net, iface)
        return (best[0], best[1]) if best else (None, "")

    async def _sweep(self, hosts, ctx: ProbeContext) -> set[str]:
        sem = asyncio.Semaphore(max(8, ctx.lan_concurrency))
        alive: set[str] = set()

        async def ping(ip: str) -> None:
            async with sem:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ping", "-c", "1", "-W", "1", "-n", "-q", ip,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    rc = await asyncio.wait_for(proc.wait(), timeout=3)
                except (asyncio.TimeoutError, OSError):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                if rc == 0:
                    alive.add(ip)

        await asyncio.gather(*(ping(str(h)) for h in hosts))
        return alive

    async def _port_scan(self, ips: list[str], ctx: ProbeContext) -> dict[str, list[int]]:
        """Plain TCP connects. Cheap, unprivileged, and honest about what it is."""
        sem = asyncio.Semaphore(max(16, ctx.lan_concurrency))
        out: dict[str, list[int]] = {ip: [] for ip in ips}

        async def probe(ip: str, port: int) -> None:
            async with sem:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port), timeout=1.0
                    )
                except (asyncio.TimeoutError, OSError):
                    return
                out[ip].append(port)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        await asyncio.gather(
            *(probe(ip, port) for ip in ips for port in ctx.lan_ports)
        )
        return {ip: sorted(ports) for ip, ports in out.items()}

    @staticmethod
    def _resolve(ips: list[str], ctx: ProbeContext) -> dict[str, str]:
        """Reverse DNS, in threads — getnameinfo has no async form."""
        names: dict[str, str] = {}
        if not ips:
            return names
        socket.setdefaulttimeout(1.0)

        def lookup(ip: str) -> tuple[str, str]:
            try:
                return ip, socket.gethostbyaddr(ip)[0]
            except (OSError, socket.herror):
                return ip, ""

        with ThreadPoolExecutor(max_workers=min(32, len(ips))) as pool:
            for ip, name in pool.map(lookup, ips):
                if name:
                    names[ip] = name
        return names

    @staticmethod
    def _neighbour(ip, mac, hostname, ports, iface, is_self, responded) -> Device:
        dev = Device(
            uid=f"lan:{ip}",
            kind=Kind.NET_HOST,
            name=hostname or ip,
            status=Status.ONLINE if responded else Status.IDLE,
            bus="lan",
            address=ip,
            parent="lan:subnet",
        )
        dev.detail["ip"] = ip
        if hostname:
            dev.detail["hostname"] = hostname
        if mac:
            dev.detail["mac"] = mac
            if is_locally_administered(mac):
                dev.detail["mac_note"] = "locally administered (randomised or virtual)"
            else:
                vendor = mac_vendor(mac)
                if vendor:
                    dev.vendor = vendor
                    dev.detail["vendor"] = vendor
        if ports:
            dev.detail["open_ports"] = [
                f"{p} ({_WELL_KNOWN_PORTS[p]})" if p in _WELL_KNOWN_PORTS else str(p)
                for p in ports
            ]
        if is_self:
            dev.tags.append("this-host")
        if not responded:
            dev.detail["source"] = "ARP cache only — did not answer ping"
            dev.tags.append("arp-only")

        bits = []
        if dev.vendor:
            bits.append(dev.vendor)
        if mac:
            bits.append(mac)
        if ports:
            bits.append(", ".join(_WELL_KNOWN_PORTS.get(p, str(p)) for p in ports[:4]))
        if is_self:
            bits.append("(this Pi)")
        dev.summary = " · ".join(bits) or ("responded to ping" if responded else "in ARP cache")
        return dev


# ==========================================================================
# helpers
# ==========================================================================

def _arp_table() -> dict[str, str]:
    """IP -> MAC from /proc/net/arp, skipping incomplete entries."""
    out: dict[str, str] = {}
    for line in read_text("/proc/net/arp").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, mac = parts[0], parts[2], parts[3]
        if mac == "00:00:00:00:00:00" or flags == "0x0":
            continue
        out[ip] = mac
    return out


def _own_addresses() -> set[str]:
    if not HAVE_PSUTIL:
        return set()
    return {
        a.address
        for addrs in psutil.net_if_addrs().values()
        for a in addrs
        if getattr(a, "family", None) == socket.AF_INET
    }


def _default_routes() -> set[str]:
    """Interfaces with a default route, straight from /proc/net/route."""
    ifaces: set[str] = set()
    for line in read_text("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "00000000":
            ifaces.add(parts[0])
    if not ifaces and have("ip"):
        for line in run_ok(["ip", "-4", "route", "show", "default"], timeout=3).splitlines():
            m = re.search(r"\bdev\s+(\S+)", line)
            if m:
                ifaces.add(m.group(1))
    return ifaces


def _iface_kind(iface: str, path) -> str:
    if iface == "lo":
        return "loopback"
    if (path / "wireless").exists() or (path / "phy80211").exists():
        return "wireless"
    if (path / "bridge").is_dir():
        return "bridge"
    if (path / "tun_flags").exists():
        return "tun/tap"
    if iface.startswith("veth"):
        return "veth (container)"
    if iface.startswith("docker"):
        return "docker bridge"
    if iface.startswith(("wg", "tail")):
        return "vpn"
    devtype = read_text(path / "uevent")
    m = re.search(r"DEVTYPE=(\S+)", devtype)
    if m:
        return m.group(1)
    return "ethernet"


def _prefix(netmask: str | None) -> int:
    if not netmask:
        return 0
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except ValueError:
        return 0


def _signal_quality(dbm: int) -> str:
    if dbm >= -50:
        return "excellent"
    if dbm >= -60:
        return "good"
    if dbm >= -70:
        return "fair"
    if dbm >= -80:
        return "weak"
    return "very weak"
