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
```
