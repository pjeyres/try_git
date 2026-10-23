"""Synthetic test catchment: rainfall, sewer GIS, MasterMap and EDM files.

Used by the demo and tests to check that the method recovers a known answer.
Files are written in the formats expected from the real sources, including
EDM times in UK local time and rainfall in UTC.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import edm
from .model import ModelParams, simulate


def rainfall(years: int = 6, step: str = "5min", seed: int = 0, start: str = "2018-01-01") -> pd.Series:
    """Stochastic rectangular-pulse rainfall, ~800 mm/yr, with frequent long
    winter storms and fewer, shorter, more intense summer storms."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=int(pd.Timedelta(f"{365.25 * years}D") / pd.Timedelta(step)),
                        freq=step, tz="UTC")
    rain = np.zeros(len(idx))
    dt_h = pd.Timedelta(step) / pd.Timedelta("1h")
    t = 0.0
    total_h = len(idx) * dt_h
    while True:
        doy = (idx[min(int(t / dt_h), len(idx) - 1)].dayofyear - 15) / 365.25
        winter = 0.5 * (1 + np.cos(2 * np.pi * doy))          # 1 in mid January, 0 in mid July
        t += rng.exponential(48 + 30 * (1 - winter))           # hours between storm starts
        if t >= total_h:
            break
        dur = rng.exponential(3 + 5 * winter) + 0.25
        inten = rng.exponential(0.8 + 1.2 * (1 - winter))
        a, b = int(t / dt_h), min(int((t + dur) / dt_h) + 1, len(idx))
        rain[a:b] += inten * dt_h * rng.gamma(2.0, 0.5, size=b - a)
    return pd.Series(np.round(rain, 2), index=idx, name="rain_mm")


def network(blocks: int = 5, spacing: float = 100.0, separate_cols: int = 2):
    """Street grid sewer network and MasterMap-style surfaces.

    Combined sewers run west along each street to a trunk that flows south
    to the CSO at (0, 0). The eastern ``separate_cols`` blocks are a newer
    estate with separate foul and surface water sewers; the surface water
    sewers outfall to a watercourse, so that estate's roofs and roads must not
    count towards the CSO. A row of combined sewers to the south drains
    elsewhere and must also be excluded.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point, box
    from shapely.ops import unary_union

    rows = []
    x_sep = (blocks - separate_cols) * spacing

    def add(pid, x0, y0, x1, y1, system, d_mm, us_il, ds_il, us, ds):
        rows.append({"pipe_id": pid, "us_node": us, "ds_node": ds, "system": system, "diameter_mm": d_mm,
                     "us_il": us_il, "ds_il": ds_il, "geometry": LineString([(x0, y0), (x1, y1)])})

    for j in range(1, blocks + 1):
        y = j * spacing
        for i in range(blocks, 0, -1):
            x0, x1 = i * spacing, (i - 1) * spacing
            us, ds = f"N{i}_{j}", f"N{i - 1}_{j}"
            sys_ = "Foul" if x0 > x_sep else "Combined"
            add(f"P{i}_{j}", x0, y, x1, y, sys_, 225, 50 - 0.01 * x0, 50 - 0.01 * x1, us, ds)
            if x0 > x_sep:  # surface water sewer offset 5 m, flowing east to an outfall
                add(f"S{i}_{j}", x1, y + 5, x0, y + 5, "Surface", 300, 49 + 0.01 * x1, 49 + 0.01 * x0,
                    f"S{i - 1}_{j}", f"S{i}_{j}")
        add(f"T{j}", 0, y, 0, y - spacing, "Combined", 450, 45 - 0.01 * (blocks - j), 45 - 0.01 * (blocks - j + 1),
            f"N0_{j}", f"N0_{j - 1}" if j > 1 else "CSO1")
    add("CONT", 0, 0, 0, -60, "Combined", 300, 40.0, 39.6, "CSO1", "D1")
    add("OVF", 0, 0, -60, 0, "Combined", 600, 40.2, 39.8, "CSO1", "OUTFALL")
    for i in range(1, blocks + 1):  # southern street draining away from the CSO
        add(f"X{i}", (i - 1) * spacing, -60, i * spacing, -60, "Combined", 225, 39, 38.8, f"X{i - 1}", f"X{i}")
    pipes = gpd.GeoDataFrame(rows, crs="EPSG:27700")

    road_w, side_w = 10.0, 3.0
    xs = np.arange(0, blocks + 1) * spacing
    ys = np.arange(1, blocks + 1) * spacing
    streets = [LineString([(x, ys[0] - spacing / 2), (x, ys[-1])]) for x in xs] + \
              [LineString([(xs[0], y), (xs[-1], y)]) for y in ys]
    road = unary_union([s.buffer(road_w / 2, cap_style="flat") for s in streets])
    side = unary_union([s.buffer(road_w / 2 + side_w, cap_style="flat") for s in streets]).difference(road)
    topo = [{"descriptiveGroup": "Road Or Track", "make": "Manmade", "geometry": road},
            {"descriptiveGroup": "Roadside", "make": "Manmade", "geometry": side}]
    footprint = road.union(side)
    rng = np.random.default_rng(1)
    for i in range(blocks):
        for j in range(blocks - 1):
            blk = box(xs[i], ys[j], xs[i + 1], ys[j + 1]).difference(footprint)
            if (i, j) == (1, 2):  # a park with a pond
                pond = Point(xs[i] + 50, ys[j] + 50).buffer(15)
                topo.append({"descriptiveGroup": "Inland Water", "make": "Natural", "geometry": pond})
                topo.append({"descriptiveGroup": "Natural Environment", "make": "Natural",
                             "geometry": blk.difference(pond)})
                continue
            houses = []
            for k in range(8):  # two rows of semi-detached houses fronting each street
                hx = xs[i] + 12 + k * 10
                for hy in (ys[j] + 15, ys[j + 1] - 25):
                    if rng.random() < 0.9:
                        houses.append(box(hx, hy, hx + 8, hy + 10))
            for h in houses:
                topo.append({"descriptiveGroup": "Building", "make": "Manmade", "geometry": h})
            topo.append({"descriptiveGroup": "General Surface", "make": "Multiple",
                         "geometry": blk.difference(unary_union(houses))})
    mastermap = gpd.GeoDataFrame(topo, crs="EPSG:27700").explode(index_parts=False).reset_index(drop=True)
    return pipes, mastermap


def write_inputs(folder, years: int = 6, true_factors: dict | None = None, seed: int = 0,
                 edm_years: int = 3) -> dict:
    """Write a complete synthetic input set and a config to run it.

    The "true" model is the GIS prior adjusted by ``true_factors``, as if
    connections and the throttle setting differed from the records. Spill
    events from the last ``edm_years`` are published with timing jitter and a
    monitor outage, as real EDM data would be.
    """
    from .catchment import catchment_priors

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    pipes, topo = network()
    pipes.to_file(folder / "sewers.gpkg", driver="GPKG")
    topo.to_file(folder / "mastermap.gpkg", driver="GPKG")

    rain = rainfall(years=years, seed=seed)
    # Hydromaster timestamps mark the end of each interval.
    rain_end = rain.copy()
    rain_end.index = rain.index + (rain.index[1] - rain.index[0])
    pd.DataFrame({"Date/Time": rain_end.index.strftime("%d/%m/%Y %H:%M"), "Rainfall (mm)": rain_end.to_numpy()}) \
        .to_csv(folder / "hydromaster_rain.csv", index=False)

    pri = catchment_priors(pipes, topo, "CSO1", system_col="system", diameter_col="diameter_mm",
                           us_il_col="us_il", ds_il_col="ds_il", combined_systems=("combined",),
                           foul_systems=("foul",))
    f = {"area_factor": 0.7, "q_pff_factor": 1.3, "k_fast_min": 20.0, "slow_coeff": 0.12,
         "k_slow_h": 30.0, "initial_loss_mm": 0.8, **(true_factors or {})}
    truth = ModelParams(imp_area_ha=pri["imp_area_ha"], perv_area_ha=pri["perv_area_ha"],
                        q_pff=pri["q_pff_m3s"] * f["q_pff_factor"], area_factor=f["area_factor"],
                        k_fast_min=f["k_fast_min"], slow_coeff=f["slow_coeff"], k_slow_h=f["k_slow_h"],
                        initial_loss_mm=f["initial_loss_mm"], dwf_m3s=pri["formula_a"]["foul_m3s"],
                        infil_m3s=pri["formula_a"]["infil_m3s"])

    sim = simulate(rain, truth)
    ev = edm.events_from_state(sim.index, sim["spilling"].to_numpy())
    rng = np.random.default_rng(seed + 1)
    ev = ev[ev["start"] >= rain.index[-1] - pd.Timedelta(f"{365.25 * edm_years}D")].copy()
    jit = lambda n: pd.to_timedelta(rng.normal(0, 5, n).round(), unit="min")
    ev["start"] = ev["start"] + jit(len(ev))
    ev["end"] = np.maximum(ev["end"] + jit(len(ev)), ev["start"] + pd.Timedelta("2min"))
    outage = (ev["start"].min() + pd.Timedelta("200D"), ev["start"].min() + pd.Timedelta("230D"))
    ev = ev[(ev["end"] < outage[0]) | (ev["start"] > outage[1])]
    local = lambda s: s.dt.tz_convert("Europe/London").dt.strftime("%d/%m/%Y %H:%M:%S")
    pd.DataFrame({"Site": "CSO1", "Start Time": local(ev["start"]), "Stop Time": local(ev["end"])}) \
        .to_csv(folder / "edm_events.csv", index=False)

    config = {
        "output_dir": str(folder / "out"),
        "rainfall": {"path": str(folder / "hydromaster_rain.csv"), "units": "mm", "tz": "UTC", "step": "5min"},
        "edm": {"events_path": str(folder / "edm_events.csv"), "tz": "Europe/London",
                "site_col": "Site", "site": "CSO1",
                "exclude": [[str(outage[0]), str(outage[1])]]},
        "gis": {"pipes": str(folder / "sewers.gpkg"), "mastermap": str(folder / "mastermap.gpkg"),
                "cso_node": "CSO1", "system_col": "system", "combined_systems": ["combined"],
                "foul_systems": ["foul"], "diameter_col": "diameter_mm", "us_il_col": "us_il",
                "ds_il_col": "ds_il", "max_dist": 60},
        "calibration": {"maxiter": 25, "popsize": 10, "seed": 3},
        "sizing": {"target": 10, "metric": "mean", "empty_rate_l_s": None, "rain_uplift": 1.0},
    }
    import json
    (folder / "config.json").write_text(json.dumps(config, indent=2))
    return {"truth": truth, "priors": pri, "rain": rain, "events": ev, "config": config}
