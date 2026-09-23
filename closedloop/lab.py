"""
Driver for the netlab FRR routers (netlab/docker-compose.yml), via docker exec.

Everything the closed loop does to the lab goes through here:

    drain / undrain   OSPF interface cost on a link (vtysh)
    fail / restore    emulated link failure:
                        silent  - tc netem loss 100% on the link interface.
                                  Carrier stays up, packets and OSPF hellos
                                  vanish; routers only notice through the
                                  OSPF dead interval (a link that fades out).
                        carrier - ip link set down. The kernel reports carrier
                                  loss and FRR reacts at once (a cable pull).
    timers            OSPF hello/dead interval on the link (runtime only)
    probes            route lookups and OSPF neighbor state

Actions return the router's clock (the colima VM clock, shared by all
containers and by ping -D timestamps) right after the action completed.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional


class Lab:
    def __init__(self, project: str = "netlab"):
        self.project = project
        self._iface: Dict[tuple, str] = {}

    # -- plumbing ------------------------------------------------------------

    def ctr(self, router: str) -> str:
        return f"{self.project}-{router}-1"

    def sh(self, router: str, cmd: str, check: bool = True,
           timeout: float = 20) -> str:
        r = subprocess.run(["docker", "exec", self.ctr(router), "sh", "-c", cmd],
                           capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{router}: {cmd!r} -> {r.returncode}: "
                               f"{r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def now(self, router: str = "r1") -> float:
        return float(self.sh(router, "date +%s.%N").strip())

    def iface_on(self, router: str, subnet: str) -> str:
        """Interface of `router` that has an address inside `subnet`
        (docker assigns eth0/eth1/... by attach order, so never hard-code)."""
        key = (router, subnet)
        if key not in self._iface:
            net = ipaddress.ip_network(subnet)
            for line in self.sh(router, "ip -o -4 addr show").splitlines():
                m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
                if m and ipaddress.ip_interface(m.group(2)).ip in net:
                    self._iface[key] = m.group(1)
                    break
            else:
                raise RuntimeError(f"{router} has no interface in {subnet}")
        return self._iface[key]

    @staticmethod
    def parallel(fn, items: Iterable) -> List:
        items = list(items)
        with ThreadPoolExecutor(max_workers=len(items)) as ex:
            return list(ex.map(fn, items))

    def _vtysh_iface(self, router: str, iface: str, lines: List[str]) -> float:
        args = " ".join(f"-c '{c}'" for c in ["conf t", f"interface {iface}", *lines])
        return float(self.sh(router, f"vtysh {args} >/dev/null && date +%s.%N").strip())

    # -- actions -------------------------------------------------------------

    def set_cost(self, router: str, subnet: str, cost: Optional[int]) -> float:
        """cost=None restores the default (10 for these links)."""
        iface = self.iface_on(router, subnet)
        line = f"ip ospf cost {int(cost)}" if cost is not None else "no ip ospf cost"
        return self._vtysh_iface(router, iface, [line])

    def set_timers(self, router: str, subnet: str,
                   hello: Optional[int], dead: Optional[int]) -> float:
        iface = self.iface_on(router, subnet)
        if hello is None:
            lines = ["no ip ospf dead-interval", "no ip ospf hello-interval"]
        else:
            lines = [f"ip ospf hello-interval {int(hello)}",
                     f"ip ospf dead-interval {int(dead)}"]
        return self._vtysh_iface(router, iface, lines)

    def fail(self, router: str, subnet: str, kind: str,
             probe_dst: Optional[str] = None) -> Dict:
        """Break the link at this end. Returns the clock just after the
        failure and, if `probe_dst` is given, the interface this router was
        using towards it in the instant before (did traffic already move?)."""
        iface = self.iface_on(router, subnet)
        if kind == "silent":
            act = f"tc qdisc replace dev {iface} root netem loss 100%"
        elif kind == "carrier":
            act = f"ip link set {iface} down"
        else:
            raise ValueError(kind)
        pre = f"ip route get {probe_dst}; " if probe_dst else ""
        out = self.sh(router, f"{pre}{act} && date +%s.%N").strip().splitlines()
        m = re.search(r"\bdev\s+(\S+)", " ".join(out[:-1]))
        return {"ts": float(out[-1]), "iface": iface,
                "dev_before": m.group(1) if m else ""}

    def restore(self, router: str, subnet: str) -> None:
        iface = self.iface_on(router, subnet)
        self.sh(router, f"tc qdisc del dev {iface} root 2>/dev/null; "
                        f"ip link set {iface} up", check=False)

    # -- state ---------------------------------------------------------------

    def route_dev(self, router: str, dst: str) -> str:
        out = self.sh(router, f"ip route get {dst}", check=False)
        m = re.search(r"\bdev\s+(\S+)", out)
        return m.group(1) if m else ""

    def neighbor_full(self, router: str, subnet: str) -> bool:
        iface = self.iface_on(router, subnet)
        out = self.sh(router, "vtysh -c 'show ip ospf neighbor'", check=False)
        return any(("Full" in l and f"{iface}:" in l) for l in out.splitlines())

    def ospf_timers(self, router: str, subnet: str) -> str:
        iface = self.iface_on(router, subnet)
        out = self.sh(router, f"vtysh -c 'show ip ospf interface {iface}'")
        m = re.search(r"Hello (\d+)s, Dead (\d+)s", out)
        return f"hello {m.group(1)}s / dead {m.group(2)}s" if m else "?"
