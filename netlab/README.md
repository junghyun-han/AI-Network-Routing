# BGP / OSPF Lab (FRRouting)

A running control-plane lab: four software routers speaking real **OSPF** and
**BGP**, to demonstrate the protocol-level side of routing that the AI work
abstracts away. Not a simulation of routing - actual FRR daemons exchanging
LSAs and BGP updates.

> Scope note: this is a hands-on **lab** built to show protocol configuration
> and convergence, not production operations experience.

## Topology

```
        OSPF area 0 (AS 65001)                 eBGP
    r1 ------------- r2                    r1 ===== r4  (AS 65002)
     \             /                       originates 203.0.113.0/24
      \           /
        --- r3 ---
   (ring -> a link failure reroutes)
```

- **OSPF** core `r1 r2 r3`, area 0, each advertising a loopback; the ring
  gives a backup path.
- **eBGP** between `r1` (AS 65001) and `r4` (AS 65002); `r4` originates a
  customer prefix, `r1` redistributes the OSPF routes into BGP.

## Run

```bash
docker build -t netlab-frr:local netlab/frrimg
docker compose -f netlab/docker-compose.yml up -d
docker exec netlab-r1-1 vtysh -c "show ip ospf neighbor"
docker exec netlab-r1-1 vtysh -c "show ip bgp summary"
```

## Verified

Brought up on Docker (colima) with FRR 8.4 (Debian bookworm, built natively
for arm64).

**OSPF adjacencies reach Full:**

```
$ docker exec netlab-r1-1 vtysh -c "show ip ospf neighbor"
Neighbor ID   State        Address     Interface
10.255.2.2    Full/DR      10.0.12.2   eth0
10.255.3.3    Full/DR      10.0.13.3   eth1
```

**eBGP session established, prefixes exchanged both ways:**

```
$ docker exec netlab-r1-1 vtysh -c "show ip bgp summary"
Neighbor      AS     Up/Down     State/PfxRcd   PfxSnt
10.0.14.4     65002  00:00:54    1              4

$ docker exec netlab-r4-1 vtysh -c "show ip bgp"      # r4 learns the core loopbacks
*> 10.255.2.2/32   10.0.14.1   ... 65001 ?
*> 10.255.3.3/32   10.0.14.1   ... 65001 ?
*> 203.0.113.0/24  0.0.0.0     ... i           # and originates its own prefix

$ docker exec netlab-r1-1 vtysh -c "show ip route"    # r1: OSPF + BGP routes
O>* 10.255.2.2/32 [110/10] via 10.0.12.2, eth0
O>* 10.255.3.3/32 [110/10] via 10.0.13.3, eth1
B>* 203.0.113.0/24 [20/0]  via 10.0.14.4, eth2
```

**Redundancy - OSPF reconverges on a link failure:**

```
# before: r1 -> r3 loopback is the direct link (metric 10)
  * 10.0.13.3, via eth1

$ docker exec netlab-r1-1 ip link set eth1 down       # break the r1-r3 link
# after ~10s: rerouted through r2 (metric 20)
  Known via "ospf", distance 110, metric 20, best
  * 10.0.12.2, via eth0
```

## What this demonstrates

- Configuring and reading **OSPF** (areas, adjacencies, LSAs) and **BGP**
  (eBGP peering, AS paths, redistribution).
- **IGP + EGP together**: OSPF inside the AS, BGP between AS, redistribution
  at the boundary.
- **Convergence and redundancy** under a link failure - the core of network
  stability work.

## Note on the image

The official `frrouting/frr` image is amd64-only and its `zebra` daemon
segfaults under emulation on Apple Silicon. `frrimg/` builds FRR from the
Debian package so it runs natively on arm64 (and amd64).

## Files

```
netlab/
  docker-compose.yml   4 routers + 4 point-to-point link networks
  daemons              which FRR daemons run (zebra, ospfd, bgpd)
  r1..r4/frr.conf      per-router OSPF/BGP configuration
  frrimg/              native FRR image (Debian-based)
```
