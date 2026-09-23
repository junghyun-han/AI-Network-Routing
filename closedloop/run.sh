#!/usr/bin/env bash
# End-to-end closed loop: FRR lab + policy service on kind Kubernetes +
# Prometheus/Grafana + controller experiment. Run from the repository root:
#   bash closedloop/run.sh            # 5 trials per mode and condition (~50 min)
#   TRIALS=1 bash closedloop/run.sh   # quick pass
#   EXPERIMENT_ARGS="--conditions silent-default --out /tmp/cl" bash closedloop/run.sh
# Needs: docker (colima or Docker Desktop), kind, kubectl, python3 with
# closedloop/requirements.txt installed.
set -euo pipefail
cd "$(dirname "$0")/.."
TRIALS="${TRIALS:-5}"
PY="${PYTHON:-python3}"

# 1. protocol lab (4 FRR routers)
docker image inspect netlab-frr:local >/dev/null 2>&1 || docker build -t netlab-frr:local netlab/frrimg
docker compose -f netlab/docker-compose.yml up -d

# 2. routing-policy service on kind Kubernetes (2 replicas behind a NodePort)
docker image inspect leo-router:latest >/dev/null 2>&1 || docker build -f serving/Dockerfile -t leo-router:latest .
kind get clusters | grep -qx leo || kind create cluster --name leo --config closedloop/k8s/kind-cluster.yaml
kind load docker-image leo-router:latest --name leo
kubectl --context kind-leo apply -f serving/k8s/deployment.yaml -f closedloop/k8s/router-nodeport.yaml
kubectl --context kind-leo rollout status deploy/leo-router --timeout=300s
until curl -sf localhost:30080/health >/dev/null; do sleep 2; done

# 3. Prometheus + Grafana (the controller is scraped on the host, port 9101).
#    colima: containers reach the macOS host at 192.168.5.2.
if docker context show 2>/dev/null | grep -q colima; then export HOST_GATEWAY_IP=192.168.5.2; fi
docker compose -f observability/docker-compose.yml up -d --no-deps prometheus grafana

# 4. wait for OSPF on the lab (the broadcast-network wait timer is 40 s)
until docker exec netlab-r1-1 vtysh -c 'show ip ospf neighbor' | grep -q 'Full.*10.0.13.3'; do sleep 3; done

# 5. the experiment (writes closedloop/results/)
"$PY" closedloop/scenario.py --check
# shellcheck disable=SC2086
"$PY" closedloop/experiment.py --trials "$TRIALS" --hold 30 ${EXPERIMENT_ARGS:-}

# tear down (images are kept):
#   docker compose -f observability/docker-compose.yml down
#   docker compose -f netlab/docker-compose.yml down
#   kind delete cluster --name leo
