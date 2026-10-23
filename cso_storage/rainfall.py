"""Rainfall ingest (Hydromaster CSV exports) and conditioning.

Everything downstream works on a regular-step series of rainfall *depth* (mm
per step) with a UTC index. Steps with no gauge record are NaN so that gaps
can be reported and excluded from calibration rather than silently read as
dry weather.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _guess_time_col(df: pd.DataFrame) -> str:
    for col in df.columns:
        if df[col].dtype == object or "date" in str(col).lower() or "time" in str(col).lower():
            parsed = pd.to_datetime(df[col].head(50), errors="coerce", dayfirst=True)
            if parsed.notna().mean() > 0.9:
                return col
    raise ValueError("Could not find a date/time column; pass time_col explicitly.")


def _guess_value_col(df: pd.DataFrame, exclude) -> str:
    for col in df.columns:
        if col in exclude:
            continue
        vals = pd.to_numeric(df[col], errors="coerce")
        if vals.notna().mean() > 0.5:
            return col
    raise ValueError("Could not find a numeric rainfall column; pass value_col explicitly.")


def load_hydromaster(
    path,
    time_col=None,
    value_col=None,
    units: str = "mm",
    tz: str = "UTC",
    step: str = "5min",
    label: str = "end",
    tip_mm: float | None = None,
    skiprows=None,
    dayfirst: bool = True,
) -> pd.Series:
    """Load a rain gauge export and return depth (mm) per ``step``.

    Parameters
    ----------
    time_col : column name, or a list of two columns (date, time) to combine.
        Guessed if omitted.
    units : ``"mm"`` depth per record, ``"mm/h"`` intensity, or ``"tips"``
        (one row per tip, or a tip count per row; needs ``tip_mm``).
    tz : time zone the export is written in. Hydromaster is normally GMT/UTC
        all year; EDM event files are often local time (``Europe/London``).
        Getting this wrong shifts every summer spill by an hour.
    label : whether a record's timestamp marks the ``"end"`` (usual for
        loggers) or ``"start"`` of its accumulation interval.
    """
    df = pd.read_csv(path, skiprows=skiprows)
    df.columns = [str(c).strip() for c in df.columns]

    if isinstance(time_col, (list, tuple)):
        stamp = df[time_col[0]].astype(str) + " " + df[time_col[1]].astype(str)
        used = list(time_col)
    else:
        time_col = time_col or _guess_time_col(df)
        stamp = df[time_col]
        used = [time_col]
    times = pd.to_datetime(stamp, errors="coerce", dayfirst=dayfirst)

    if units == "tips" and value_col is None and len(df.columns) == len(used):
        values = pd.Series(1.0, index=df.index)
    else:
        value_col = value_col or _guess_value_col(df, used)
        values = pd.to_numeric(df[value_col], errors="coerce")

    s = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(times)).dropna()
    s = s[s.index.notna()].sort_index()
    s = s.groupby(level=0).sum()
    if s.index.tz is None:
        s.index = s.index.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
        s = s[s.index.notna()]
    s.index = s.index.tz_convert("UTC")

    if units == "tips":
        if tip_mm is None:
            raise ValueError("units='tips' needs tip_mm (e.g. 0.2).")
        depth = (s * tip_mm).resample(step).sum()
        depth.name = "rain_mm"
        return depth

    return to_regular_depth(s, units=units, step=step, label=label)


def to_regular_depth(s: pd.Series, units: str = "mm", step: str = "5min", label: str = "end") -> pd.Series:
    """Convert an (approximately) regular gauge series to depth per ``step``.

    Coarser records are spread uniformly over the finer step; finer records
    are summed. Depth is conserved either way.
    """
    if len(s) < 2:
        raise ValueError("Need at least two rainfall records.")
    raw = pd.Series(s.index).diff().median()
    target = pd.Timedelta(step)
    if units == "mm/h":
        s = s * (raw / pd.Timedelta("1h"))
    elif units != "mm":
        raise ValueError(f"Unknown units {units!r}")
    if s.lt(0).any():
        raise ValueError("Negative rainfall values in record.")

    starts = s.index - raw if label == "end" else s.index
    s = pd.Series(s.to_numpy(), index=starts)

    if raw <= target:
        out = s.resample(target).sum(min_count=1)
    else:
        n = int(round(raw / target))
        if abs(n * target - raw) > pd.Timedelta("1s"):
            raise ValueError(f"Record interval {raw} is not a multiple of step {target}.")
        idx = (s.index.values[:, None] + np.arange(n)[None, :] * target.to_timedelta64()).ravel()
        vals = np.repeat(s.to_numpy() / n, n)
        out = pd.Series(vals, index=pd.DatetimeIndex(idx, tz="UTC")).resample(target).sum(min_count=1)
    out.name = "rain_mm"
    return out


def gap_report(rain: pd.Series, min_gap: str = "1h") -> pd.DataFrame:
    """List contiguous NaN periods at least ``min_gap`` long."""
    isna = rain.isna().to_numpy()
    if not isna.any():
        return pd.DataFrame(columns=["start", "end", "hours"])
    edges = np.diff(np.concatenate([[0], isna.astype(int), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    step = rain.index[1] - rain.index[0]
    rows = []
    for a, b in zip(starts, ends):
        t0 = rain.index[a]
        t1 = rain.index[b - 1] + step
        if t1 - t0 >= pd.Timedelta(min_gap):
            rows.append({"start": t0, "end": t1, "hours": (t1 - t0) / pd.Timedelta("1h")})
    return pd.DataFrame(rows, columns=["start", "end", "hours"])


def annual_totals(rain: pd.Series) -> pd.DataFrame:
    """Annual depth and data completeness, for a sanity check against the
    long-term average annual rainfall (SAAR) for the area."""
    g = rain.groupby(rain.index.year)
    return pd.DataFrame({"depth_mm": g.sum(), "complete_frac": g.apply(lambda x: x.notna().mean())})
