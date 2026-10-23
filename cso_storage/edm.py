"""Event Duration Monitoring (EDM) spill data and EA 12/24 counting.

Two forms of published data are supported:

* event level - one row per discharge with start and stop times (water
  company storm overflow maps/APIs, or EDM data requested from the company);
* annual returns - counted spills and total duration per year, as in the
  Environment Agency's EDM annual return spreadsheets.

Event data calibrates far better (it tells the model *which* storms spilled),
so use it whenever it can be obtained.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HOUR = pd.Timedelta("1h")


def load_spill_events(
    path,
    start_col=None,
    end_col=None,
    tz: str = "Europe/London",
    site_col=None,
    site=None,
    dayfirst: bool = True,
    merge_gap: str = "0min",
) -> pd.DataFrame:
    """Load event-level EDM data as a sorted, merged ``start``/``end`` table in UTC."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    if site_col is not None and site is not None:
        df = df[df[site_col].astype(str) == str(site)]

    def pick(explicit, words):
        if explicit:
            return explicit
        for col in df.columns:
            low = col.lower()
            if any(w in low for w in words):
                return col
        raise ValueError(f"Could not find a column matching {words}; pass it explicitly.")

    start_col = pick(start_col, ["start"])
    end_col = pick(end_col, ["end", "stop", "finish"])
    out = pd.DataFrame(
        {
            "start": pd.to_datetime(df[start_col], errors="coerce", dayfirst=dayfirst),
            "end": pd.to_datetime(df[end_col], errors="coerce", dayfirst=dayfirst),
        }
    ).dropna()
    for col in ("start", "end"):
        if out[col].dt.tz is None:
            out[col] = out[col].dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
        out[col] = out[col].dt.tz_convert("UTC")
    out = out.dropna()
    bad = out["end"] < out["start"]
    if bad.any():
        raise ValueError(f"{int(bad.sum())} spill events end before they start.")
    return merge_events(out, gap=merge_gap)


def merge_events(events: pd.DataFrame, gap: str = "0min") -> pd.DataFrame:
    """Merge overlapping events, or events separated by less than ``gap``."""
    if events.empty:
        return events[["start", "end"]].reset_index(drop=True)
    ev = events.sort_values("start")[["start", "end"]].to_numpy()
    g = pd.Timedelta(gap)
    merged = [list(ev[0])]
    for s, e in ev[1:]:
        if s <= merged[-1][1] + g:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return pd.DataFrame(merged, columns=["start", "end"])


def count_blocks(starts, ends, first_block, block) -> np.ndarray:
    """EA 12/24 counting on any numeric/time axis; returns the start of each
    counted block.

    The first discharge opens a ``first_block`` (12 h) window that counts as
    one spill. Each following ``block`` (24 h) window that contains any
    discharge counts as one more. A window with no discharge ends the
    sequence and the next discharge starts a new 12 h window.
    ``starts``/``ends`` must be sorted and non-overlapping.
    """
    n = len(starts)
    counted = []
    i = 0
    while i < n:
        counted.append(starts[i])
        block_end = starts[i] + first_block
        k = i
        while True:
            while k < n and starts[k] < block_end:
                k += 1
            still_running = ends[k - 1] > block_end
            next_starts = k < n and starts[k] < block_end + block
            if not (still_running or next_starts):
                break
            counted.append(block_end)
            block_end = block_end + block
        i = k
    return np.asarray(counted)


def count_12_24(events: pd.DataFrame) -> pd.DatetimeIndex:
    """Times of each counted spill under the EA 12/24 method."""
    if events.empty:
        return pd.DatetimeIndex([], tz="UTC")
    s = events["start"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    e = events["end"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    t = count_blocks(s, e, np.timedelta64(12, "h"), np.timedelta64(24, "h"))
    return pd.DatetimeIndex(t).tz_localize("UTC")


def annual_summary(events: pd.DataFrame, years=None) -> pd.DataFrame:
    """Counted spills (12/24), raw events and total duration per calendar year."""
    counted = count_12_24(events)
    if years is None:
        years = sorted(set(events["start"].dt.year) | set(events["end"].dt.year)) if not events.empty else []
    rows = []
    for y in years:
        y0 = pd.Timestamp(year=y, month=1, day=1, tz="UTC")
        y1 = pd.Timestamp(year=y + 1, month=1, day=1, tz="UTC")
        s = events["start"].clip(lower=y0, upper=y1)
        e = events["end"].clip(lower=y0, upper=y1)
        dur = ((e - s) / HOUR).sum() if not events.empty else 0.0
        rows.append(
            {
                "year": y,
                "spills_12_24": int(((counted >= y0) & (counted < y1)).sum()),
                "events": int(((events["start"] >= y0) & (events["start"] < y1)).sum()),
                "duration_h": float(dur),
            }
        )
    return pd.DataFrame(rows).set_index("year")


def load_annual_returns(path, year_col, count_col, duration_col, site_col=None, site=None) -> pd.DataFrame:
    """Load per-year counted spills and duration from an annual return table.

    EA annual return workbooks have one sheet per year; combine them into one
    CSV with a year column first (column names vary year to year).
    """
    df = pd.read_csv(path)
    if site_col is not None and site is not None:
        df = df[df[site_col].astype(str) == str(site)]
    out = pd.DataFrame(
        {
            "year": pd.to_numeric(df[year_col], errors="coerce").astype("Int64"),
            "spills_12_24": pd.to_numeric(df[count_col], errors="coerce"),
            "duration_h": pd.to_numeric(df[duration_col], errors="coerce"),
        }
    ).dropna()
    out["year"] = out["year"].astype(int)
    return out.set_index("year")


def events_from_state(index: pd.DatetimeIndex, state: np.ndarray) -> pd.DataFrame:
    """Convert a boolean spilling/not-spilling series on a regular grid to events."""
    step = index[1] - index[0]
    edges = np.diff(np.concatenate([[0], state.astype(np.int8), [0]]))
    a, b = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return pd.DataFrame({"start": index[a], "end": index[b - 1] + step})


def flag_dry_spills(events: pd.DataFrame, rain: pd.Series, lookback: str = "24h", min_mm: float = 2.0) -> pd.Series:
    """True for spills with less than ``min_mm`` of rain in the ``lookback``
    before they started or during them. These usually point to blockages,
    pump failures, tide locking or groundwater rather than storm runoff,
    and should not be used to calibrate a rainfall-runoff model."""
    cum = rain.fillna(0).cumsum()
    def depth(t0, t1):
        a = cum.asof(t0) if t0 > cum.index[0] else 0.0
        b = cum.asof(t1) if t1 > cum.index[0] else 0.0
        return b - a
    lb = pd.Timedelta(lookback)
    in_range = (events["start"] - lb >= rain.index[0]) & (events["end"] <= rain.index[-1])
    dry = [in_r and depth(s - lb, e) < min_mm for s, e, in_r in zip(events["start"], events["end"], in_range)]
    return pd.Series(dry, index=events.index, name="dry_spill")
