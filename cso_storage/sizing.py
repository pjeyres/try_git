"""Storage needed at the overflow to meet a spill-frequency target."""
from __future__ import annotations

import calendar
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import edm
from .model import ModelParams, inflow, simulate


def _years(index: pd.DatetimeIndex) -> float:
    return (index[-1] - index[0] + (index[1] - index[0])) / pd.Timedelta("365.25D")


def spill_stats(rain: pd.Series, p: ModelParams, warmup: str = "14D", q: pd.DataFrame | None = None) -> dict:
    """Run the model and summarise spills as the EA would report them.
    ``q`` is a precomputed :func:`inflow` for the same rain and catchment."""
    sim = simulate(rain, p, q)
    sim = sim[sim.index >= sim.index[0] + pd.Timedelta(warmup)]
    events = edm.events_from_state(sim.index, sim["spilling"].to_numpy())
    annual = edm.annual_summary(events, years=sorted(set(sim.index.year)))
    # Spill counts for part years are scaled to a full year in the mean.
    step = sim.index[1] - sim.index[0]
    have = sim.groupby(sim.index.year).size()
    annual["year_fraction"] = [have.get(y, 0) / (pd.Timedelta(days=366 if calendar.isleap(y) else 365) / step)
                               for y in annual.index]
    annual = annual[annual["year_fraction"] > 0.02]
    full = annual[annual["year_fraction"] > 0.95]
    dt_s = (sim.index[1] - sim.index[0]).total_seconds()
    stored = sim["storage"].to_numpy() > 0
    a, b = (np.flatnonzero(np.diff(np.concatenate([[0], stored.astype(np.int8), [0]])) == k) for k in (1, -1))
    return {
        "mean_per_year": len(edm.count_12_24(events)) / _years(sim.index),
        "max_year": int(full["spills_12_24"].max()) if len(full) else np.nan,
        "annual": annual,
        "spill_volume_m3_per_year": float(sim["spill"].sum() * dt_s / _years(sim.index)),
        "longest_storage_use_h": float(((b - a).max() * dt_s / 3600) if len(a) else 0.0),
        "events": events,
    }


def _metric(stats: dict, metric: str, year: int | None) -> float:
    if metric == "mean":
        return stats["mean_per_year"]
    if metric == "max_year":
        return stats["max_year"]
    if metric == "year":
        return float(stats["annual"].loc[year, "spills_12_24"])
    raise ValueError(f"Unknown metric {metric!r}")


@dataclass
class SizingResult:
    volume_m3: float
    achieved: float
    stats: dict
    curve: pd.DataFrame


def storage_curve(rain: pd.Series, p: ModelParams, volumes, warmup: str = "14D",
                  q: pd.DataFrame | None = None) -> pd.DataFrame:
    q = inflow(rain, p) if q is None else q
    rows = []
    for v in volumes:
        s = spill_stats(rain, p.with_(storage_m3=p.storage_m3 + v), warmup, q)
        rows.append({"new_storage_m3": v, "spills_per_year": s["mean_per_year"], "max_year": s["max_year"],
                     "spill_volume_m3_per_year": s["spill_volume_m3_per_year"],
                     "longest_storage_use_h": s["longest_storage_use_h"]})
    return pd.DataFrame(rows)


def size_storage(rain: pd.Series, p: ModelParams, target: float = 10, metric: str = "mean",
                 year: int | None = None, tol_m3: float = 10.0, v_max: float | None = None,
                 warmup: str = "14D", curve: bool = True) -> SizingResult:
    """Smallest additional storage (m3) giving ``metric`` <= ``target``.

    ``metric`` is ``"mean"`` (average counted spills per year over the record,
    the Storm Overflows Discharge Reduction Plan basis), ``"max_year"`` (worst
    complete year) or ``"year"`` (a chosen typical year).
    """
    q = inflow(rain, p)
    stats_for = lambda v: spill_stats(rain, p.with_(storage_m3=p.storage_m3 + v), warmup, q)
    f = lambda v: _metric(stats_for(v), metric, year)
    if f(0.0) <= target:
        vol = 0.0
    else:
        # Bracket: start from the mean annual spill volume and double until the target is met.
        hi = v_max or max(stats_for(0.0)["spill_volume_m3_per_year"], 100.0)
        while f(hi) > target:
            hi *= 2
            if hi > 1e7:
                raise RuntimeError("Target not reachable with storage alone: pass-forward or emptying "
                                   "rate too low to drain the tank between storms.")
        lo = 0.0
        while hi - lo > tol_m3:
            mid = 0.5 * (lo + hi)
            if f(mid) <= target:
                hi = mid
            else:
                lo = mid
        vol = hi
    stats = stats_for(vol)
    table = pd.DataFrame()
    if curve:
        grid = np.unique(np.round(np.linspace(0, max(vol * 1.5, 100.0), 11), 0))
        table = storage_curve(rain, p, grid, warmup, q)
    return SizingResult(vol, _metric(stats, metric, year), stats, table)


def rank_volume_check(rain: pd.Series, p: ModelParams, target: float = 10, warmup: str = "14D") -> float:
    """Quick cross-check: storage equal to the (N+1)th largest spill volume,
    where N is the number of spills allowed over the record. Ignores emptying
    time and 12/24 re-counting, so it is usually an underestimate."""
    sim = simulate(rain, p)
    sim = sim[sim.index >= sim.index[0] + pd.Timedelta(warmup)]
    dt_s = (sim.index[1] - sim.index[0]).total_seconds()
    ev = edm.merge_events(edm.events_from_state(sim.index, sim["spill"].to_numpy() > 0), gap="12h")
    vols = sorted((sim.loc[(sim.index >= s) & (sim.index < e), "spill"].sum() * dt_s
                   for s, e in zip(ev["start"], ev["end"])), reverse=True)
    allowed = int(np.floor(target * _years(sim.index)))
    return float(vols[allowed]) if len(vols) > allowed else 0.0


def ensemble_sizing(rain: pd.Series, param_sets, target: float = 10, metric: str = "mean",
                    year: int | None = None, max_sets: int = 20) -> pd.DataFrame:
    """Required storage for each behavioural parameter set from calibration."""
    rows = []
    for p in list(param_sets)[:max_sets]:
        r = size_storage(rain, p, target, metric, year, curve=False)
        rows.append({"volume_m3": r.volume_m3, "area_factor": p.area_factor, "q_pff": p.q_pff})
    return pd.DataFrame(rows)


def scale_band(rain: pd.Series, p: ModelParams, sigma_scale: float, target: float = 10,
               metric: str = "mean", year: int | None = None, z: float = 1.2816) -> dict:
    """Storage if effective area and pass-forward are both scaled up or down
    together by ``exp(z * sigma_scale)`` (P90/P10 for the default ``z``).

    Spill timing cannot tell this common scale apart, so this is the main
    uncertainty in the volume unless the pass-forward has been measured.
    """
    out = {}
    for label, k in (("p10", np.exp(-z * sigma_scale)), ("p90", np.exp(z * sigma_scale))):
        q = p.with_(area_factor=p.area_factor * k, q_pff=p.q_pff * k)
        out[label] = size_storage(rain, q, target, metric, year, curve=False).volume_m3
    out["sigma_scale"] = sigma_scale
    return out


def _bisect(f, lo, hi, target, tol):
    """Smallest x in [lo, hi] with f(x) <= target, for f non-increasing."""
    if f(lo) <= target:
        return lo
    if f(hi) > target:
        return np.nan
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if f(mid) <= target:
            hi = mid
        else:
            lo = mid
    return hi


def size_area_removal(rain: pd.Series, p: ModelParams, target: float = 10, metric: str = "mean",
                      year: int | None = None) -> float:
    """Effective impermeable area (ha) to disconnect, e.g. by SuDS or
    surface water separation, to meet the target with no new storage.
    NaN if removing all impermeable area is not enough."""
    eff = p.imp_area_ha * p.area_factor
    f = lambda ha: _metric(spill_stats(rain, p.with_(area_factor=p.area_factor * (1 - ha / eff))), metric, year)
    return _bisect(f, 0.0, eff, target, 0.01)


def size_pass_forward(rain: pd.Series, p: ModelParams, target: float = 10, metric: str = "mean",
                      year: int | None = None, max_factor: float = 10.0) -> float:
    """Pass-forward flow (m3/s) needed to meet the target with no new storage.
    The downstream network and treatment works must be able to take it."""
    f = lambda q: _metric(spill_stats(rain, p.with_(q_pff=q)), metric, year)
    return _bisect(f, p.q_pff, p.q_pff * max_factor, target, 0.0005)
