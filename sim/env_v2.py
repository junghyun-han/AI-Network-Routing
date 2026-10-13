"""
env_v2 : env.py 의 v2. DESIGN_V2.md 참고.

물리 시뮬레이션은 env.py 와 완전히 동일하다 (궤도 배치, 위치 계산, 링크 범위 150km,
시간 진행 1s, 위성 68기, 잔여 접촉시간 추정식). 바뀐 것은 관측 / 보상 / 행동 제약뿐이다.

v1 대비 변경점
  관측  : 노드 특징에 속도 추가, 역할을 0/1 세 차원으로 분리, 위치 정규화, 상수차원 제거
          + 엣지 특징(링크 거리 / 잔여 접촉시간 / 안테나 각도 / 링크 품질) 신규 노출
  보상  : eta = B * min(1, T_rem/T_ref)   (tau_align 을 eta 에서 제거 -> 이중 계상 해소)
          전 항을 [0,1] 로 정규화한 뒤 가중치 적용
  행동  : '안 넘기고 들고 있기' 금지
  정리  : FEAT_DIM / handover_mode / LEO_B 제거, guard_time_s -> T_ref_s 로 재활용
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


class LeoParameters:
    LEO_A = {
        "height": 1.0e6,  # 1000 km
        "beam_radius": 30,
        "transmitter_gain": 2,
        "receiver_gain": 2,
        "uplink_bandwidth": 159e6,
        "downlink_bandwidth": 180e6,
        "side_lobe_attenuations": [0, -14, -18],
    }
    # (v1 의 LEO_B 는 어디에서도 쓰이지 않아 제거)


class ParamsV2:
    # ---- 물리 / 도메인 : v1 과 동일, 건드리지 않음 ----
    height = LeoParameters.LEO_A["height"]
    LEO_A = LeoParameters.LEO_A

    ground_size = 300
    ground_stations = [(-300, 200), (-300, 0), (-300, -200), (300, 100), (300, -100)]

    time_step = 1.0
    horizon = 1200
    link_range_km = 150.0
    omega_max_deg_s = 2.0

    isl_bandwidth = 1.0      # 정규화 기준 (항상 1.0)
    bw_alpha = 1.0           # 거리 감쇠 지수 (항상 1.0 -> 선형)

    # ---- 잔여 접촉시간 ----
    TREM_CAP_S = 20.0        # 추정치 상한
    T_ref_s = 20.0           # eta 의 접촉시간 기준값 (v1 의 미사용 guard_time_s 자리)
                             # 근거: 멀어지는 링크의 87% 가 20s 안에서 서로 구분됨 (v1 측정)
    RISKY_TREM_S = 2.0       # '위험홉' 판정 임계값 (평가 지표용)

    # ---- 보상 : 전 항 [0,1] 정규화 후 가중 ----
    # 전송기회 eta = B * S 는 '지표'로 유지하고, 보상에서는 두 성분을 분리한다.
    # 근거: eta 와 B 의 상관이 +0.954 라서 eta 를 그대로 쓰면 가중치를 키워도
    #       강화되는 것은 '짧은 링크 선호'이고 '만료 임박 링크 회피'가 아니다 (v2 측정).
    #       S 단독의 변동폭은 eta 의 1.6배.
    # 품질 항은 '부족분에 대한 비용' 으로 쓴다 (보너스로 쓰면 홉을 늘려 점수를 벌 수 있다).
    #   -w_B*(d/R)  : 링크가 멀수록 비용        (= -w_B*(1-B))
    #   -w_S*(1-S)  : 접촉시간이 짧을수록 비용
    # 완벽한 링크라도 홉당 이득은 0 이고, 보상의 원천은 전진과 도착뿐이다.
    w_B = 0.3                # 링크 거리 비용
    w_S = 1.0                # 접촉시간 부족 비용  (Cost-Greedy 대리 sweep 으로 확정)
    w_dist = 1.0             # 목적지 전진 이득 (Delta d / R_max)
    w_align = 0.2            # 안테나 오버헤드 비용 (tau / tau_max)
    bonus_success = 5.0      # 도착 보너스
    hop_penalty = 0.0        # 별도 스텝 벌점 불필요 (품질 비용이 그 역할을 한다)

    invalid_action_penalty = -1.0

    max_steps = 200
    eval_mode = False        # 지표 기록용 플래그 (보상에는 영향 없음)

    # 오라클 전용 (기본 False -> 온라인 방법은 영향 없음).
    # 미래를 전부 아는 오프라인 오라클에게만 '왔던 노드로 되돌아가기' 를 허용한다.
    allow_backtrack = False

    @property
    def tau_max_s(self) -> float:
        return 180.0 / float(self.omega_max_deg_s)

    @property
    def pos_scale(self) -> float:
        return 2.0 * float(self.ground_size)


# 노드 특징은 논문 IV-A 의 state 정의를 그대로 따른다 (위치 / 목적지 진행도 /
# 패킷 보유 표시 / 목적지권역 표시) + 진행 스텝. 위성 속도는 넣지 않는다.
#   속도를 넣으면 모델이 잔여 접촉시간을 스스로 계산해버려서, 접촉시간을 명시적으로
#   주는 것(엣지 특징)의 기여가 가려진다. 논문에도 없는 입력이다.
NODE_FEAT_DIM = 7   # [x, y, d_dst, is_start, is_dst_region, is_carrier, step]
EDGE_FEAT_DIM = 4   # [d/R, T_rem/T_ref, tau/tau_max, B]


def angle_deg_between(p_from, p_mid, p_to) -> float:
    v1 = np.array(p_mid) - np.array(p_from)
    v2 = np.array(p_to) - np.array(p_mid)
    if np.linalg.norm(v1) < 1e-6 or np.linalg.norm(v2) < 1e-6:
        return 0.0
    cos = np.clip(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)), -1.0, 1.0)
    return float(np.degrees(np.arccos(cos)))


def align_time_s(angle_deg: float, omega_deg_s: float) -> float:
    return float(angle_deg) / max(1e-6, float(omega_deg_s))


def det_start_sat(st: np.ndarray, src_xy, radius: float) -> int:
    d = np.linalg.norm(st[:, :2] - np.asarray(src_xy), axis=1)
    near = np.where(d < float(radius))[0]
    k = near[np.argmin(d[near])] if len(near) > 0 else int(np.argmin(d))
    return int(k)


class LeoEnvV2:
    def __init__(self, p: Optional[ParamsV2] = None):
        self.p = p if p is not None else ParamsV2()
        self.t = 0
        self.prev_a: Optional[int] = None
        self.prev_prev_a: Optional[int] = None
        self.prev_dist_dst: Optional[float] = None
        self.start: Optional[int] = None
        self.step_count = 0

        self.sats: List[Dict[str, Any]] = []
        self._pos_cache: Dict[int, np.ndarray] = {}
        self._dist_cache: Dict[int, np.ndarray] = {}
        self._adj_cache: Dict[int, np.ndarray] = {}

        # ---- v1 과 동일한 궤도 배치 ----
        self.orbit_params = ([(1, 4, 1200 - c * 600) for c in range(5)]
                             + [(-4, 1, 1000 - c * 1000) for c in range(3)])

        for orbit_idx, orbit in enumerate(self.orbit_params):
            v, period = self._get_velocity_period(self.p.height)
            xs, ys = self._get_xs_ys(v, period, orbit)
            dist = np.linalg.norm([max(xs) - min(xs), max(ys) - min(ys)])
            num_sats_on_orbit = max(1, round(dist / (self.p.LEO_A["beam_radius"] * 2)))
            for i in range(num_sats_on_orbit):
                offset = (i * len(xs) // num_sats_on_orbit) % len(xs)
                # 속도는 직선 궤도라 시간에 무관하게 일정하다. 주기 wrap 에서의
                # 순간이동을 차분으로 잡으면 잘못되므로 궤도 방향에서 직접 구한다.
                vx = float(xs[1] - xs[0]) if len(xs) > 1 else 0.0
                vy = float(ys[1] - ys[0]) if len(ys) > 1 else 0.0
                self.sats.append({"xs": xs, "ys": ys, "offset": offset,
                                  "period": len(xs), "orbit_id": orbit_idx,
                                  "vx": vx, "vy": vy})

        self.grounds = [self.p.ground_stations[0], self.p.ground_stations[4]]
        self.N = len(self.sats)

    # ================= 물리 (v1 과 동일) =================

    def _get_velocity_period(self, height: float) -> Tuple[float, float]:
        G, M, R = 6.6743e-11, 5.9742e24, 6.357e6
        v = math.sqrt(G * M / (R + height)) / 1000.0
        period = 2 * math.pi * (R + height) / (v * 1000.0)
        return v, period

    def _get_xs_ys(self, v: float, period: float, orbit: Tuple[float, float, float]):
        a, b, c = orbit
        y = lambda x: -(a / b) * x - (c / b)
        x = lambda yv: -(b / a) * yv - (c / a)
        g = self.p.ground_size

        pts = [p for p in [(-g, x(-g)), (g, x(g)), (y(-g), -g), (y(g), g)]
               if abs(p[0]) <= g and abs(p[1]) <= g]
        if len(pts) < 2:
            return np.zeros(self.p.horizon), np.zeros(self.p.horizon)

        path_len = np.linalg.norm(np.array(pts[0]) - np.array(pts[1]))
        time_range = np.arange(0, path_len / v, self.p.time_step)
        xs = np.linspace(pts[0][0], pts[1][0], len(time_range))
        ys = np.linspace(pts[0][1], pts[1][1], len(time_range))
        if xs[0] > xs[-1]:
            xs, ys = xs[::-1], ys[::-1]
        return xs, ys

    def positions(self, t: Optional[int] = None) -> np.ndarray:
        """시각 t 의 전 위성 좌표 [N,2].  (시각별 캐시)"""
        t_idx = int(self.t if t is None else t) % self.p.horizon
        hit = self._pos_cache.get(t_idx)
        if hit is not None:
            return hit
        out = np.empty((self.N, 2), dtype=np.float32)
        for k, s in enumerate(self.sats):
            idx = (t_idx + int(s["offset"])) % s["period"]
            out[k, 0] = s["xs"][idx]
            out[k, 1] = s["ys"][idx]
        self._pos_cache[t_idx] = out
        return out

    def pair_dist(self, t: Optional[int] = None) -> np.ndarray:
        """전 위성쌍 거리 [N,N]. (시각별 캐시)"""
        t_idx = int(self.t if t is None else t) % self.p.horizon
        hit = self._dist_cache.get(t_idx)
        if hit is not None:
            return hit
        pos = self.positions(t_idx)
        diff = pos[:, None, :] - pos[None, :, :]
        D = np.sqrt((diff ** 2).sum(-1)).astype(np.float32)
        self._dist_cache[t_idx] = D
        return D

    def compute_graph(self, t: Optional[int] = None) -> np.ndarray:
        t_idx = int(self.t if t is None else t) % self.p.horizon
        hit = self._adj_cache.get(t_idx)
        if hit is not None:
            return hit
        D = self.pair_dist(t_idx)
        A = (D <= float(self.p.link_range_km)).astype(np.float32)
        np.fill_diagonal(A, 0.0)
        self._adj_cache[t_idx] = A
        return A

    def _pred_remaining_contact_time(self, i: int, j: int, t_idx: int) -> float:
        """v1 과 동일한 추정식: 1슬롯 선형 외삽."""
        rng, dt = float(self.p.link_range_km), float(self.p.time_step)
        d0 = float(self.pair_dist(t_idx)[int(i), int(j)])
        if d0 >= rng:
            return 0.0
        d1 = float(self.pair_dist(int(t_idx) + 1)[int(i), int(j)])
        dd = d1 - d0
        if dd <= 0:
            return float("inf")                      # 서로 가까워지는 중
        return max(0.0, (rng - d0) / dd) * dt

    # ================= 관측 =================

    def node_features(self) -> np.ndarray:
        """[N, 7] : [x, y, d_dst, is_start, is_dst_region, is_carrier, step]"""
        S = self.p.pos_scale
        pos = self.positions()
        dst = np.array(self.grounds[1], dtype=np.float32)
        sr = float(self.p.link_range_km)

        f = np.zeros((self.N, NODE_FEAT_DIM), dtype=np.float32)
        f[:, 0:2] = pos / S
        d_dst = np.linalg.norm(pos - dst[None, :], axis=1)
        f[:, 2] = d_dst / S
        if self.start is not None:
            f[int(self.start), 3] = 1.0
        f[d_dst < sr, 4] = 1.0
        if self.prev_a is not None:
            f[int(self.prev_a), 5] = 1.0
        f[:, 6] = float(self.step_count) / float(self.p.max_steps)
        return f

    def edge_features(self, i: int) -> np.ndarray:
        """현재 위성 i 에서 각 후보 j 로 가는 링크의 특징 [N, 4].
        이웃이 아닌 j 는 0 으로 둔다."""
        pos = self.positions()
        R = float(self.p.link_range_km)
        T_ref = float(self.p.T_ref_s)
        tau_max = float(self.p.tau_max_s)

        e = np.zeros((self.N, EDGE_FEAT_DIM), dtype=np.float32)
        d = self.pair_dist()[int(i)]
        nbr = np.where((d <= R) & (np.arange(self.N) != int(i)))[0]
        for j in nbr:
            j = int(j)
            trem = self._pred_remaining_contact_time(int(i), j, int(self.t))
            trem = min(trem, float(self.p.TREM_CAP_S))
            tau = self._align_time(int(i), j, pos)
            e[j, 0] = d[j] / R
            e[j, 1] = min(1.0, trem / T_ref)
            e[j, 2] = min(1.0, tau / tau_max)
            e[j, 3] = max(0.0, 1.0 - d[j] / R)
        return e

    def _align_time(self, i: int, j: int, pos: Optional[np.ndarray] = None) -> float:
        """직전 홉 방향에서 (i->j) 방향으로 트는 데 걸리는 시간."""
        if self.prev_prev_a is None or self.prev_a is None or int(self.prev_a) != int(i):
            return 0.0
        if pos is None:
            pos = self.positions()
        ang = angle_deg_between(pos[int(self.prev_prev_a)], pos[int(i)], pos[int(j)])
        return align_time_s(ang, float(self.p.omega_max_deg_s))

    # ================= 전송기회 =================

    def hop_quality(self, i: int, j: int) -> Tuple[float, float, float, float, float, float]:
        """(eta, tau_align, T_rem_capped, d_ij, B, S)

        eta = B * min(1, T_rem / T_ref)
          - B      : 링크 품질 = 1 - d/R_max
          - min(..): 접촉시간 여유. v1 은 min(dt=1s, T_rem) 이라 70% 가 상수로 뭉개졌다.
          - tau_align 은 여기서 빼지 않는다 (보상의 -w_align 항이 단독 담당)
        """
        pos = self.positions()
        R = float(self.p.link_range_km)
        d_ij = float(self.pair_dist()[int(i), int(j)])
        tau = self._align_time(int(i), int(j), pos)

        if d_ij > R:
            return 0.0, tau, 0.0, d_ij, 0.0, 0.0

        trem = self._pred_remaining_contact_time(int(i), int(j), int(self.t))
        trem = float(min(trem, float(self.p.TREM_CAP_S)))

        B = float(max(0.0, 1.0 - d_ij / R) ** float(self.p.bw_alpha))
        S = float(min(1.0, trem / float(self.p.T_ref_s)))
        return float(np.clip(B * S, 0.0, 1.0)), tau, trem, d_ij, B, S

    # ================= 에피소드 =================

    def reset(self, t0: Optional[int] = None, start: Optional[int] = None) -> np.ndarray:
        """start 를 주면 그 위성에서 시작한다 (오라클의 시작 위성 자유 선택용).
        None 이면 기존과 동일하게 소스에 가장 가까운 위성."""
        self.t = int(self.t if t0 is None else t0) % self.p.horizon
        self.prev_prev_a = None
        self.prev_dist_dst = None
        self.step_count = 0

        pos = self.positions()
        self.start = (int(start) if start is not None else
                      det_start_sat(pos, np.array(self.grounds[0], dtype=np.float32),
                                    float(self.p.link_range_km)))
        self.prev_a = int(self.start)
        self.prev_dist_dst = float(np.linalg.norm(
            np.array(self.grounds[1], dtype=np.float32) - pos[self.prev_a]))
        return self.node_features()

    def action_mask(self) -> np.ndarray:
        """v2 행동 제약: 이웃만, 자기 자신 금지, 직전 노드 되돌아가기 금지.
        allow_backtrack 을 켜면 되돌아가기만 해제된다 (오라클 전용)."""
        A = self.compute_graph()
        m = A[int(self.prev_a)].copy()
        m[int(self.prev_a)] = 0.0                       # 안 넘기기 금지
        if self.prev_prev_a is not None and not self.p.allow_backtrack:
            m[int(self.prev_prev_a)] = 0.0              # 되돌아가기 금지
        if m.sum() == 0:                                # 되돌아가기밖에 없으면 허용
            m = A[int(self.prev_a)].copy()
            m[int(self.prev_a)] = 0.0
        return m

    def step(self, action: int):
        p = self.p
        action = int(action)
        i = int(self.prev_a)
        pos = self.positions()
        dst = np.array(self.grounds[1], dtype=np.float32)

        # ---- 유효성: 자기 자신 금지 + 링크 존재 ----
        bad = (action == i) or \
              (float(self.pair_dist()[i, action]) > float(p.link_range_km))
        if bad:
            self.t = (self.t + 1) % p.horizon
            self.step_count += 1
            info = {"success": False, "invalid_action": True,
                    "eta": 0.0, "align_time_s": 0.0, "trem_s": 0.0, "d_ij": 0.0,
                    "risky": False, "actual_action": i}
            return self.node_features(), float(p.invalid_action_penalty), False, info

        eta, tau, trem, d_ij, Bq, Sq = self.hop_quality(i, action)

        dist_dst = float(np.linalg.norm(dst - pos[action]))
        success = dist_dst < float(p.link_range_km)
        progress = (self.prev_dist_dst - dist_dst) if self.prev_dist_dst is not None else 0.0

        # ---- 보상: 전 항 [0,1] 정규화 후 가중 ----
        r = (float(p.w_dist) * (progress / float(p.link_range_km))
             - float(p.w_B) * (1.0 - Bq)
             - float(p.w_S) * (1.0 - Sq)
             - float(p.w_align) * (min(tau, p.tau_max_s) / float(p.tau_max_s))
             + (float(p.bonus_success) if success else 0.0)
             - float(p.hop_penalty))

        self.prev_prev_a = self.prev_a
        self.prev_a = action
        self.prev_dist_dst = dist_dst
        self.t = (self.t + 1) % p.horizon
        self.step_count += 1

        info = {"success": bool(success), "invalid_action": False,
                "eta": float(eta), "B": float(Bq), "S": float(Sq),
                "align_time_s": float(tau),
                "trem_s": float(trem), "d_ij": float(d_ij),
                "dist_dst": float(dist_dst), "progress_km": float(progress),
                "risky": bool(trem < float(p.RISKY_TREM_S)),
                "actual_action": action}
        return self.node_features(), float(r), bool(success), info
