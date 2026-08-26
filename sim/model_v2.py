"""
model_v2 : model.py 의 v2. DESIGN_V2.md 3절 참고.

사다리 4단 (축이 하나씩만 바뀐다)
  linear    : 비선형 X, 이웃 X, 엣지 X      (v1 baseline 유지)
  mlp       : 비선형 O, 이웃 X, 엣지 X      (DISCUSSION.md #3 해결)
  gcn       : 비선형 O, 이웃 O, 엣지 X      (논문의 현재 모델)
  gcn_edge  : 비선형 O, 이웃 O, 엣지 O      (v2 제안)

mlp / gcn / gcn_edge 는 정책 헤드가 동일하고 인코더(또는 엣지 입력)만 다르다.
-> mlp vs gcn = 이웃 정보 하나만 차이,  gcn vs gcn_edge = 엣지(접촉시간) 하나만 차이

v1 대비 수정
  - critic 을 진짜 V(s) 스칼라로 (v1 은 노드별 출력을 action 으로 인덱싱해 Q 처럼 썼다)
  - GAE(lambda) 도입 + 잘림에서 bootstrap (v1 은 MC return - V, 잘림에 R=0 강제)
  - GCN 을 6단 전파 -> 2블록 + LayerNorm + residual (over-smoothing 완화)
  - 안 넘기기 금지는 env_v2.action_mask() 가 담당
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

KINDS = ("linear", "mlp", "gcn", "gcn_edge")


# ======================================================
# GCN
# ======================================================

def gcn_norm_adj(adj: torch.Tensor) -> torch.Tensor:
    """D^-1/2 (A+I) D^-1/2. [N,N] 또는 [B,N,N]."""
    if adj.dim() == 2:
        A = adj + torch.eye(adj.size(0), device=adj.device, dtype=adj.dtype)
        dinv = A.sum(1).clamp(min=1e-6).pow(-0.5)
        return dinv.unsqueeze(1) * A * dinv.unsqueeze(0)
    if adj.dim() == 3:
        B, N, _ = adj.shape
        A = adj + torch.eye(N, device=adj.device, dtype=adj.dtype).expand(B, N, N)
        dinv = A.sum(2).clamp(min=1e-6).pow(-0.5)
        return dinv.unsqueeze(2) * A * dinv.unsqueeze(1)
    raise ValueError("adj must be 2D or 3D")


class GCNBlock(nn.Module):
    """DenseGCN -> LayerNorm -> ReLU, residual (차원이 같을 때)."""

    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.lin = nn.Linear(in_dim, out_dim)
        self.norm = nn.LayerNorm(out_dim)
        self.res = (in_dim == out_dim)

    def forward(self, x: torch.Tensor, adj_norm: torch.Tensor) -> torch.Tensor:
        h = adj_norm @ self.lin(x)
        h = F.relu(self.norm(h))
        return x + h if self.res else h


class Encoder(nn.Module):
    """노드 특징 -> 노드 임베딩 [.., N, H]."""

    def __init__(self, kind: str, node_dim: int, hidden: int):
        super().__init__()
        self.kind = kind
        if kind == "mlp":
            self.net = nn.Sequential(nn.Linear(node_dim, hidden), nn.ReLU(),
                                     nn.Linear(hidden, hidden), nn.ReLU())
        elif kind in ("gcn", "gcn_edge"):
            self.inp = nn.Linear(node_dim, hidden)
            self.b1 = GCNBlock(hidden, hidden)
            self.b2 = GCNBlock(hidden, hidden)
        elif kind != "linear":
            raise ValueError(kind)

    def forward(self, x: torch.Tensor, adj: Optional[torch.Tensor]) -> torch.Tensor:
        if self.kind == "linear":
            return x
        if self.kind == "mlp":
            return self.net(x)
        an = gcn_norm_adj(adj)
        h = F.relu(self.inp(x))
        return self.b2(self.b1(h, an), an)


# ======================================================
# Actor-Critic
# ======================================================

class ActorCriticV2(nn.Module):
    def __init__(self, kind: str, node_dim: int, edge_dim: int, hidden: int = 64):
        super().__init__()
        assert kind in KINDS, kind
        self.kind = kind
        self.enc = Encoder(kind, node_dim, hidden)

        H = node_dim if kind == "linear" else hidden
        self.H = H
        use_edge = (kind == "gcn_edge")
        self.edge_dim = edge_dim if use_edge else 0

        if kind == "linear":
            # v1 baseline 과 같은 형태: 노드 특징의 선형 함수
            self.pi = nn.Linear(H, 1)
            self.v = nn.Linear(2 * H, 1)
        else:
            self.pi = nn.Sequential(
                nn.Linear(3 * H + self.edge_dim, hidden), nn.ReLU(),
                nn.Linear(hidden, hidden), nn.ReLU(),
                nn.Linear(hidden, 1))
            self.v = nn.Sequential(
                nn.Linear(2 * H, hidden), nn.ReLU(),
                nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor, adj: torch.Tensor, edge: torch.Tensor,
                carrier: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """x [B,N,F], adj [B,N,N], edge [B,N,E], carrier [B] -> logits [B,N], value [B]"""
        h = self.enc(x, adj)                                  # [B,N,H]
        B, N, H = h.shape
        idx = carrier.view(B, 1, 1).expand(B, 1, H)
        h_c = h.gather(1, idx)                                # [B,1,H]

        if self.kind == "linear":
            logits = self.pi(h).squeeze(-1)
        else:
            hc = h_c.expand(B, N, H)
            parts = [hc, h, h - hc]
            if self.edge_dim:
                parts.append(edge)
            logits = self.pi(torch.cat(parts, dim=-1)).squeeze(-1)

        value = self.v(torch.cat([h_c.squeeze(1), h.mean(dim=1)], dim=-1)).squeeze(-1)
        return logits, value


# ======================================================
# PPO
# ======================================================

class PPOAgentV2:
    def __init__(self, kind: str, node_dim: int, edge_dim: int, hidden: int = 64,
                 lr: float = 3e-4, device: str = "cpu", gamma: float = 0.99,
                 lam: float = 0.95, clip: float = 0.2, c1: float = 0.5,
                 c2: float = 0.01, vclip: float = 0.2, max_grad_norm: float = 0.5):
        self.kind = kind
        self.device = device
        self.ac = ActorCriticV2(kind, node_dim, edge_dim, hidden).to(device)
        self.opt = optim.Adam(self.ac.parameters(), lr=lr)
        self.gamma, self.lam, self.clip = gamma, lam, clip
        self.c1, self.c2, self.vclip = c1, c2, vclip
        self.max_grad_norm = max_grad_norm

    # ---------- 단일 스텝 ----------

    def _tensors(self, x, adj, edge, mask, carrier):
        d = self.device
        return (torch.as_tensor(x, dtype=torch.float32, device=d).unsqueeze(0),
                torch.as_tensor(adj, dtype=torch.float32, device=d).unsqueeze(0),
                torch.as_tensor(edge, dtype=torch.float32, device=d).unsqueeze(0),
                torch.as_tensor(mask, dtype=torch.float32, device=d).unsqueeze(0),
                torch.as_tensor([int(carrier)], dtype=torch.long, device=d))

    @torch.no_grad()
    def act(self, x, adj, edge, mask, carrier, greedy: bool = False):
        xt, at, et, mt, ct = self._tensors(x, adj, edge, mask, carrier)
        logits, value = self.ac(xt, at, et, ct)
        logits = logits.masked_fill(mt <= 0, -1e9)
        if greedy:
            a = int(torch.argmax(logits, dim=-1).item())
            return a, 0.0, float(value.item())
        dist = torch.distributions.Categorical(logits=logits)
        a = dist.sample()
        return int(a.item()), float(dist.log_prob(a).item()), float(value.item())

    @torch.no_grad()
    def value_of(self, x, adj, edge, mask, carrier) -> float:
        xt, at, et, _mt, ct = self._tensors(x, adj, edge, mask, carrier)
        return float(self.ac(xt, at, et, ct)[1].item())

    # ---------- GAE ----------

    def compute_gae(self, rewards: List[float], values: List[float],
                    last_value: float) -> Tuple[List[float], List[float]]:
        """에피소드 하나에 대한 GAE. 종료(success)면 last_value=0, 잘림이면 V(s_last)."""
        adv, gae = [0.0] * len(rewards), 0.0
        for t in reversed(range(len(rewards))):
            v_next = last_value if t == len(rewards) - 1 else values[t + 1]
            delta = rewards[t] + self.gamma * v_next - values[t]
            gae = delta + self.gamma * self.lam * gae
            adv[t] = gae
        ret = [a + v for a, v in zip(adv, values)]
        return adv, ret

    # ---------- 업데이트 ----------

    def set_lr(self, lr: float) -> None:
        for g in self.opt.param_groups:
            g["lr"] = float(lr)

    def update(self, mem: List[dict], epochs: int = 5, minibatch: int = 256,
               target_kl: Optional[float] = 0.03):
        if len(mem) < minibatch:
            return {}
        d = self.device
        x = torch.as_tensor(np.stack([m["x"] for m in mem]), dtype=torch.float32, device=d)
        adj = torch.as_tensor(np.stack([m["adj"] for m in mem]), dtype=torch.float32, device=d)
        edge = torch.as_tensor(np.stack([m["edge"] for m in mem]), dtype=torch.float32, device=d)
        mask = torch.as_tensor(np.stack([m["mask"] for m in mem]), dtype=torch.float32, device=d)
        carrier = torch.as_tensor([m["carrier"] for m in mem], dtype=torch.long, device=d)
        act = torch.as_tensor([m["a"] for m in mem], dtype=torch.long, device=d)
        lp_old = torch.as_tensor([m["lp"] for m in mem], dtype=torch.float32, device=d)
        v_old = torch.as_tensor([m["v"] for m in mem], dtype=torch.float32, device=d)
        adv = torch.as_tensor([m["adv"] for m in mem], dtype=torch.float32, device=d)
        ret = torch.as_tensor([m["ret"] for m in mem], dtype=torch.float32, device=d)

        adv = (adv - adv.mean()) / (adv.std(unbiased=False) + 1e-8)

        logs = {"policy_loss": [], "value_loss": [], "entropy": [], "clipfrac": [], "kl": []}
        n = len(mem)
        stop = False
        for _ in range(epochs):
            if stop:
                break
            perm = torch.randperm(n, device=d)
            for s in range(0, n, minibatch):
                b = perm[s:s + minibatch]
                logits, value = self.ac(x[b], adj[b], edge[b], carrier[b])
                logits = logits.masked_fill(mask[b] <= 0, -1e9)
                dist = torch.distributions.Categorical(logits=logits)
                lp = dist.log_prob(act[b])
                ratio = torch.exp(lp - lp_old[b])

                s1 = ratio * adv[b]
                s2 = torch.clamp(ratio, 1 - self.clip, 1 + self.clip) * adv[b]
                pol = -torch.min(s1, s2).mean()

                v_clipped = v_old[b] + torch.clamp(value - v_old[b], -self.vclip, self.vclip)
                val = torch.max(F.mse_loss(value, ret[b]),
                                F.mse_loss(v_clipped, ret[b]))

                ent = dist.entropy().mean()
                loss = pol + self.c1 * val - self.c2 * ent

                self.opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.ac.parameters(), self.max_grad_norm)
                self.opt.step()

                logs["policy_loss"].append(float(pol.item()))
                logs["value_loss"].append(float(val.item()))
                logs["entropy"].append(float(ent.item()))
                logs["clipfrac"].append(float(((ratio - 1).abs() > self.clip).float().mean()))
                kl = float((lp_old[b] - lp).mean().item())
                logs["kl"].append(kl)
                # KL 조기 종료 (PPO 표준 안전장치) : 정책이 한 번에 너무 많이 변하면 중단
                if target_kl is not None and kl > 1.5 * target_kl:
                    stop = True
                    break
        return logs

    # ---------- 저장/로드 ----------

    def save(self, path: str) -> None:
        torch.save({"kind": self.kind, "state": self.ac.state_dict()}, path)

    def load(self, path: str) -> None:
        ck = torch.load(path, map_location=self.device)
        assert ck["kind"] == self.kind, f"kind mismatch: {ck['kind']} vs {self.kind}"
        self.ac.load_state_dict(ck["state"])
        self.ac.eval()
