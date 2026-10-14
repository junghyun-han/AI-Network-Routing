# AI 기반 네트워크 라우팅·운용 시스템

![CI](https://github.com/junghyun-han/AI-Network-Routing/actions/workflows/ci.yml/badge.svg)

토폴로지가 계속 바뀌는 네트워크에서 강화학습 라우팅 정책을 **운용 가능한 시스템**으로 구현한 프로젝트. 장애 선제
탐지, 최적해 대비 검증, 컨테이너 배포, 관측, 기존 라우팅 프로토콜 연동까지 운용 단계 전체를 코드와 수치로 확인.

- **문제**: 링크가 수시로 생기고 끊기는 망에서는 경로를 잘 고르는 것만으로 부족. 장애를 미리 알고, 서비스로
  배포하고, 상태를 관측하고, 기존 프로토콜과 연동해야 실제 운용 가능
- **접근**: 라우팅 정책 위에 AI·데이터 계층(탐지, 검증, 분석), 플랫폼 계층(배포, 관측, IaC, CI), 폐루프(탐지 → 우회)를
  차례로 추가
- **검증 환경**: 대규모 저궤도(LEO) 위성 메시망 시뮬레이터, 라우터 4대 에뮬레이션 랩

| 운용 질문 | 방법 | 결과 |
|---|---|---|
| 곧 끊길 링크를 미리 알 수 있는가 | 임박 단절 탐지 | ROC-AUC **0.840** (거리 기준 0.661) |
| 강화학습 경로가 최적해에 얼마나 가까운가 | OR-Tools CP-SAT 최적해와 비교 | optimality gap **1.83%** |
| 서비스로 배포·확장·관측할 수 있는가 | FastAPI · Docker · Kubernetes, Prometheus · Grafana | kind 클러스터 배포, 레플리카 2→3 확장 확인, HPA 구성 |
| 탐지를 실제 우회로 이어 장애를 줄일 수 있는가 | 탐지 → OSPF 비용 조정 폐루프 | 조용한 단절 시 단절 시간 34.2 s → **0 s** |

<p align="center">
  <img src="analysis/figures/fig_quality_stability.png" width="46%" alt="품질-안정성 tradeoff: 제안 정책이 최적해 근처에서 안정+효율">
  <img src="anomaly/figures/fig_roc.png" width="46%" alt="링크 단절 선제 탐지 ROC (AUC 0.840)">
</p>
<p align="center"><sub>제안 라우팅 정책이 최적해(Oracle) 근처의 안정·효율 영역에 위치(좌) · 링크 단절을 사전 탐지 ROC-AUC 0.840(우)</sub></p>

전체 구조와 인터페이스는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 라우팅 지능 (AI·데이터)

| 기능 | 결과 | 위치 |
|---|---|---|
| 라우팅 정책 품질·트래픽 분석 | 곧 끊길 링크 사용 **0.0%** (최단경로 30~34%), 최적해 근접 | [`analysis/`](analysis/) |
| 링크 장애 선제 탐지 | 임박 단절 탐지 **ROC-AUC 0.840**, 거리 기반 대비 +0.179 | [`anomaly/`](anomaly/) |
| 최적해 대비 성능 검증 (OR-Tools) | optimality gap **1.83%**, CP-SAT 최적해 40/40 일치 | [`solver/`](solver/) |
| E2E 트래픽 가시성 | 연결성·지연·병목 링크·경로 다양성(독립경로 6) | [`traffic/`](traffic/) |
| 링크 장애 영향도 분석 | 링크별 중요도 순위, 경로상 링크 1개 장애 시 경로 평균 8.9km 증가 | [`impact/`](impact/) |

## 플랫폼·운용 (인프라)

| 기능 | 결과 | 위치 |
|---|---|---|
| 컨테이너 서빙 · **Kubernetes** 배포 | 라우팅 정책 REST 서빙, kind 클러스터 배포, 레플리카 2→3 확장 확인, HPA 자동 확장 구성 | [`serving/`](serving/) |
| **Prometheus + Grafana** 관측 | `/metrics` → 스크레이프 → 대시보드, 실기동 검증 | [`observability/`](observability/) |
| **BGP/OSPF** 프로토콜 라우팅 (FRR) | OSPF Full 인접 · eBGP 수렴 · 링크장애 재수렴 | [`netlab/`](netlab/) |
| **Terraform / EKS** 클라우드 IaC | VPC+EKS 정의, `terraform validate` 통과 | [`terraform/`](terraform/) |
| **CI/CD** (GitHub Actions) | push마다 실험 검증 + 컨테이너 빌드·기동 | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |
| **폐루프** (탐지 → 선제 우회) | K8s 서빙 탐지 → OSPF 비용 조정, 조용한 단절 시 단절시간 34.2 s → **0 s** | [`closedloop/`](closedloop/) |

방법·수치·검증 결과는 각 폴더의 `README.md`에 정리.

## 폐루프: AI 탐지 → OSPF 선제 우회

Kubernetes(kind)에 배포한 라우팅 정책의 임박 단절 탐지(`GET /detect`)를 컨트롤러가 매초
호출하고, 감시 중인 링크가 탐지되면 FRR 랩에서 그 링크의 OSPF 비용을 양 끝에서 올려
링크가 끊기기 **전에** 트래픽을 우회. 관측(Prometheus·Grafana), 이상(임박 단절) 탐지,
자동 경로 복구가 하나의 흐름으로 동작. 손실은 실제 패킷(ping 0.1 s 간격)으로 측정.

| 링크 장애 유형 (OSPF hello/dead) | 반응형 (OSPF 단독) | 선제형 (AI 탐지 → OSPF 우회) |
|---|---|---|
| 조용한 단절, 기본 타이머 10/40 s | 단절 34.2 ± 2.6 s · 손실 57.0% | **0 s · 0%** (5/5회) |
| 조용한 단절, 튜닝 타이머 1/4 s | 단절 3.5 ± 0.4 s · 손실 5.8% | **0 s · 0%** (5/5회) |
| 캐리어 다운 (인터페이스 down) | 0 s (0.1 s 해상도에서 손실 없음) | 0 s (차이 없음) |

<sub>손실 = 장애 전후 60초 창에서 응답이 없던 시간 비율. 각 5회 반복.</sub>

> 에뮬레이션: 시뮬레이터의 위성 링크 1개(26–44, 탐지 리드타임 2 s)의 수명을
> 4대 라우터 랩의 r1–r3 링크에 실시간(1:1)으로 재생. 상용망·실제 위성 아님.
> 이득은 반응형 장애 감지 지연을 없앤 만큼(캐리어 다운처럼 즉시 감지되는 장애에선 차이 없음).
> 방법·한계는 [`closedloop/README.md`](closedloop/README.md).

---

## 빠르게 보기

```bash
# 라우팅 지능
pip install -r requirements.txt
python analysis/ops_analysis.py --episodes data/episodes_all_v2.csv --out analysis
python anomaly/detect.py --out anomaly
python solver/optimality_gap.py --episodes data/episodes_all_v2.csv --out solver
python traffic/visibility.py --out traffic
python impact/criticality.py --out impact

# 플랫폼·운용 (Docker)
docker compose -f serving/docker-compose.yml up --build         # 라우팅 서빙
docker compose -f observability/docker-compose.yml up --build   # 관측 스택
docker compose -f netlab/docker-compose.yml up -d               # BGP/OSPF 라우팅
bash closedloop/run.sh                                           # 폐루프 (랩 + kind + 관측 + 실험)
# Kubernetes 배포: serving/README.md   ·   클라우드 IaC: terraform/README.md
```

---

## 구조

```
AI-Network-Routing/
  analysis/       라우팅 품질·트래픽 분석
  anomaly/        링크 장애 선제 탐지 (ROC/PR)
  solver/         OR-Tools 최적해 대비 성능 검증
  traffic/        E2E 트래픽 가시성 (networkx)
  impact/         링크 장애 영향도·중요도
  serving/        컨테이너 서빙 + Kubernetes 배포 (FastAPI)
  observability/  Prometheus + Grafana 관측
  netlab/         BGP/OSPF 라우팅 (FRRouting)
  closedloop/     AI 탐지 → OSPF 선제 우회 폐루프 (K8s 서빙 + FRR 랩)
  terraform/      EKS 클라우드 IaC
  docs/           시스템 아키텍처
  .github/        CI/CD
  sim/            네트워크 물리·정책·토폴로지
  data/           라우팅 실험 결과
```

`sim/`의 라우팅 정책·환경은 석사 연구 산출물이고, 이 저장소는 그 위에
**운용·플랫폼 계층**을 구현. `netlab/`은 4대 라우터 실습 랩이고,
`closedloop/`가 K8s에 배포된 AI 탐지기로 이 랩을 구동(에뮬레이션).
