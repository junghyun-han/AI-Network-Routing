"""
Network routing policy as a service.

A small FastAPI app that serves the trained GCN-PPO routing policy: given a
network snapshot (identified by a start time), it returns the end-to-end
route and the proactive link-failure signal. Packaged for Docker and
Kubernetes (see the Dockerfile and k8s/).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from serving.policy_runner import Router

app = FastAPI(title="LEO Routing Policy Service", version="1.0")
_router: Router | None = None


def get_router() -> Router:
    global _router
    if _router is None:
        _router = Router(os.environ.get("MODEL_PATH",
                                        "serving/model/PPO-GCN_seed0.pt"))
    return _router


@app.get("/")
def root():
    return {"service": "LEO routing policy",
            "endpoints": ["/health", "/route/{t0}", "/detect/{t0}?tau_s=2.0"]}


@app.get("/health")
def health():
    # touch the model so readiness reflects a loadable policy
    get_router()
    return {"status": "ok"}


@app.get("/route/{t0}")
def route(t0: int):
    if not (0 <= t0 < 1200):
        raise HTTPException(status_code=400, detail="t0 must be in [0, 1200)")
    return get_router().route(t0)


@app.get("/detect/{t0}")
def detect(t0: int, tau_s: float = 2.0):
    if not (0 <= t0 < 1200):
        raise HTTPException(status_code=400, detail="t0 must be in [0, 1200)")
    return get_router().detect(t0, tau_s=tau_s)
