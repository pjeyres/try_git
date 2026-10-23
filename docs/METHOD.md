# From spill records to a storage volume: method

This note explains how `cso_storage` turns four data sets into an estimate of the
storage needed at a combined sewer overflow (CSO) to bring spills down to a target,
normally **10 counted spills a year on average**, the Storm Overflows Discharge
Reduction Plan basis, with spills counted by the Environment Agency's **12/24 method**.

| Data | What it gives the method |
|---|---|
| Hydromaster rain gauge record | The storms that drive the model, both for calibration and for the long-term sizing run |
| Published spill records (EDM) | When the overflow actually spilled and for how long; this is the calibration target |
| Sewer GIS | The network upstream of the overflow, which land drains to it, the continuation pipe and so the pass-forward flow |
| OS MasterMap Topography Layer | How much of that land is roof, road, paving, garden or green space |

## Why this is not a decompiled ICM

InfoWorks ICM's licence forbids reverse engineering the software, and there is no
need to. The hydrology and hydraulics it uses are published: the Wallingford
Procedure runoff and routing models, Colebrook-White pipe friction, Formula A and
the EA counting rules. This tool takes the parts of those methods that set spill
frequency at **a single overflow** and uses them in a lumped form that runs in
seconds. It can then be calibrated against spill records and run thousands of
times to size storage.

| ICM component | Here |
|---|---|
| Subcatchments with surfaces and runoff coefficients | One lumped catchment with surface areas from MasterMap, scaled by calibration |
| Fixed/New UK PR runoff, initial loss | Initial loss with recovery on impermeable surfaces; slow response with an antecedent precipitation index (the New UK model's soil-wetness idea) |
| Wallingford double linear reservoir routing | Two linear reservoirs in series, with one lumped time constant |
| RDII / infiltration | Slow linear reservoir, plus constant infiltration in DWF |
| Hydrodynamic pipe network | Not modelled; only the overflow is |
| CSO chamber, weir, throttle | One pass-forward flow; flow above it spills |
| Storage tank with return pump | Storage that fills before spilling and drains back at a set rate |
| Long time series and spill counting | Same approach: continuous simulation, EA 12/24 counting |

The result is an **optioneering estimate**. Confirm the option you choose (volume,
location, emptying arrangement) in the verified ICM model before detailed design.

## 1. Rainfall (Hydromaster)

`rainfall.load_hydromaster` reads a CSV export and produces rainfall depth per
5 minutes, in UTC, with gaps left as gaps.

* **Time base.** Gauge loggers normally record GMT all year. EDM event files are often
  in UK local time. If the time zones are mixed up, every summer spill is an hour out
  and calibration suffers. Set `tz` for each source.
* **Interval labelling.** Logger timestamps normally mark the *end* of the interval
  (`label="end"`). Records are moved to interval start before resampling. Depth is
  conserved whether the record is finer or coarser than 5 minutes. Tipping-bucket
  tip times are also accepted (`units="tips"`, `tip_mm=0.2`).
* **Quality checks.** `gap_report` lists missing periods, which are excluded from
  calibration scoring. `annual_totals` should be compared with the area's
  standard average annual rainfall (SAAR). A gauge reading 20% low will make the
  model's area 20% too big.
* **Representativeness.** One gauge more than a few km away will miss convective summer
  storms that cause spills, and that shows up as unmatched EDM events. Use the nearest
  gauge, or radar-adjusted rainfall if Hydromaster holds it.
* **Record length.** Sizing uses the whole record. Ten years or more gives a stable
  average. With a short record, check how unusual the years were. The
  `sizing.rain_uplift` setting tests a climate change uplift.

## 2. Spill records (EDM)

`edm.load_spill_events` reads event-level data (start and stop of each discharge),
merges overlaps, and converts to UTC. `edm.count_12_24` applies the EA rule:

> The first discharge starts a 12-hour block, which counts as one spill. Each
> following 24-hour block with any discharge counts as one more. A 24-hour block with
> no discharge ends the sequence; the next discharge starts a new 12-hour block.

So a 50-hour continuous spill counts as 3, and two short spills 8 hours apart count
as 1.

**Event data is much better than annual returns.** Annual returns (`load_annual_returns`)
give only a count and total hours per year, which a model can match with the wrong
storms. Event data tells the calibration which storms caused spills and which did
not. Water companies publish near-real-time event data, and historical event data
can be requested from them.

Before calibrating:

* **Dry-weather spills** (`flag_dry_spills`): a spill with almost no rain beforehand
  points to a blockage, pump failure, tide locking or groundwater. These are excluded
  from calibration by default and listed in the report. They need a different fix
  from storage.
* **Monitor downtime**: the annual returns give monitor uptime. Put known outages under
  `edm.exclude` so the model is not penalised for spills nobody recorded.
* **Change in the system**: if the overflow was modified during the record, only
  calibrate on the period after the change (`edm.period`).

## 3. Sewer GIS: what drains to the overflow

`catchment.catchment_priors` does the following:

1. **Trace upstream** from the CSO node through pipe from/to node ids. If the GIS has
   no node ids, they are built from pipe end points, which needs pipes digitised in the
   flow direction. `stop_nodes` stops the trace at upstream overflows or pumping
   stations. Their pass-forward then becomes an inflow, which is not yet modelled; see
   *Next steps*.
2. **Allocate land to the nearest sewer.** Points every 10 m along every pipe build a
   Voronoi tessellation, and each piece of land goes to the nearest pipe, up to
   `max_dist` (60 m by default, roughly a house plus a garden from the street).
   * The **surface catchment** is land nearest an upstream *combined* sewer, measured
     against all sewers including surface water ones. Roofs and roads beside a surface
     water sewer are therefore excluded.
   * The **foul catchment** is land nearest any upstream foul or combined sewer. It
     gives population and pervious area, because houses on a separate system still
     send foul flow to the overflow.
3. **Pass-forward flow.**
   * The continuation pipe's full-bore capacity comes from Colebrook-White
     (`ks` = 1.5 mm, gradient from invert levels).
   * **Formula A** = DWF + 1360P + 2E (litres/day), where DWF = PG + I + E.
   * The prior is Formula A, capped by the continuation pipe capacity. Set `q_pff_l_s`
     if the throttle setting is known.

## 4. OS MasterMap: how much of the land is impermeable

Topography Layer polygons are classified on `descriptiveGroup` and `make`:

| MasterMap | Class | Impermeable fraction | Connected fraction |
|---|---|---|---|
| Building, Glasshouse | roof | 1.0 | 0.95 |
| Road Or Track (Manmade) | road | 1.0 | 1.0 |
| Roadside / Path / General Surface (Manmade), Structure | paved | 1.0 | 0.7 |
| General Surface / Roadside (Multiple) | curtilage: gardens, drives, yards | 0.35 | 0.6 |
| Road Or Track (not Manmade) | track | 0.3 | 0.5 |
| Rail | rail | 0.1 | 0.3 |
| Natural Environment, other Natural | pervious | 0 | 0 |
| Inland / Tidal Water | water | 0 | 0, and not counted as pervious |

**Contributing impermeable area** = Σ area in the surface catchment × impermeable
fraction × connected fraction. **Pervious area** = Σ area in the foul catchment ×
(1 − impermeable fraction).

These fractions are typical values, not measurements. Which roofs go to soakaways,
and which drives drain to the road, varies street by street. That is why calibration
applies one multiplier (`area_factor`) to the GIS area. The values are in
`SURFACE_PROPS` and `MASTERMAP_RULES` and can be edited for local knowledge (for
example a postwar estate known to have soakaways).

Population is estimated by counting dwelling-sized buildings × 2.3 people. MasterMap
does not record building use, so use AddressBase or billing counts if you have them
(`population` in the config).

## 5. The model

At each 5-minute step:

```
effective rain      = rain − filling of initial-loss store (0–2 mm), which empties at 2.5 mm/day
fast flow           = effective rain × impermeable area × area_factor, routed through 2 linear reservoirs (k_fast each)
wetness             = API / (API + 20 mm), with API an antecedent precipitation index decaying over 10 days
slow flow           = slow_coeff × wetness × rain × pervious area, routed through 1 linear reservoir (k_slow)
inflow              = fast + slow + foul DWF × diurnal profile + infiltration
storage             fills with (inflow − pass-forward) when positive, drains with (pass-forward − inflow) when negative
spill               = what is left when storage is full; counted when above 1 l/s
```

The slow component matters. EDM records are full of spills lasting a day or more after
wet spells. Those come from wet ground and infiltration, not roof runoff, and they
inflate 12/24 counts.

## 6. Calibration against spill records

Differential evolution adjusts `area_factor`, the pass-forward multiplier, `k_fast`,
`slow_coeff`, `k_slow` and initial loss to minimise:

```
(1 − F1 of matched spill events, ±2 h tolerance)
+ 0.5 |ln(model spill hours / observed spill hours)|
+ 0.5 |ln(model counted spills / observed counted spills)|
+ a small penalty for moving area or pass-forward away from the GIS and Formula A estimates
```

### The thing spill records cannot tell you

EDM data is on/off. The overflow spills when inflow exceeds pass-forward, which
fixes the **ratio** of effective area to pass-forward flow. It does not fix either one
on its own. Double both, and the model spills in exactly the same storms, but spills
twice the volume and needs twice the storage. In one synthetic test with both left free and
no penalty, the calibration matched 90%+ of spill events but the storage came out
70% too high.

The method handles this openly:

* The penalty term puts the common scale where the GIS area and Formula A estimates
  agree, in proportion to how much each is trusted (`prior_sigma`, 0.35 log-sd each
  by default).
* The report gives a **scale uncertainty band** (P10–P90) for the storage, from
  scaling area and pass-forward together.
* **To narrow it, measure one of them.** In order of usefulness:
  1. **EDM level data.** Most EDM monitors are level sensors over the weir. The raw
     level series, with the weir crest level and length, gives spill flow and
     therefore spill volume. That fixes the scale directly. Ask the water company for
     the level data behind the published events.
  2. A pass-forward measurement: a flow survey downstream, a pump rate or a throttle
     test. Then leave `q_pff_factor` out of the calibration bounds and set
     `q_pff_l_s`.
  3. A short-term flow survey in the upstream catchment, which fixes the area.

The report also gives the spread of storage across all parameter sets that fit the spill
record almost as well as the best one. This is usually smaller than the scale band.

## 7. Sizing

With the calibrated parameters, the whole rainfall record is run with extra storage
*V* at the overflow. The storage fills before the weir spills, and returns flow
when inflow drops below pass-forward, optionally limited by a pump rate
(`empty_rate_l_s`). Bisection finds the smallest *V* for which counted spills meet
the target:

* `metric: "mean"`: average counted spills per year over the record (the default)
* `metric: "max_year"`: the worst complete year
* `metric: "year"`: one chosen typical year

The report also gives:

* a **storage curve**: spills/year, spill volume and longest storage use at a range
  of volumes, which shows diminishing returns;
* a **rank check**: the volume of the spill that must be eliminated if *N* are
  allowed. It ignores emptying time and 12/24 re-counting, so it is a quick sanity
  check, not the answer;
* **other interventions on the same basis**: the impermeable area that would have to
  be disconnected (SuDS, surface water separation), and the pass-forward flow that
  would meet the target without storage.

Emptying time matters. A tank still part full when the next storm arrives spills
earlier, and long wet spells keep adding 12/24 counts. The `longest_storage_use_h`
column shows how long the tank stays in use.

## 8. Handing over to ICM

1. Add a storage node or tank of the chosen volume upstream of the CSO weir, with the
   return pump or throttle used here.
2. Run the same Hydromaster series as time-varying rainfall. Use the same climate
   uplift.
3. Count spills from the overflow link with the 12/24 method and compare with
   `storage_curve.csv`.
4. Where ICM and this tool differ, the cause is usually network attenuation or
   backwater, which this tool does not represent. ICM is the authority.

## Limitations

* One overflow at a time. Overflows upstream or downstream that interact are not
  modelled.
* No hydrodynamics: surcharge, backwater from the receiving water or the works, and
  attenuation in large pipes are ignored. Their combined effect is absorbed into the
  calibrated time constants and pass-forward.
* Spatially uniform rainfall from one gauge.
* Area/pass-forward scale ambiguity, as above.

## Next steps

* Use EDM level data to calibrate on spill volume, not just timing.
* Model chains of overflows, with upstream pass-forward as an inflow.
* Accept radar rainfall grids and areal averaging.
* Use stochastic or UKCP18-perturbed rainfall series for climate change sizing.
* Read ICM network exports (CSV/SHP) directly, as an alternative to the raw GIS.
