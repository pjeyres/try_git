import numpy as np
import pandas as pd
import pytest

from cso_storage import calibrate as cal
from cso_storage import catchment, edm, rainfall, sizing
from cso_storage.model import ModelParams, effective_rain, overflow, simulate

T = lambda s: pd.Timestamp(s, tz="UTC")


def ev(*pairs):
    return pd.DataFrame({"start": [T(a) for a, _ in pairs], "end": [T(b) for _, b in pairs]})


# ------------------------------------------------------------------ 12/24 counting

def test_single_short_spill_counts_once():
    assert len(edm.count_12_24(ev(("2024-01-01 00:00", "2024-01-01 02:00")))) == 1


def test_spills_within_first_12h_count_once():
    e = ev(("2024-01-01 00:00", "2024-01-01 01:00"), ("2024-01-01 10:00", "2024-01-01 11:00"))
    assert len(edm.count_12_24(e)) == 1


def test_spill_in_next_24h_block_counts_again():
    e = ev(("2024-01-01 00:00", "2024-01-01 01:00"), ("2024-01-01 20:00", "2024-01-01 21:00"))
    assert len(edm.count_12_24(e)) == 2


def test_long_spill_counts_per_block():
    # 0-12h block, 12-36h, 36-60h: a 50 h spill touches three blocks.
    assert len(edm.count_12_24(ev(("2024-01-01 00:00", "2024-01-03 02:00")))) == 3


def test_empty_block_resets_count():
    # Second spill 40 h later: block 12-36 h is empty, so a new 12 h block starts.
    e = ev(("2024-01-01 00:00", "2024-01-01 01:00"), ("2024-01-02 16:00", "2024-01-02 17:00"))
    counted = edm.count_12_24(e)
    assert len(counted) == 2
    assert counted[1] == T("2024-01-02 16:00")


def test_merge_overlapping_events():
    e = ev(("2024-01-01 00:00", "2024-01-01 03:00"), ("2024-01-01 02:00", "2024-01-01 04:00"))
    assert len(edm.merge_events(e)) == 1


def test_annual_summary_splits_duration_at_year_end():
    e = ev(("2023-12-31 22:00", "2024-01-01 02:00"))
    a = edm.annual_summary(e)
    assert a.loc[2023, "duration_h"] == pytest.approx(2)
    assert a.loc[2024, "duration_h"] == pytest.approx(2)


def test_edm_local_time_converted_to_utc(tmp_path):
    f = tmp_path / "edm.csv"
    pd.DataFrame({"Start Time": ["01/07/2024 10:00"], "Stop Time": ["01/07/2024 11:00"]}).to_csv(f, index=False)
    e = edm.load_spill_events(f, tz="Europe/London")
    assert e["start"].iloc[0] == T("2024-07-01 09:00")  # BST is UTC+1


# ------------------------------------------------------------------ rainfall

def test_rainfall_resample_conserves_depth(tmp_path):
    idx = pd.date_range("2024-01-01 00:15", periods=96, freq="15min")
    vals = np.random.default_rng(0).gamma(0.3, 1.0, 96).round(2)
    f = tmp_path / "rain.csv"
    pd.DataFrame({"Date/Time": idx.strftime("%d/%m/%Y %H:%M"), "Rain": vals}).to_csv(f, index=False)
    r5 = rainfall.load_hydromaster(f, step="5min")
    assert r5.sum() == pytest.approx(vals.sum())
    assert r5.index[0] == T("2024-01-01 00:00")  # end-labelled record moved to interval start
    r1h = rainfall.load_hydromaster(f, step="1h")
    assert r1h.sum() == pytest.approx(vals.sum())


def test_rainfall_gap_report():
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    r = pd.Series(0.0, index=idx)
    r.iloc[10:15] = np.nan
    g = rainfall.gap_report(r)
    assert len(g) == 1 and g["hours"].iloc[0] == 5


# ------------------------------------------------------------------ hydraulics and catchment

def test_colebrook_white_satisfies_equation():
    d, s, ks = 0.3, 1 / 200, 1.5
    q = float(catchment.full_bore_capacity(d, s, ks))
    v = q / (np.pi * d**2 / 4)
    lam = 8 * 9.81 * (d / 4) * s / v**2
    re = v * d / catchment.NU
    rhs = -2 * np.log10(ks / 1000 / (3.7 * d) + 2.51 / (re * np.sqrt(lam)))
    assert 1 / np.sqrt(lam) == pytest.approx(rhs, rel=1e-6)
    assert 0.05 < q < 0.08  # 300 mm at 1 in 200 carries roughly 60-65 l/s


def test_formula_a():
    fa = catchment.formula_a(1000, g_l_hd_d=150, infil_frac=0.0)
    assert fa["formula_a_m3s"] == pytest.approx((150_000 + 1_360_000) / 86_400 / 1000)


def test_trace_upstream_stops_at_stop_nodes():
    us = ["A", "B", "C", "X", "D"]
    ds = ["B", "C", "OUT", "C", "E"]
    assert list(catchment.trace_upstream(us, ds, "OUT")) == [True, True, True, True, False]
    assert list(catchment.trace_upstream(us, ds, "OUT", stop_nodes=["B"])) == [False, True, True, True, False]


def test_mastermap_classification():
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box
    topo = gpd.GeoDataFrame({"descriptiveGroup": ["Building", "Road Or Track", "General Surface",
                                                  "General Surface", "Inland Water", "Path"],
                             "make": ["Manmade", "Manmade", "Multiple", "Natural", "Natural", "Natural"]},
                            geometry=[box(0, 0, 1, 1)] * 6)
    out = catchment.classify_mastermap(topo)
    assert list(out["surface"]) == ["roof", "road", "curtilage", "pervious", "water", "pervious"]


def test_synthetic_gis_excludes_separate_and_foreign_areas():
    pytest.importorskip("geopandas")
    from cso_storage import synthetic
    pipes, topo = synthetic.network()
    pri = catchment.catchment_priors(pipes, topo, "CSO1", system_col="system", diameter_col="diameter_mm",
                                     us_il_col="us_il", ds_il_col="ds_il")
    # Combined area is the western 3 of 5 block columns: about 60% of roads and roofs.
    roofs = topo[topo["descriptiveGroup"] == "Building"].area.sum() / 1e4
    tbl = pri["surface_table"]
    assert 0.5 * roofs < tbl.loc["roof", "imp_ha"] < 0.7 * roofs
    # Population counts every house on a foul or combined sewer (all 5 columns).
    houses = (topo["descriptiveGroup"] == "Building").sum()
    assert pri["population"] == pytest.approx(houses * 2.3, rel=0.1)
    assert pri["upstream_pipes"] == 5 * 5 + 5  # street sewers + trunk, not surface water or other branches


# ------------------------------------------------------------------ model and sizing

def test_effective_rain_initial_loss():
    r = np.array([0.3, 0.3, 0.3, 0.0, 0.0])
    assert effective_rain(r, 0.5, 0.0).sum() == pytest.approx(0.4)


def test_overflow_without_storage_spills_excess():
    q = np.array([0.0, 0.2, 0.05])
    spill, _ = overflow(q, 60, 0.1)
    assert list(spill) == pytest.approx([0, 0.1, 0])


def test_overflow_storage_absorbs_then_spills_then_drains():
    q = np.array([0.2, 0.2, 0.2, 0.0, 0.0])
    spill, level = overflow(q, 100, 0.1, storage_m3=15)
    # 10 m3 excess per step: fills 10, then 15 with 5 spilt, spills 10, then drains 10 per step.
    assert list(level) == pytest.approx([10, 15, 15, 5, 0])
    assert list(spill * 100) == pytest.approx([0, 5, 10, 0, 0])


def test_more_storage_never_spills_more():
    rng = np.random.default_rng(3)
    q = rng.gamma(0.5, 0.05, 5000)
    prev = None
    for v in [0, 50, 200, 800]:
        s, _ = overflow(q, 300, 0.03, storage_m3=v, empty_rate_m3s=0.01)
        state = s > 0
        if prev is not None:
            assert not (state & ~prev).any()
        prev = state


def _toy_rain(years=3, seed=0):
    from cso_storage import synthetic
    return synthetic.rainfall(years=years, seed=seed, step="15min")


def test_size_storage_meets_target():
    rain = _toy_rain()
    p = ModelParams(imp_area_ha=4.0, perv_area_ha=8.0, q_pff=0.015, dwf_m3s=0.002, slow_coeff=0.05)
    r = sizing.size_storage(rain, p, target=10, tol_m3=5)
    assert r.achieved <= 10
    smaller = sizing.spill_stats(rain, p.with_(storage_m3=max(r.volume_m3 - 10, 0)))
    assert smaller["mean_per_year"] > 10
    # The rank check ignores emptying and re-counting, so it should be in the same range.
    assert sizing.rank_volume_check(rain, p, 10) == pytest.approx(r.volume_m3, rel=0.5)


def _calibration_case():
    rain = _toy_rain(years=3, seed=4)
    truth = ModelParams(imp_area_ha=5.0, perv_area_ha=10.0, q_pff=0.02, dwf_m3s=0.002,
                        area_factor=0.7, k_fast_min=20, slow_coeff=0.1, k_slow_h=24, initial_loss_mm=0.8)
    sim = simulate(rain, truth)
    events = edm.events_from_state(sim.index, sim["spilling"].to_numpy())
    return rain, truth, cal.Scorer.from_events(rain.index, events)


def test_calibration_with_known_pass_forward_recovers_storage():
    rain, truth, scorer = _calibration_case()
    bounds = {k: v for k, v in cal.DEFAULT_BOUNDS.items() if k != "q_pff_factor"}
    res = cal.calibrate(rain, truth.with_(area_factor=1.0), scorer, bounds=bounds, maxiter=20, popsize=8, seed=2)
    assert res.metrics["f1"] > 0.9
    assert res.params.area_factor == pytest.approx(0.7, rel=0.15)
    want = sizing.size_storage(rain, truth, curve=False).volume_m3
    got = sizing.size_storage(rain, res.params, curve=False).volume_m3
    assert got == pytest.approx(want, rel=0.25)


def test_spill_records_fix_area_to_pass_forward_ratio():
    # With both free, timing data pins effective area / pass-forward, not each one.
    rain, truth, scorer = _calibration_case()
    res = cal.calibrate(rain, truth.with_(area_factor=1.0), scorer, maxiter=20, popsize=8, seed=2)
    ratio = res.params.area_factor / (res.params.q_pff / truth.q_pff)
    assert ratio == pytest.approx(0.7, rel=0.2)


def test_flag_dry_spills():
    idx = pd.date_range("2024-01-01", periods=96, freq="1h", tz="UTC")
    rain = pd.Series(0.0, index=idx)
    rain.iloc[30:33] = 2.0
    e = ev(("2024-01-02 08:00", "2024-01-02 10:00"), ("2024-01-03 20:00", "2024-01-03 21:00"))
    assert list(edm.flag_dry_spills(e, rain)) == [False, True]


def test_alternative_interventions_meet_target():
    rain = _toy_rain(years=2, seed=5)
    p = ModelParams(imp_area_ha=4.0, perv_area_ha=8.0, q_pff=0.015, dwf_m3s=0.002)
    ha = sizing.size_area_removal(rain, p, 10)
    q = sizing.size_pass_forward(rain, p, 10)
    assert 0 < ha < 4 and q > p.q_pff
    assert sizing.spill_stats(rain, p.with_(area_factor=1 - ha / 4))["mean_per_year"] <= 10
    assert sizing.spill_stats(rain, p.with_(q_pff=q))["mean_per_year"] <= 10
