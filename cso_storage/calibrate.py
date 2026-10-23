"""Fit the lumped model to observed EDM spills.

The GIS gives a first estimate of contributing area and pass-forward flow;
calibration adjusts these (and the response time constants) until the model
spills in the same storms, for about as long, as the EDM record shows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution

from . import edm
from .model import ModelParams, spilling

DEFAULT_BOUNDS = {
    "area_factor": (0.3, 1.5),
    "q_pff_factor": (0.4, 2.0),   # multiplier on the GIS/Formula A pass-forward estimate
    "k_fast_min": (5.0, 60.0),
    "slow_coeff": (0.0, 0.4),
    "k_slow_h": (6.0, 96.0),
    "initial_loss_mm": (0.0, 2.0),
}

# Log-space spread of the GIS area and Formula A pass-forward estimates.
DEFAULT_PRIOR_SIGMA = {"area_factor": 0.35, "q_pff_factor": 0.35}


def _to_index_events(events: pd.DataFrame, index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """Event start/end as grid step numbers (end exclusive)."""
    t0 = index[0]
    step = index[1] - index[0]
    a = np.floor((events["start"] - t0) / step).astype(int).to_numpy()
    b = np.ceil((events["end"] - t0) / step).astype(int).to_numpy()
    b = np.maximum(b, a + 1)
    keep = (b > 0) & (a < len(index))
    return np.clip(a[keep], 0, len(index)), np.clip(b[keep], 0, len(index))


def _state_events(state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    edges = np.diff(np.concatenate([[0], state.astype(np.int8), [0]]))
    return np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)


def _hit(a, b, other_a, other_b, tol) -> np.ndarray:
    """For each event [a, b) say whether any ``other`` event overlaps it
    once widened by ``tol`` steps either side."""
    if len(other_a) == 0:
        return np.zeros(len(a), dtype=bool)
    idx = np.searchsorted(other_a, b + tol, side="left") - 1
    ok = idx >= 0
    out = np.zeros(len(a), dtype=bool)
    out[ok] = other_b[idx[ok]] > a[ok] - tol
    return out


@dataclass
class Scorer:
    """Precomputed observation data for fast repeated scoring."""

    index: pd.DatetimeIndex
    obs_a: np.ndarray
    obs_b: np.ndarray
    score_mask: np.ndarray   # True on steps used for scoring
    steps_per_hour: float
    tol_steps: int

    @classmethod
    def from_events(cls, index, events, window=None, exclude=(), warmup="14D", tol="2h"):
        step = index[1] - index[0]
        mask = np.ones(len(index), dtype=bool)
        mask[index < index[0] + pd.Timedelta(warmup)] = False
        if window is not None:
            mask &= (index >= window[0]) & (index < window[1])
        for s, e in exclude:
            mask &= ~((index >= s) & (index < e))
        a, b = _to_index_events(events, index)
        inside = np.array([mask[x:y].all() for x, y in zip(a, b)], dtype=bool)
        return cls(index, a[inside], b[inside], mask, pd.Timedelta("1h") / step, int(pd.Timedelta(tol) / step))

    def metrics(self, spilling: np.ndarray) -> dict:
        sa, sb = _state_events(spilling)
        inside = np.array([self.score_mask[x:y].all() for x, y in zip(sa, sb)], dtype=bool)
        sa, sb = sa[inside], sb[inside]
        pod = _hit(self.obs_a, self.obs_b, sa, sb, self.tol_steps).mean() if len(self.obs_a) else np.nan
        sr = _hit(sa, sb, self.obs_a, self.obs_b, self.tol_steps).mean() if len(sa) else np.nan
        f1 = 0.0 if not (pod > 0 and sr > 0) else 2 * pod * sr / (pod + sr)
        obs_dur = (self.obs_b - self.obs_a).sum() / self.steps_per_hour
        sim_dur = (sb - sa).sum() / self.steps_per_hour
        day = 24 * self.steps_per_hour
        obs_n = len(edm.count_blocks(self.obs_a, self.obs_b, day / 2, day))
        sim_n = len(edm.count_blocks(sa, sb, day / 2, day))
        return {
            "obs_events": int(len(self.obs_a)), "sim_events": int(len(sa)),
            "hit_rate": float(pod), "success_ratio": float(sr), "f1": float(f1),
            "obs_spills_12_24": obs_n, "sim_spills_12_24": sim_n,
            "obs_duration_h": float(obs_dur), "sim_duration_h": float(sim_dur),
        }

    def score(self, spilling: np.ndarray) -> float:
        m = self.metrics(spilling)
        eps = 0.5
        dur = abs(np.log((m["sim_duration_h"] + eps) / (m["obs_duration_h"] + eps)))
        cnt = abs(np.log((m["sim_spills_12_24"] + eps) / (m["obs_spills_12_24"] + eps)))
        return (1 - m["f1"]) + 0.5 * dur + 0.5 * cnt


@dataclass
class AnnualScorer:
    """Scoring against annual-return totals when event data is unavailable."""

    index: pd.DatetimeIndex
    annual: pd.DataFrame  # columns spills_12_24, duration_h indexed by year
    warmup: str = "14D"

    def metrics(self, spilling: np.ndarray) -> dict:
        ev = edm.events_from_state(self.index, spilling)
        ev = ev[ev["start"] >= self.index[0] + pd.Timedelta(self.warmup)]
        sim = edm.annual_summary(ev, years=list(self.annual.index))
        out = {}
        for y in self.annual.index:
            out[int(y)] = {
                "obs_spills_12_24": float(self.annual.loc[y, "spills_12_24"]),
                "sim_spills_12_24": int(sim.loc[y, "spills_12_24"]),
                "obs_duration_h": float(self.annual.loc[y, "duration_h"]),
                "sim_duration_h": float(sim.loc[y, "duration_h"]),
            }
        return out

    def score(self, spilling: np.ndarray) -> float:
        eps = 0.5
        terms = []
        for m in self.metrics(spilling).values():
            terms.append(abs(np.log((m["sim_spills_12_24"] + eps) / (m["obs_spills_12_24"] + eps)))
                         + 0.5 * abs(np.log((m["sim_duration_h"] + eps) / (m["obs_duration_h"] + eps))))
        return float(np.mean(terms))


def apply_vector(base: ModelParams, names, x) -> ModelParams:
    kw = {}
    for name, val in zip(names, x):
        if name == "q_pff_factor":
            kw["q_pff"] = base.q_pff * val
        else:
            kw[name] = float(val)
    return base.with_(**kw)


@dataclass
class CalibrationResult:
    params: ModelParams
    score: float
    metrics: dict
    names: list
    best_x: np.ndarray
    behavioural: list = field(default_factory=list)  # parameter sets within tolerance of the best


def calibrate(rain: pd.Series, base: ModelParams, scorer, bounds: dict | None = None,
              maxiter: int = 40, popsize: int = 12, seed: int | None = 1,
              behavioural_tol: float = 0.05, workers: int = 1,
              prior_sigma: dict | None = None, prior_weight: float = 0.1) -> CalibrationResult:
    """Differential evolution search over ``bounds`` (names -> (lo, hi)).

    Spill on/off records fix *when* flow exceeds the pass-forward, which pins
    the ratio of effective area to pass-forward flow, but not their absolute
    size: double both and the model spills in the same storms, while spilling
    twice the volume. ``prior_sigma`` (log-space standard deviation of each
    multiplier about 1.0) settles this along the GIS and Formula A estimates in
    proportion to how much each is trusted. If the pass-forward is measured
    (flow survey, pump rate, throttle test), leave ``q_pff_factor`` out of
    ``bounds`` so it is held fixed.

    The objective is step-wise in places (counts, hit rates), which is why a
    population search is used rather than a gradient method. Every parameter
    set scoring within ``behavioural_tol`` of the best is kept, so that the
    storage estimate can carry calibration uncertainty through.
    """
    bounds = dict(DEFAULT_BOUNDS if bounds is None else bounds)
    prior_sigma = DEFAULT_PRIOR_SIGMA if prior_sigma is None else prior_sigma
    names = list(bounds)
    seen: list[tuple[float, np.ndarray]] = []

    def objective(x):
        p = apply_vector(base, names, x)
        j = scorer.score(spilling(rain, p))
        for name, val in zip(names, x):
            if name in prior_sigma:
                j += prior_weight * 0.5 * (np.log(val) / prior_sigma[name]) ** 2
        seen.append((j, np.array(x)))
        return j

    res = differential_evolution(objective, [bounds[n] for n in names], maxiter=maxiter,
                                 popsize=popsize, seed=seed, polish=False, tol=1e-3,
                                 workers=workers, updating="deferred" if workers != 1 else "immediate")
    best = apply_vector(base, names, res.x)
    metrics = scorer.metrics(spilling(rain, best))

    beh, keys = [], set()
    for j, x in sorted(seen, key=lambda t: t[0]):
        if j > res.fun + behavioural_tol:
            break
        key = tuple(np.round(x, 3))
        if key not in keys:
            keys.add(key)
            beh.append(apply_vector(base, names, x))
    return CalibrationResult(best, float(res.fun), metrics, names, res.x, beh)
