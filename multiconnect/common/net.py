"""LAN helpers: find the commander without UDP (outbound-only subnet scan) and diagnose firewalls.

Why: on school PCs without admin rights Windows Firewall usually blocks INBOUND traffic for a
freshly installed python.exe. UDP discovery needs inbound on the worker; the subnet scan only
makes outbound TCP connections, so it still works there."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import subprocess
import sys

import httpx


def local_ipv4s() -> list[tuple[str, int]]:
    """[(ip, prefixlen)] of real LAN interfaces (no loopback / link-local)."""
    out: list[tuple[str, int]] = []
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and a.address and not a.address.startswith(("127.", "169.254.")):
                    try:
                        plen = ipaddress.IPv4Network(f"0.0.0.0/{a.netmask}").prefixlen if a.netmask else 24
                    except ValueError:
                        plen = 24
                    out.append((a.address, plen))
    except Exception:
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            out.append((s.getsockname()[0], 24))
            s.close()
        except OSError:
            pass
    return out


def scan_targets(max_hosts: int = 1024) -> list[str]:
    """Hosts to probe: own /24 first (or the whole subnet if it is small), nearest neighbours first."""
    # Several adapters (LAN + VPN like Radmin/Hamachi/Tailscale): private LAN ranges first, then the
    # nearest neighbours of every adapter interleaved; the TOTAL stays <= max_hosts.
    ifaces = sorted(local_ipv4s(), key=lambda x: (not ipaddress.IPv4Address(x[0]).is_private, x[0]))
    mine = {ipaddress.IPv4Address(ip) for ip, _ in ifaces}
    lists = []
    for ip, plen in ifaces:
        me = ipaddress.IPv4Address(ip)
        net = ipaddress.IPv4Network(f"{ip}/{max(plen, 22)}", strict=False)
        lists.append(sorted((h for h in net.hosts() if h not in mine), key=lambda h: abs(int(h) - int(me))))
    seen: set[str] = set()
    targets: list[str] = []
    i = 0
    while len(targets) < max_hosts and any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst) and len(targets) < max_hosts:
                s = str(lst[i])
                if s not in seen:
                    seen.add(s)
                    targets.append(s)
        i += 1
    return targets


async def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def scan_for_commander(port: int, token: str, timeout: float = 0.6, extra: list[str] | None = None,
                             limit: int = 200) -> str | None:
    """Return http://ip:port of a coordinator that accepts our team token, or None."""
    cands = list(dict.fromkeys((extra or []) + ["127.0.0.1"] + scan_targets()))
    sem = asyncio.Semaphore(limit)
    found: list[str] = []

    async def probe(ip: str) -> None:
        async with sem:
            if found or not await _tcp_open(ip, port, timeout):
                return
        url = f"http://{ip}:{port}"
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(url + "/api/ping")
                if r.status_code == 200 and r.json().get("name") == "multiconnect":
                    r2 = await c.get(url + "/api/w/accounts", headers={"x-team-token": token})
                    if r2.status_code == 200:
                        found.append(url)
        except Exception:
            pass

    await asyncio.gather(*(probe(ip) for ip in cands))
    return found[0] if found else None


def _ps(cmd: str, timeout: float = 15) -> str:
    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                           timeout=timeout, creationflags=0x08000000)
        return r.stdout.strip()
    except Exception:
        return ""


def network_profiles() -> str:
    """'Ethernet: Public' etc. Public networks block inbound connections by default."""
    return _ps("Get-NetConnectionProfile | ForEach-Object { $_.InterfaceAlias + ': ' + $_.NetworkCategory }")


def firewall_rules_for(exe: str) -> list[str]:
    """Firewall rules for this program (read-only, works without admin, language independent)."""
    exe = exe.replace("'", "''")
    out = _ps(f"Get-NetFirewallApplicationFilter -Program '{exe}' -ErrorAction SilentlyContinue | "
              "Get-NetFirewallRule -ErrorAction SilentlyContinue | ForEach-Object { "
              "\"$($_.Direction) $($_.Action) enabled=$($_.Enabled) profile=$($_.Profile) [$($_.DisplayName)]\" }",
              timeout=40)
    return [l for l in out.splitlines() if l.strip()]
