# Routing Policy Serving (Docker + Kubernetes)

Packages the trained GCN-PPO routing policy as a REST service and deploys it
on Kubernetes - the automation/AX side of network operations: the model runs
as a managed, self-healing, scalable service rather than a script.

## What it serves

| Endpoint | Purpose |
|---|---|
| `GET /health` | readiness/liveness probe (loads the policy) |
| `GET /route/{t0}` | run the policy from a start time; returns the end-to-end route with per-hop predicted contact time and imminent-failure flags |
| `GET /detect/{t0}?tau_s=2.0` | list live links predicted to drop within `tau_s` seconds (proactive anomaly signal) |

Example:

```json
GET /route/300
{"t0": 300, "path": [47, 12, 13, 57, 49, 25], "hops": 5,
 "reward": 7.0754, "success": true,
 "steps": [{"from": 47, "to": 12, "pred_contact_s": 20.0, "risky": false}, ...]}
```

## Run locally (Docker)

```bash
# from the repository root
docker build -f serving/Dockerfile -t leo-router:latest .
docker compose -f serving/docker-compose.yml up
curl localhost:8000/route/100
```

## Deploy on Kubernetes (kind)

```bash
kind create cluster --name leo
kind load docker-image leo-router:latest --name leo     # local image into the cluster
kubectl apply -f serving/k8s/deployment.yaml            # Deployment (2 replicas) + Service
kubectl rollout status deploy/leo-router
kubectl port-forward svc/leo-router 8080:80
curl localhost:8080/route/300
```

For the closed loop ([`closedloop/`](../closedloop/)), the cluster is created
with `closedloop/k8s/kind-cluster.yaml` (host port 30080 mapped into the node)
and `closedloop/k8s/router-nodeport.yaml` adds a NodePort Service, so the
controller on the host reaches the Service (kube-proxy load-balances across
both replicas) instead of a single pod via port-forward:

```bash
kind create cluster --name leo --config closedloop/k8s/kind-cluster.yaml
kind load docker-image leo-router:latest --name leo
kubectl apply -f serving/k8s/deployment.yaml -f closedloop/k8s/router-nodeport.yaml
curl localhost:30080/detect/14      # the controller polls this every second
```

Autoscaling (optional, needs metrics-server):

```bash
kubectl apply -f serving/k8s/hpa.yaml
```

## Verified deployment

Built and run end-to-end on Docker 29.7 (colima) + kind 0.32 / Kubernetes 1.36:

```
$ kubectl get deploy leo-router
leo-router   2/2   2   2

$ kubectl get pods -l app=leo-router
leo-router-...-6ttwq   1/1   Running
leo-router-...-d9lmj   1/1   Running

$ kubectl get svc leo-router
leo-router   ClusterIP   10.96.61.253   80/TCP

$ curl localhost:8080/health
{"status":"ok"}

$ kubectl scale deploy/leo-router --replicas=3   # scales cleanly to 3/3
```

- Image `leo-router:latest`, ~1 GB (CPU-only PyTorch).
- Runs as a non-root user, with HTTP readiness and liveness probes on
  `/health` and CPU/memory requests and limits set.
- The `Deployment` keeps the requested replicas healthy; scaling is a
  one-command change and the `HorizontalPodAutoscaler` automates it on CPU
  load.

## What this demonstrates

- **Containerization**: reproducible image, dependency isolation, non-root,
  health checks.
- **Orchestration**: Kubernetes `Deployment` + `Service` + `HPA`, probes,
  resource governance, horizontal scaling.
- **Model serving / automation**: an AI policy exposed as a managed network
  service, the shape of an AX operations tool.

## Files

```
serving/
  app.py               FastAPI service
  policy_runner.py     inference wrapper around the GCN-PPO policy
  requirements.txt
  Dockerfile           CPU PyTorch, non-root, healthcheck
  docker-compose.yml   local run
  k8s/
    deployment.yaml    Deployment (2 replicas) + Service
    hpa.yaml           HorizontalPodAutoscaler (CPU 70%, 2-5 replicas)
  model/               trained policy weights
```
