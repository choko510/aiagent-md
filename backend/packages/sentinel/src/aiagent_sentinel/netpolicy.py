"""Address vetting for egress (09 network-egress-and-ssrf). Pure functions, no I/O.

Every address a destination resolves to must be public unicast; one bad address rejects the whole destination
(no "pick the good one", which would let DNS answers steer us). IPv6 forms that embed IPv4 (v4-mapped, NAT64,
6to4, Teredo) are judged by the embedded IPv4 too. Exact private addresses can be allowlisted only per destination
and only outside production (development mock receivers).
"""

from __future__ import annotations

import ipaddress
import re

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_BLOCKED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
)]
_BLOCKED_V6 = [ipaddress.ip_network(n) for n in (
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
    "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]
_HOST = re.compile(r"\A(?=.{1,253}\Z)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")


class EgressDenied(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_host(host: str) -> str:
    """Lowercase ASCII hostname or literal IP; rejects userinfo, ports, IDN tricks, trailing-dot aliases, %-zones."""
    h = host.strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h.isascii() or any(ch in h for ch in "@/\\?#% \t") or h.endswith("."):
        raise EgressDenied("BAD_HOST")
    h = h.lower()
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if not _HOST.match(h) or h.replace(".", "").isdigit():  # "2130706433"-style integer IPs are not hostnames
        raise EgressDenied("BAD_HOST")
    return h


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return not any(ip in n for n in _BLOCKED_V4) and ip.is_global
    v4 = _embedded_v4(ip)
    if v4 is not None and not is_public(v4):
        return False
    return not any(ip in n for n in _BLOCKED_V6) and ip.is_global


def vet_addresses(addrs: list[str], *, allow_private: frozenset[str] = frozenset()) -> list[str]:
    """Return the vetted addresses (in resolver order) or raise EgressDenied. One bad address -> deny."""
    if not addrs:
        raise EgressDenied("DNS_EMPTY")
    out = []
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%", 1)[0])
        if str(ip) in allow_private:
            out.append(str(ip))
            continue
        if not is_public(ip):
            raise EgressDenied("PRIVATE_ADDRESS")
        out.append(str(ip))
    return out
