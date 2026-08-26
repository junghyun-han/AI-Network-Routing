# AI 기반 네트워크 라우팅·운용 최적화

동적으로 토폴로지가 변하는 패킷 네트워크에서, **강화학습 라우팅 정책**이
곧 끊길 링크를 미리 피하며 전달 품질과 안정성을 지키는 과정을 코드·데이터·수치로 정리한 포트폴리오다.

연구 도메인은 저궤도(LEO) 위성 메시망이지만, 여기서 다루는 운용 질문
(전달 성공률 유지 / 임박한 링크 단절 회피 / 경로 효율 / 안정성 여유 확보)은
동적 네트워크라면 공통으로 마주하는 문제다.

> 학습된 결과·가중치를 재사용해 **분석·검증·서빙**으로 확장한 것이며, 모델을 다시 학습하지 않는다.
> 따라서 GPU·대형 서버 없이 노트북에서 재현된다.

---

## 무엇을 보여주는가

| 역량 | 내용 | 위치 |
|---|---|---|
| 네트워크 품질·이상탐지 분석 | 전달 성공률, 임박 링크 단절(이상징후) 회피율, 트래픽 시나리오별 패턴, 안정성 여유 | [`analysis/`](analysis/) |
| 최적화·솔버 활용 | 오프라인 최적 라우팅을 OR-Tools로 정식화, 강화학습 정책의 optimality gap 검증 | [`solver/`](solver/) *(작업 중)* |
| 컨테이너 배포·서빙 | 라우팅 정책을 API로 서빙, Docker + Kubernetes 배포 | [`serving/`](serving/) *(작업 중)* |

---

## 1. 네트워크 운용 분석 — [analysis/OPERATIONS_REPORT.md](analysis/OPERATIONS_REPORT.md)

79,200개 에피소드(1,200개 네트워크 시나리오)를 운용 관점으로 분석한다.

- **이상징후(임박 링크 단절) 선제 회피**: 제안 정책은 곧 끊길 링크를 **0.0%** 사용.
  최단경로·탐욕 기반 방법은 약 **30~34%** 사용해 단절을 그대로 밟는다.
- **품질·안정성 균형**: 제안 정책은 최적해(Oracle) 바로 옆, "안정+효율" 영역에 위치.
- **안정성 여유**: 선택 링크의 잔여 접촉시간 중앙값이 상한(20초)에 도달.

```bash
python analysis/ops_analysis.py --episodes data/episodes_all_v2.csv --out analysis
```

---

## 재현

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pandas numpy matplotlib
python analysis/ops_analysis.py --episodes data/episodes_all_v2.csv --out analysis
```

---

## 구조

```
AI-Network-Routing/
  analysis/     운용 지표·이상탐지 분석 (스크립트 + 리포트 + 그림)
  solver/       OR-Tools 최적화·optimality gap 검증        (작업 중)
  serving/      Docker + Kubernetes 배포·서빙             (작업 중)
  data/         분석 입력 (에피소드 결과 CSV)
```

## 범위

라우팅 정책·환경의 연구 산출물은 별도 저장소에서 관리하며, 이 저장소는 그 결과를
**네트워크 운용 관점의 분석·최적화 검증·배포**로 확장한 응용 층이다.
