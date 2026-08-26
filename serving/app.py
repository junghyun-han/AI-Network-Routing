"""
Network routing policy as a service.

A small FastAPI app that serves the trained GCN-PPO routing policy: given a
network snapshot (identified by a start time), it returns the end-to-end
route and the proactive link-failure signal. Packaged for Docker and
Kubernetes (see the Dockerfile and k8s/) and instrumented for Prometheus
(GET /metrics, see observability/).
"""

from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException, Request
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)
from starlette.responses import Response

from serving.policy_runner import Router

app = FastAPI(title="LEO Routing Policy Service", version="1.0")
_router: Router | None = None

# -- Prometheus metrics -----------------------------------------------------
REQS = Counter("router_requests_total", "requests", ["endpoint", "status"])
LAT = Histogram("router_request_latency_seconds", "request latency", ["endpoint"])
G_HOPS = Gauge("router_last_route_hops", "hops of the last computed route")
G_SUCC = Gauge("router_last_route_success", "1 if the last route reached the destination")
G_IMM = Gauge("router_last_detect_imminent", "imminent-loss links in the last detect call")


def get_router() -> Router:
    global _router
    if _router is None:
        _router = Router(os.environ.get("MODEL_PATH",
                                        "serving/model/PPO-GCN_seed0.pt"))
    return _router


@app.middleware("http")
async def instrument(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    endpoint = getattr(route, "path", request.url.path)   # template, low cardinality
    LAT.labels(endpoint).observe(time.perf_counter() - start)
    REQS.labels(endpoint, response.status_code).inc()
    return response


@app.get("/")
def root():
    return {"service": "LEO routing policy",
            "endpoints": ["/health", "/route/{t0}", "/detect/{t0}?tau_s=2.0",
                          "/metrics"]}


@app.get("/health")
def health():
    get_router()
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/route/{t0}")
def route(t0: int):
    if not (0 <= t0 < 1200):
        raise HTTPException(status_code=400, detail="t0 must be in [0, 1200)")
    out = get_router().route(t0)
    G_HOPS.set(out["hops"])
    G_SUCC.set(1 if out["success"] else 0)
    return out


@app.get("/detect/{t0}")
def detect(t0: int, tau_s: float = 2.0):
    if not (0 <= t0 < 1200):
        raise HTTPException(status_code=400, detail="t0 must be in [0, 1200)")
    out = get_router().detect(t0, tau_s=tau_s)
    G_IMM.set(out["n_imminent"])
    return out
