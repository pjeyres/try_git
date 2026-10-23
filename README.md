# cso_storage

Estimates the storage needed at a combined sewer overflow to bring spills down to a
target, by default 10 a year counted by the EA 12/24 method. It works from:

* rain gauge data exported from **Hydromaster**;
* published **EDM spill records**, either event start/stop times or annual returns;
* **sewer GIS** pipe layers;
* **OS MasterMap** Topography Layer.

It uses the published methods InfoWorks ICM is based on (Wallingford runoff and
routing, Colebrook-White, Formula A), lumped at one overflow so it can be calibrated to
spill records and run many times. It is for optioneering. Confirm the chosen option in
the verified ICM model. **[docs/METHOD.md](docs/METHOD.md)** explains how each data set
is used and where the method's limits are.

## Quick start

```bash
pip install -r requirements.txt

# Build a synthetic catchment with a known answer and run the full method on it
python -m cso_storage demo out/demo

# Run on real data
cp examples/config_template.json my_cso.json   # then edit paths and column names
python -m cso_storage run my_cso.json
```

Outputs in `output_dir`:

| File | Contents |
|---|---|
| `report.md` | Storage needed, uncertainty, alternatives, calibration fit, spills by year |
| `summary.json` | The same numbers, machine readable |
| `storage_curve.csv` / `.png` | Spills per year against added storage |
| `annual_spills.csv` | Observed, modelled now, and modelled with storage, by year |
| `surface_areas.csv` | MasterMap surface areas in the catchment |
| `calibration_window.png` | Rain, flow and observed against modelled spills over the wettest observed spell |

## Synthetic check

`demo` builds a 5 × 4 block street grid. It has combined sewers, a separately sewered
estate whose roofs and roads must be excluded, a park with a pond, and a sewer
draining elsewhere. Six years of stochastic rainfall drive a "true" model whose
connections and throttle differ from the GIS estimate. Three years of its spills are
published as EDM events, with timing noise and a monitor outage. The method then works
only from the files.

| | Storage for 10 spills/yr |
|---|---|
| True answer | 631 m³ |
| Estimated | 686 m³ (spills 59.7 → 9.4 a year) |
| Scale uncertainty band (P10–P90) | 502 – 911 m³ |

## Package layout

| Module | Purpose |
|---|---|
| `rainfall.py` | Hydromaster CSV ingest, resampling, gap and annual total checks |
| `edm.py` | EDM event and annual return ingest, 12/24 counting, dry-weather spill flags |
| `catchment.py` | Upstream trace, Voronoi land allocation, MasterMap classification, Colebrook-White, Formula A |
| `model.py` | Runoff, routing, DWF and overflow/storage mass balance |
| `calibrate.py` | Event matching scores and differential evolution calibration with priors |
| `sizing.py` | Storage bisection, storage curve, rank check, uncertainty band, area removal and pass-forward alternatives |
| `pipeline.py` | Config-driven end-to-end run and report |
| `synthetic.py` | Synthetic test catchment |

Tests: `python -m pytest tests`.

The synthetic catchment uses the same model structure as the method, so its
calibration fit (99% of spills matched) is better than real data will give. What it
does check is the data handling end to end: time zones, interval labelling, GIS
tracing and exclusions, MasterMap areas, 12/24 counting, and sizing.
