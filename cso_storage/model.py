"""Lumped rainfall-runoff and overflow mass balance for one CSO.

This is a deliberately simplified version of what an ICM model does at a
single overflow, using the same published building blocks:

* impermeable runoff with an initial (depression storage) loss that recovers
  between storms, as in the Wallingford fixed-PR model;
* surface and pipe routing as a cascade of two linear reservoirs (the
  Wallingford double-linear-reservoir idea, lumped for the whole catchment);
* a slow, wetness-dependent response for pervious runoff and rainfall-induced
  infiltration, which drives the long-duration spills common in EDM data;
* dry weather flow with a diurnal profile;
* a pass-forward flow at the overflow, with storage that must fill before the
  weir spills, emptied back into the sewer when there is spare capacity.

What it does not do: hydrodynamic routing, backwater, surcharge, weir and
throttle hydraulics, or interaction with other overflows. Treat the volume
it gives as an optioneering estimate, and confirm the chosen option in the
verified ICM model before design.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace

import numpy as np
import pandas as pd
from scipy.signal import lfilter

# Typical UK domestic diurnal profile, hourly from midnight (local time).
DIURNAL = np.array(
    [0.60, 0.50, 0.45, 0.40, 0.40, 0.45, 0.70, 1.10, 1.45, 1.50, 1.40, 1.30,
     1.20, 1.15, 1.10, 1.05, 1.05, 1.10, 1.20, 1.25, 1.20, 1.10, 0.95, 0.75]
)
DIURNAL = DIURNAL / DIURNAL.mean()


@dataclass
class ModelParams:
    imp_area_ha: float              # contributing impermeable area from GIS/MasterMap
    perv_area_ha: float             # pervious land in the catchment
    q_pff: float                    # pass-forward flow at the overflow (m3/s)
    area_factor: float = 1.0        # calibrated multiplier on imp_area_ha
    initial_loss_mm: float = 0.5    # depression storage on impermeable surfaces
    recovery_mm_day: float = 2.5    # depression storage recovery between storms
    k_fast_min: float = 15.0        # time constant of each fast routing reservoir
    slow_coeff: float = 0.05        # fraction of pervious rainfall to slow response at full wetness
    k_slow_h: float = 24.0          # time constant of the slow response
    api_days: float = 10.0          # antecedent precipitation index decay
    api_half_mm: float = 20.0       # API giving half of full wetness
    dwf_m3s: float = 0.0            # mean foul flow (PG + E)
    infil_m3s: float = 0.0          # constant groundwater infiltration (I)
    storage_m3: float = 0.0         # storage that must fill before spill (existing + new)
    empty_rate_m3s: float | None = None  # max return rate from storage (None: limited by q_pff only)
    spill_threshold_m3s: float = 0.001   # flows below this are not recorded as a spill

    def with_(self, **kw) -> "ModelParams":
        return replace(self, **kw)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _linres(x: np.ndarray, k_s: float, dt_s: float) -> np.ndarray:
    a = np.exp(-dt_s / max(k_s, 1e-9))
    return lfilter([1 - a], [1, -a], x)


def effective_rain(rain_mm: np.ndarray, initial_loss_mm: float, recovery_mm_step: float) -> np.ndarray:
    """Rain left after filling depression storage, which empties at a fixed
    rate. Only wet steps are visited; dry spells are handled in one go."""
    eff = rain_mm.copy()
    if initial_loss_mm <= 0:
        return eff
    wet = np.flatnonzero(rain_mm > 0)
    out = []
    store = 0.0
    last = -1
    for i, r in zip(wet.tolist(), rain_mm[wet].tolist()):
        store -= recovery_mm_step * (i - last)
        if store < 0.0:
            store = 0.0
        absorbed = initial_loss_mm - store
        if absorbed > r:
            absorbed = r
        store += absorbed
        out.append(r - absorbed)
        last = i
    eff[wet] = out
    return eff


_DIURNAL_CACHE: dict = {}


def diurnal_factor(index: pd.DatetimeIndex) -> np.ndarray:
    key = (index[0], len(index), index[1] - index[0])
    if key not in _DIURNAL_CACHE:
        local = index.tz_convert("Europe/London") if index.tz is not None else index
        if len(_DIURNAL_CACHE) > 8:
            _DIURNAL_CACHE.clear()
        _DIURNAL_CACHE[key] = DIURNAL[local.hour.to_numpy()]
    return _DIURNAL_CACHE[key]


def inflow(rain: pd.Series, p: ModelParams) -> pd.DataFrame:
    """Flow arriving at the overflow (m3/s) and its components."""
    fast, slow, dwf = _components(rain, p)
    return pd.DataFrame({"fast": fast, "slow": slow, "dwf": dwf, "total": fast + slow + dwf}, index=rain.index)


def inflow_total(rain: pd.Series, p: ModelParams) -> np.ndarray:
    fast, slow, dwf = _components(rain, p)
    return fast + slow + dwf


def _components(rain: pd.Series, p: ModelParams):
    dt_s = (rain.index[1] - rain.index[0]).total_seconds()
    r = np.nan_to_num(rain.to_numpy(dtype=float), nan=0.0)

    eff = effective_rain(r, p.initial_loss_mm, p.recovery_mm_day * dt_s / 86400)
    fast_in = eff / 1000 * p.imp_area_ha * p.area_factor * 1e4 / dt_s
    k_fast = p.k_fast_min * 60
    fast = _linres(_linres(fast_in, k_fast, dt_s), k_fast, dt_s)

    api = lfilter([1.0], [1.0, -np.exp(-dt_s / (p.api_days * 86400))], r)
    wet = api / (api + p.api_half_mm)
    slow_in = p.slow_coeff * wet * r / 1000 * p.perv_area_ha * 1e4 / dt_s
    slow = _linres(slow_in, p.k_slow_h * 3600, dt_s)

    dwf = p.dwf_m3s * diurnal_factor(rain.index) + p.infil_m3s
    return fast, slow, dwf


def overflow(q_in: np.ndarray, dt_s: float, q_pff: float, storage_m3: float = 0.0,
             empty_rate_m3s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Spill flow and stored volume at the overflow.

    Flow above ``q_pff`` first fills storage; once storage is full the excess
    spills. Storage drains back when inflow drops below ``q_pff``, at up to
    ``empty_rate_m3s``.
    """
    excess = q_in - q_pff
    if storage_m3 <= 0:
        return np.maximum(excess, 0.0), np.zeros_like(q_in)

    spill = np.zeros_like(q_in)
    level = np.zeros_like(q_in)
    rate_cap = np.inf if empty_rate_m3s is None else empty_rate_m3s
    wet_steps = np.flatnonzero(excess > 0)
    n = len(q_in)
    s = 0.0
    j = 0  # pointer into wet_steps, used to skip dry weather while storage is empty
    i = 0
    while i < n:
        if s <= 0.0:
            while j < len(wet_steps) and wet_steps[j] < i:
                j += 1
            if j == len(wet_steps):
                break
            i = wet_steps[j]
        x = excess[i]
        if x > 0:
            s += x * dt_s
            if s > storage_m3:
                spill[i] = (s - storage_m3) / dt_s
                s = storage_m3
        else:
            s = max(s - min(-x, rate_cap) * dt_s, 0.0)
        level[i] = s
        i += 1
    return spill, level


def simulate(rain: pd.Series, p: ModelParams, q: pd.DataFrame | None = None) -> pd.DataFrame:
    """Run the catchment and overflow for a rainfall series. Pass ``q`` from
    :func:`inflow` to reuse it when only the overflow settings change."""
    q = (inflow(rain, p) if q is None else q).copy()
    dt_s = (rain.index[1] - rain.index[0]).total_seconds()
    spill, level = overflow(q["total"].to_numpy(), dt_s, p.q_pff, p.storage_m3, p.empty_rate_m3s)
    q["spill"] = spill
    q["storage"] = level
    q["spilling"] = spill > p.spill_threshold_m3s
    return q


def spilling(rain: pd.Series, p: ModelParams) -> np.ndarray:
    """Spill state only, without building a table (used in calibration)."""
    q = inflow_total(rain, p)
    dt_s = (rain.index[1] - rain.index[0]).total_seconds()
    spill, _ = overflow(q, dt_s, p.q_pff, p.storage_m3, p.empty_rate_m3s)
    return spill > p.spill_threshold_m3s
