# Observability (Prometheus + Grafana)

Turns the routing service into a monitored system: it exposes Prometheus
metrics, Prometheus scrapes them, and Grafana shows them on a dashboard -
the real-time operations view a network team watches.

## What is measured

The service (`serving/app.py`) exposes `GET /metrics`:

| Metric | Meaning |
|---|---|
| `router_requests_total{endpoint,status}` | request count per endpoint and HTTP status |
| `router_request_latency_seconds{endpoint}` | request latency histogram (for p95, etc.) |
| `router_last_route_hops` | hops of the most recent computed route |
| `router_last_route_success` | whether the last route reached the destination |
| `router_last_detect_imminent` | imminent-loss links flagged in the last detect call |

Endpoint labels use the route template (`/route/{t0}`), not the raw path, so
cardinality stays bounded.

## Run

```bash
docker compose -f observability/docker-compose.yml up --build
# generate some load:
for i in $(seq 0 60 1140); do curl -s localhost:8000/route/$i >/dev/null; done
```

- Router:     http://localhost:8000  (`/metrics`)
- Prometheus: http://localhost:9090  (Status -> Targets shows `leo-router` UP)
- Grafana:    http://localhost:3000  (anonymous admin; dashboard **LEO Router Service**)

The Prometheus data source and the dashboard are provisioned automatically
from `grafana/provisioning/` and `grafana/dashboards/router.json` - nothing to
click to set up.

## Verified

Brought up end-to-end on Docker (colima) with Prometheus 2.55 and Grafana 11.4:

```
$ docker compose -f observability/docker-compose.yml ps
router       Up (healthy)   0.0.0.0:8000->8000
prometheus   Up             0.0.0.0:9090->9090
grafana      Up             0.0.0.0:3000->3000

$ curl -s localhost:8000/metrics | grep router_requests_total
router_requests_total{endpoint="/route/{t0}",status="200"} 20.0
router_requests_total{endpoint="/detect/{t0}",status="200"} 20.0

# Prometheus target
leo-router  up  http://router:8000/metrics

# Grafana -> Prometheus (datasource proxy)
via Grafana total requests = 52
# dashboard "LEO Router Service" provisioned with 5 panels
```

## Dashboard panels

- Request rate (req/s) by endpoint
- Request latency p95 by endpoint
- Imminent-loss links (last detect) - green/orange/red thresholds
- Last route hops
- Total requests

## Closed-loop metrics

The closed-loop controller ([`closedloop/`](../closedloop/)) exports its own
metrics on the host (port 9101); Prometheus scrapes them as job
`closedloop-controller`, and Grafana provisions a second dashboard,
**Closed Loop (detect -> drain)** (`grafana/dashboards/closedloop.json`).

| Metric | Meaning |
|---|---|
| `closedloop_detect_requests_total{result}` / `closedloop_detect_latency_seconds` | calls from the controller to the K8s-served `/detect` |
| `closedloop_detect_imminent_links` | imminent-loss links in the last detect call |
| `closedloop_link_flagged{link}` / `closedloop_link_flag_events_total{link}` | the replayed satellite link, mapped to lab link r1-r3, was flagged |
| `closedloop_link_drained{link}` / `closedloop_drain_actions_total{link}` | controller drained r1-r3 (OSPF cost raised on both ends) |
| `closedloop_link_failed{link}` | emulated failure applied to r1-r3 |
| `closedloop_probe_up`, `closedloop_probe_{replies,lost}_total` | live ping r1 -> r3 loopback |
| `closedloop_trial_{loss_ratio,outage_seconds,drain_lead_seconds}{mode,condition}` | result of the last trial |

```bash
# run Prometheus + Grafana without the compose router (the closed loop uses
# the router on kind); on colima the host is 192.168.5.2 from containers:
HOST_GATEWAY_IP=192.168.5.2 docker compose -f observability/docker-compose.yml up -d --no-deps prometheus grafana
```

With `--no-deps` the compose `router` is not started, so the `leo-router` target
shows *down*; the router on kind is reached through a NodePort that
load-balances across replicas, so it is not scraped per pod here (that needs
an in-cluster Prometheus).

Verified during the closed-loop runs: target `closedloop-controller` **up**,
`closedloop_*` series queryable in Prometheus and through the Grafana
datasource, dashboard provisioned.

## What this demonstrates

- Instrumenting a service with **application and domain metrics**, not just
  infrastructure counters.
- A complete **metrics -> scrape -> dashboard** pipeline, provisioned as code.
- The monitoring/anomaly view an operations team would keep on screen, wired
  to the same imminent-failure signal the router acts on.

## Files

```
observability/
  docker-compose.yml            router + prometheus + grafana
  prometheus/prometheus.yml     scrape config
  grafana/provisioning/         datasource + dashboard providers (as code)
  grafana/dashboards/router.json
  grafana/dashboards/closedloop.json   closed-loop dashboard (closedloop/)
```
