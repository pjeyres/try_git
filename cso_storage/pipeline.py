"""End-to-end run from a JSON config: data in, storage estimate and report out."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import calibrate as cal
from . import edm, rainfall, sizing
from .model import ModelParams, simulate


def _priors(cfg: dict) -> dict:
    if "gis" in cfg:
        import geopandas as gpd

        from .catchment import catchment_priors

        g = dict(cfg["gis"])
        pipes = gpd.read_file(g.pop("pipes"))
        topo = gpd.read_file(g.pop("mastermap"))
        cso = g.pop("cso_node")
        pri = catchment_priors(pipes, topo, cso, population=cfg.get("population"), **g)
        if cfg.get("q_pff_l_s"):
            pri["q_pff_m3s"] = cfg["q_pff_l_s"] / 1000
        return pri
    # No GIS: priors typed straight into the config.
    p = cfg["priors"]
    from .catchment import formula_a
    fa = formula_a(p.get("population", 0))
    return {"imp_area_ha": p["imp_area_ha"], "perv_area_ha": p.get("perv_area_ha", 0.0),
            "q_pff_m3s": p["q_pff_l_s"] / 1000, "formula_a": fa, "population": p.get("population", 0),
            "population_estimated": False, "surface_table": None}


def run(cfg: dict | str | Path) -> dict:
    if not isinstance(cfg, dict):
        cfg = json.loads(Path(cfg).read_text())
    cfg = _strip_notes(cfg)
    out = Path(cfg.get("output_dir", "out"))
    out.mkdir(parents=True, exist_ok=True)

    rc = dict(cfg["rainfall"])
    rain = rainfall.load_hydromaster(rc.pop("path"), **rc)
    gaps = rainfall.gap_report(rain)

    pri = _priors(cfg)
    base = ModelParams(imp_area_ha=pri["imp_area_ha"], perv_area_ha=pri["perv_area_ha"],
                       q_pff=pri["q_pff_m3s"], dwf_m3s=pri["formula_a"]["foul_m3s"],
                       infil_m3s=pri["formula_a"]["infil_m3s"],
                       storage_m3=cfg.get("existing_storage_m3", 0.0))

    ec = cfg["edm"]
    exclude = [(pd.Timestamp(a).tz_convert("UTC") if pd.Timestamp(a).tz else pd.Timestamp(a, tz="UTC"),
                pd.Timestamp(b).tz_convert("UTC") if pd.Timestamp(b).tz else pd.Timestamp(b, tz="UTC"))
               for a, b in ec.get("exclude", [])]
    exclude += list(zip(gaps["start"], gaps["end"]))
    if "events_path" in ec:
        events = edm.load_spill_events(ec["events_path"], start_col=ec.get("start_col"),
                                       end_col=ec.get("end_col"), tz=ec.get("tz", "Europe/London"),
                                       site_col=ec.get("site_col"), site=ec.get("site"))
        dry = edm.flag_dry_spills(events, rain, min_mm=ec.get("dry_spill_mm", 2.0))
        if ec.get("exclude_dry_spills", True):
            exclude += list(zip(events.loc[dry, "start"], events.loc[dry, "end"] + pd.Timedelta("1h")))
        if "period" in ec:
            window = tuple(pd.Timestamp(t, tz="UTC") for t in ec["period"])
        else:
            window = (events["start"].min().normalize() - pd.Timedelta("7D"), events["end"].max())
        cal_rain = rain[(rain.index >= window[0] - pd.Timedelta("60D")) & (rain.index < window[1])]
        scorer = cal.Scorer.from_events(cal_rain.index, events, window=window, exclude=exclude)
        observed = edm.annual_summary(events)
        n_dry = int(dry.sum())
    else:
        observed = edm.load_annual_returns(ec["annual_path"], ec["year_col"], ec["count_col"],
                                           ec["duration_col"], ec.get("site_col"), ec.get("site"))
        observed = observed[observed.index.isin(set(rain.index.year))]
        y0 = pd.Timestamp(year=int(observed.index.min()), month=1, day=1, tz="UTC")
        cal_rain = rain[(rain.index >= y0 - pd.Timedelta("60D"))
                        & (rain.index.year <= int(observed.index.max()))]
        scorer = cal.AnnualScorer(cal_rain.index, observed, warmup="60D")
        n_dry = None

    cc = cfg.get("calibration", {})
    bounds = {k: tuple(v) for k, v in cc["bounds"].items()} if "bounds" in cc else None
    prior_sigma = {**cal.DEFAULT_PRIOR_SIGMA, **cc.get("prior_sigma", {})}
    result = cal.calibrate(cal_rain, base, scorer, bounds=bounds, maxiter=cc.get("maxiter", 40),
                           popsize=cc.get("popsize", 12), seed=cc.get("seed", 1), prior_sigma=prior_sigma)
    # Spread of the common area/pass-forward scale that spill timing cannot resolve.
    # A pass-forward left out of the bounds is treated as measured, to within its sigma
    # (default 10%).
    names = bounds or cal.DEFAULT_BOUNDS
    s_area = prior_sigma["area_factor"] if "area_factor" in names else cc.get("area_sigma_if_fixed", 0.1)
    s_q = prior_sigma["q_pff_factor"] if "q_pff_factor" in names else cc.get("q_pff_sigma_if_fixed", 0.1)
    sigma_scale = 1 / np.sqrt(1 / s_area**2 + 1 / s_q**2)
    best = result.params

    sc = cfg.get("sizing", {})
    if sc.get("empty_rate_l_s"):
        best = best.with_(empty_rate_m3s=sc["empty_rate_l_s"] / 1000)
    design_rain = rain * sc.get("rain_uplift", 1.0)
    target, metric, year = sc.get("target", 10), sc.get("metric", "mean"), sc.get("year")
    sized = sizing.size_storage(design_rain, best, target, metric, year)
    baseline = sizing.spill_stats(design_rain, best)
    rank = sizing.rank_volume_check(design_rain, best, target)
    area_removal = sizing.size_area_removal(design_rain, best, target, metric, year)
    pff_needed = sizing.size_pass_forward(design_rain, best, target, metric, year)
    band = sizing.scale_band(design_rain, best, sigma_scale, target, metric, year)
    ens = sizing.ensemble_sizing(design_rain, [p.with_(empty_rate_m3s=best.empty_rate_m3s)
                                               for p in result.behavioural],
                                 target, metric, year, max_sets=sc.get("ensemble_sets", 10))

    summary = {
        "catchment": {k: v for k, v in pri.items()
                      if k not in ("surface_table", "catchment_geom", "combined_geom", "formula_a")},
        "formula_a": pri["formula_a"],
        "rain_gaps_h": float(gaps["hours"].sum()) if len(gaps) else 0.0,
        "calibration": {"score": result.score, "metrics": result.metrics,
                        "params": {k: v for k, v in best.as_dict().items()}},
        "baseline_spills_per_year": baseline["mean_per_year"],
        "baseline_max_year": baseline["max_year"],
        "storage_m3": sized.volume_m3,
        "spills_per_year_with_storage": sized.stats["mean_per_year"],
        "max_year_with_storage": sized.stats["max_year"],
        "rank_check_m3": rank,
        "scale_band_m3": band,
        "alternatives": {"area_removal_ha": area_removal, "pass_forward_l_s": pff_needed * 1000},
        "dry_spills_flagged": n_dry,
        "ensemble_m3": {"n": len(ens), "p10": float(ens["volume_m3"].quantile(0.1)) if len(ens) else None,
                        "p50": float(ens["volume_m3"].median()) if len(ens) else None,
                        "p90": float(ens["volume_m3"].quantile(0.9)) if len(ens) else None},
        "target": target, "metric": metric,
        "rain_uplift": sc.get("rain_uplift", 1.0),
        "record_years": float(sizing._years(rain.index)),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=_jsonable))
    sized.curve.to_csv(out / "storage_curve.csv", index=False)
    ann = baseline["annual"][["spills_12_24", "duration_h"]].add_prefix("baseline_").join(
        sized.stats["annual"][["spills_12_24", "duration_h"]].add_prefix("with_storage_"))
    ann = ann.join(observed.add_prefix("observed_"), how="left")
    ann.to_csv(out / "annual_spills.csv")
    if pri.get("surface_table") is not None:
        pri["surface_table"].to_csv(out / "surface_areas.csv")
    _plots(out, cal_rain, best, sized, scorer)
    (out / "report.md").write_text(_report(summary, ann, sized.curve, pri))
    return {"summary": summary, "calibration": result, "sizing": sized, "ensemble": ens, "priors": pri}


def _strip_notes(d):
    """Drop keys starting with ``_``, used for notes in config files."""
    if isinstance(d, dict):
        return {k: _strip_notes(v) for k, v in d.items() if not str(k).startswith("_")}
    return d


def _jsonable(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    return str(x)


def _plots(out: Path, rain, p, sized, scorer):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    c = sized.curve
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(c["new_storage_m3"], c["spills_per_year"], marker="o", color="#2a6f97")
    ax.axhline(10, color="#c1121f", lw=1, ls="--")
    ax.axvline(sized.volume_m3, color="#555", lw=1, ls=":")
    ax.set_xlabel("Additional storage (m³)")
    ax.set_ylabel("Counted spills per year (12/24)")
    ax.set_title("Storage against spill frequency")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "storage_curve.png", dpi=130)
    plt.close(fig)

    if isinstance(scorer, cal.Scorer) and len(scorer.obs_a):
        sim = simulate(rain, p)
        i0 = max(scorer.obs_a[np.argmax(scorer.obs_b - scorer.obs_a)] - 288 * 5, 0)
        i1 = min(i0 + 288 * 30, len(rain))
        idx = rain.index[i0:i1]
        obs = np.zeros(len(rain), dtype=bool)
        for a, b in zip(scorer.obs_a, scorer.obs_b):
            obs[a:b] = True
        fig, ax = plt.subplots(3, 1, figsize=(9, 6), sharex=True, height_ratios=[1, 2, 1])
        ax[0].bar(idx, rain.iloc[i0:i1].fillna(0), width=pd.Timedelta(rain.index.freq or "5min"), color="#2a6f97")
        ax[0].set_ylabel("Rain (mm)")
        ax[0].invert_yaxis()
        ax[1].plot(idx, sim["total"].iloc[i0:i1] * 1000, color="#333", lw=0.8, label="Inflow")
        ax[1].axhline(p.q_pff * 1000, color="#c1121f", lw=1, ls="--", label="Pass forward")
        ax[1].set_ylabel("Flow (l/s)")
        ax[1].legend(loc="upper right", fontsize=8)
        ax[2].fill_between(idx, 0, obs[i0:i1], step="post", color="#999", alpha=0.6, label="EDM")
        ax[2].fill_between(idx, 0, -sim["spilling"].iloc[i0:i1].astype(int), step="post", color="#2a6f97",
                           alpha=0.8, label="Model")
        ax[2].set_yticks([])
        ax[2].legend(loc="upper right", fontsize=8)
        ax[2].set_ylabel("Spilling")
        fig.autofmt_xdate()
        fig.suptitle("Calibration check: wettest observed spell")
        fig.tight_layout()
        fig.savefig(out / "calibration_window.png", dpi=130)
        plt.close(fig)


def _report(s: dict, ann: pd.DataFrame, curve: pd.DataFrame, pri: dict) -> str:
    m = s["calibration"]["metrics"]
    p = s["calibration"]["params"]
    c = s["catchment"]
    lines = [
        "# CSO storage estimate", "",
        f"Target: {s['target']} counted spills per year ({s['metric']}), EA 12/24 counting. "
        f"Rainfall record {s['record_years']:.1f} years, uplift x{s['rain_uplift']}.", "",
        "## Result", "",
        f"* Additional storage needed: **{s['storage_m3']:.0f} m³**",
        f"* Spills per year: {s['baseline_spills_per_year']:.1f} now, "
        f"{s['spills_per_year_with_storage']:.1f} with storage "
        f"(worst complete year {s['baseline_max_year']} now, {s['max_year_with_storage']} with storage)",
    ]
    b = s["scale_band_m3"]
    lines.append(f"* Uncertainty from the area/pass-forward scale (P10-P90): {b['p10']:.0f} to {b['p90']:.0f} m³ "
                 f"(log-sd {b['sigma_scale']:.2f}; narrows if the pass-forward flow is measured)")
    e = s["ensemble_m3"]
    if e["n"]:
        lines.append(f"* Spread over {e['n']} equally good fits to the spill record: P10 {e['p10']:.0f}, "
                     f"P50 {e['p50']:.0f}, P90 {e['p90']:.0f} m³")
    lines.append(f"* Rank check (storage = volume of the spill that must be stopped, ignoring emptying): "
                 f"{s['rank_check_m3']:.0f} m³")
    alt = s["alternatives"]
    ok = lambda x: x == x  # False for NaN
    lines += ["", "Other ways to reach the target, each on its own with no new storage:", "",
              f"* Disconnect {alt['area_removal_ha']:.2f} ha of effective impermeable area (SuDS, separation)"
              if ok(alt["area_removal_ha"]) else "* Disconnecting impermeable area alone cannot reach the target",
              f"* Raise pass-forward to {alt['pass_forward_l_s']:.1f} l/s (if downstream capacity allows)"
              if ok(alt["pass_forward_l_s"]) else "* Raising pass-forward up to 10x alone cannot reach the target"]
    lines += ["",
              "## Catchment from GIS", "",
              f"* Contributing impermeable area (prior): {c['imp_area_ha']:.2f} ha; pervious {c['perv_area_ha']:.2f} ha",
              f"* Population: {c['population']:.0f}" + (" (estimated from building count)" if c.get("population_estimated") else ""),
              f"* Formula A: {s['formula_a']['formula_a_m3s'] * 1000:.1f} l/s; DWF {s['formula_a']['dwf_m3s'] * 1000:.2f} l/s"]
    if c.get("continuation_capacity_m3s"):
        lines.append(f"* Continuation pipe full-bore capacity: {c['continuation_capacity_m3s'] * 1000:.1f} l/s")
    lines += ["", "## Calibration", "",
              f"* Effective impermeable area: {p['imp_area_ha'] * p['area_factor']:.2f} ha "
              f"(GIS x {p['area_factor']:.2f})",
              f"* Pass-forward flow: {p['q_pff'] * 1000:.1f} l/s",
              f"* Fast response time constant: {p['k_fast_min']:.0f} min x 2 reservoirs; "
              f"slow response {p['k_slow_h']:.0f} h, coefficient {p['slow_coeff']:.3f}",
              f"* Initial loss: {p['initial_loss_mm']:.2f} mm"]
    if s.get("dry_spills_flagged"):
        lines.append(f"* {s['dry_spills_flagged']} observed spills had under 2 mm of rain in the previous 24 h "
                     "and were left out of calibration (check for blockages, pump faults or tide locking)")
    if "f1" in m:
        lines += [f"* Observed spills matched: {m['hit_rate']:.0%}; modelled spills that were observed: "
                  f"{m['success_ratio']:.0%}",
                  f"* Counted spills over the calibration window: observed {m['obs_spills_12_24']}, "
                  f"model {m['sim_spills_12_24']}",
                  f"* Spill duration: observed {m['obs_duration_h']:.0f} h, model {m['sim_duration_h']:.0f} h"]
    lines += ["", "## Spills by year", "", ann.round(1).to_markdown(), "",
              "## Storage curve", "", curve.round(1).to_markdown(index=False), "",
              "## Caveats", "",
              "This is a lumped mass-balance estimate for optioneering. It does not model backwater, "
              "surcharge, weir or throttle hydraulics, or other overflows upstream. Confirm the chosen "
              "volume, its location and its emptying arrangement in the verified ICM model."]
    return "\n".join(lines) + "\n"
