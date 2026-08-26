# AI 기반 네트워크 라우팅·운용

동적으로 토폴로지가 변하는 패킷 네트워크에서, **강화학습 라우팅 정책**이
곧 끊길 링크를 미리 피하며 전달 품질과 안정성을 지키는 과정을
코드·데이터·수치로 정리한 포트폴리오다.

연구 도메인은 저궤도(LEO) 위성 메시망이지만, 여기서 다루는 운용 질문
(전달 성공률 유지 / 임박한 링크 단절 회피 / 트래픽 최적화 / 안정성 여유 확보 /
장애 선제 예측 / 자동 배포·서빙)은 동적 네트워크라면 공통으로 마주하는 문제다.

> 학습된 결과·가중치를 재사용해 **분석·이상탐지·최적화·가시성·배포**로 확장한 것이며,
> 모델을 다시 학습하지 않는다. GPU·대형 서버 없이 노트북에서 재현된다.
> 시스템 구조는 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 무엇을 보여주는가

| # | 역량 | 핵심 결과 | 위치 |
|---|---|---|---|
| 1 | **운용 지표·트래픽 분석** | 곧 끊길 링크 사용 0.0% (최단경로 30~34%), 최적해 옆 안정+효율 | [`analysis/`](analysis/) |
| 2 | **장애 선제 예측(이상탐지)** | 임박 단절 탐지 ROC-AUC 0.840, 거리기반 baseline 대비 +0.179 | [`anomaly/`](anomaly/) |
| 3 | **최적화·솔버(OR-Tools)** | 오프라인 최적을 CP-SAT로 풀어 optimality gap 1.83%, 40/40 검증 | [`solver/`](solver/) |
| 4 | **E2E 트래픽 가시성** | 연결성·지연·병목 링크·경로 다양성(독립경로 중앙값 6) | [`traffic/`](traffic/) |
| 5 | **링크 장애 영향도 분석** | 링크별 criticality 랭킹, 단일 장애 평균 E2E 영향 8.9km | [`impact/`](impact/) |
| 6 | **컨테이너 배포·자동화(Docker+K8s)** | 정책을 REST로 서빙, kind K8s에 배포·스케일링 검증 | [`serving/`](serving/) |
| 7 | **시스템 아키텍처 문서** | 구성품·인터페이스·데이터 흐름·설계 근거 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |

각 폴더의 `README.md`가 그 산출물의 리포트다(수치·그림 포함).

---

## 재현

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pandas numpy matplotlib scikit-learn ortools networkx fastapi "uvicorn[standard]" torch

python analysis/ops_analysis.py --episodes data/episodes_all_v2.csv --out analysis
python anomaly/detect.py --out anomaly
python solver/optimality_gap.py --episodes data/episodes_all_v2.csv --out solver
python traffic/visibility.py --out traffic
python impact/criticality.py --out impact
# serving (Docker + Kubernetes): serving/README.md 참고
```

---

## 구조

```
AI-Network-Routing/
  analysis/     운용 지표·트래픽 분석
  anomaly/      장애 선제 예측(이상탐지, ROC/PR)
  solver/       OR-Tools 최적화·optimality gap
  traffic/      E2E 트래픽 가시성 (networkx)
  impact/       링크 장애 영향도·criticality
  serving/      Docker + Kubernetes 서빙 (FastAPI, kind 검증)
  docs/         시스템 아키텍처 문서
  sim/          시뮬레이션 물리·정책·토폴로지 (문제 정의)
  data/         분석 입력 (에피소드 결과 CSV)
```

## 범위

라우팅 정책·환경의 연구 산출물은 별도 저장소에서 관리하며, 이 저장소는 그 결과를
**네트워크 운용 관점의 분석·이상탐지·최적화·가시성·배포**로 확장한 응용 층이다.
