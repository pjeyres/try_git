"""Catchment priors from sewer GIS and OS MasterMap Topography Layer.

Steps:

1. Trace the sewer network upstream of the overflow.
2. Allocate land to the nearest sewer (a Voronoi split along the pipes,
   limited to ``max_dist`` from any pipe). Land nearest an upstream
   *combined* sewer is taken to drain to the overflow; land nearest a surface
   water sewer, or a sewer elsewhere in the network, is not.
3. Classify MasterMap polygons into surface types and sum the impermeable
   and pervious areas inside that catchment.
4. Estimate pass-forward flow from the continuation pipe (Colebrook-White
   full bore) and from Formula A if a population is known or estimated.

These are starting values. Connection of roofs and paving to the combined
system varies street by street, so calibration to EDM data adjusts them.
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd

try:
    import geopandas as gpd
    import shapely
    from shapely.geometry import box
except ImportError:  # GIS steps are optional; the model runs from priors alone.
    gpd = None

G = 9.81
NU = 1.31e-6  # kinematic viscosity of water at 10 C (m2/s)

# (descriptiveGroup contains, make equals or None for any) -> surface class. First match wins.
MASTERMAP_RULES = [
    ("building", None, "roof"),
    ("glasshouse", None, "roof"),
    ("inland water", None, "water"),
    ("tidal water", None, "water"),
    ("road or track", "manmade", "road"),
    ("road or track", None, "track"),
    ("roadside", "manmade", "paved"),
    ("roadside", "multiple", "curtilage"),
    ("roadside", None, "pervious"),
    ("path", "manmade", "paved"),
    ("path", None, "pervious"),
    ("structure", None, "paved"),
    ("rail", None, "rail"),
    ("general surface", "manmade", "paved"),
    ("general surface", "multiple", "curtilage"),
    ("general surface", None, "pervious"),
    ("natural environment", None, "pervious"),
]

# surface class -> (impermeable fraction, fraction of that connected to the sewer)
SURFACE_PROPS = {
    "roof": (1.0, 0.95),
    "road": (1.0, 1.0),
    "paved": (1.0, 0.7),
    "curtilage": (0.35, 0.6),   # gardens and yards: drives and patios mixed with lawn
    "track": (0.3, 0.5),
    "rail": (0.1, 0.3),
    "pervious": (0.0, 0.0),
    "water": (0.0, 0.0),
    "unclassified": (0.3, 0.5),
}


def _require_gis():
    if gpd is None:
        raise ImportError("GIS steps need geopandas and shapely (pip install geopandas).")


# --------------------------------------------------------------------- network


def node_ids_from_geometry(pipes, snap_tol: float = 0.5):
    """Add ``us_node``/``ds_node`` from line end points, for GIS without node ids.
    Pipes must be digitised in the direction of flow."""
    _require_gis()
    def key(xy):
        return f"{round(xy[0] / snap_tol) * snap_tol:.2f}_{round(xy[1] / snap_tol) * snap_tol:.2f}"
    pipes = pipes.copy()
    lines = pipes.geometry.apply(lambda g: g.geoms[0] if g.geom_type == "MultiLineString" else g)
    pipes["us_node"] = [key(g.coords[0]) for g in lines]
    pipes["ds_node"] = [key(g.coords[-1]) for g in lines]
    return pipes


def trace_upstream(us_nodes, ds_nodes, outlet, stop_nodes=()) -> np.ndarray:
    """Boolean mask of pipes draining to ``outlet``. Tracing stops at
    ``stop_nodes`` (e.g. upstream overflows or pumping stations whose
    pass-forward should be modelled as a separate inflow)."""
    into = defaultdict(list)
    for i, d in enumerate(ds_nodes):
        into[d].append(i)
    mask = np.zeros(len(us_nodes), dtype=bool)
    stop = set(stop_nodes)
    seen = {outlet}
    queue = deque([outlet])
    while queue:
        node = queue.popleft()
        for i in into.get(node, ()):
            if mask[i]:
                continue
            mask[i] = True
            up = us_nodes[i]
            if up not in seen and up not in stop:
                seen.add(up)
                queue.append(up)
    return mask


def full_bore_capacity(diameter_m, gradient, ks_mm: float = 1.5):
    """Colebrook-White full-bore capacity (m3/s) of a circular pipe.
    ``ks_mm`` 1.5 is the usual value for a slimed concrete or clay sewer."""
    d = np.asarray(diameter_m, dtype=float)
    s = np.maximum(np.asarray(gradient, dtype=float), 1e-6)
    root = np.sqrt(2 * G * d * s)
    v = -2 * root * np.log10(ks_mm / 1000 / (3.7 * d) + 2.51 * NU / (d * root))
    return v * np.pi * d**2 / 4


def formula_a(population: float, g_l_hd_d: float = 140.0, infil_frac: float = 0.25,
              trade_l_d: float = 0.0) -> dict:
    """UK Formula A setting: DWF + 1360P + 2E (l/day), returned in m3/s.

    DWF = PG + I + E. ``infil_frac`` sets I as a fraction of PG.
    """
    pg = population * g_l_hd_d
    i = infil_frac * pg
    dwf = pg + i + trade_l_d
    fa = dwf + 1360 * population + 2 * trade_l_d
    per_s = 1 / 1000 / 86400
    return {"foul_m3s": (pg + trade_l_d) * per_s, "infil_m3s": i * per_s, "dwf_m3s": dwf * per_s,
            "formula_a_m3s": fa * per_s}


# --------------------------------------------------------------------- surfaces


def classify_mastermap(topo, group_col=None, make_col=None, rules=MASTERMAP_RULES):
    """Add ``surface``, ``imp_frac`` and ``connect_frac`` columns to MasterMap
    Topography Area polygons."""
    def find(candidates):
        for c in topo.columns:
            if c.lower() in candidates:
                return c
        return None
    group_col = group_col or find({"descriptivegroup", "descgroup", "descriptive_group", "description", "theme"})
    make_col = make_col or find({"make"})
    if group_col is None:
        raise ValueError("No descriptiveGroup column found in MasterMap layer; pass group_col.")
    groups = topo[group_col].astype(str).str.lower()
    makes = topo[make_col].astype(str).str.lower() if make_col else pd.Series("", index=topo.index)
    surface = pd.Series("unclassified", index=topo.index)
    done = pd.Series(False, index=topo.index)
    for grp, make, cls in rules:
        hit = ~done & groups.str.contains(grp, regex=False)
        if make is not None:
            hit &= makes == make
        surface[hit] = cls
        done |= hit
    out = topo.copy()
    out["surface"] = surface
    out["imp_frac"] = surface.map(lambda c: SURFACE_PROPS[c][0])
    out["connect_frac"] = surface.map(lambda c: SURFACE_PROPS[c][1])
    return out


def _voronoi_owner(pipes, generators, spacing, extent):
    """Voronoi cells around points spaced along the ``generators`` pipes,
    and the pipe index each cell belongs to."""
    pts, owner = [], []
    for i in np.flatnonzero(generators):
        g = pipes.geometry.iloc[i]
        if g.geom_type == "MultiLineString":
            g = shapely.line_merge(g)
            g = g.geoms[0] if g.geom_type == "MultiLineString" else g
        n = max(int(g.length // spacing), 1)
        for d in (np.arange(n) + 0.5) * g.length / n:
            pts.append(g.interpolate(d))
            owner.append(i)
    owner = np.array(owner)
    pts = np.array(pts, dtype=object)
    _, first = np.unique(shapely.get_coordinates(pts).round(2), axis=0, return_index=True)
    keep = np.sort(first)
    cells = shapely.voronoi_polygons(shapely.MultiPoint(list(pts[keep])), extend_to=extent, ordered=True)
    return np.array(list(cells.geoms), dtype=object), owner[keep]


def delineate(pipes, upstream_mask, drains_surface_mask, carries_foul_mask=None,
              max_dist: float = 60.0, spacing: float = 10.0):
    """Polygons of land draining to the upstream network.

    Returns ``(foul_catchment, surface_catchment)``:

    * land nearest an upstream foul or combined sewer, among sewers that carry
      foul flow (population and pervious area come from here);
    * land nearest an upstream combined sewer, among *all* sewers including
      surface water ones (impermeable area comes from here, so roads and
      roofs beside a surface water sewer are left out).
    """
    _require_gis()
    if carries_foul_mask is None:
        carries_foul_mask = np.ones(len(pipes), dtype=bool)
    extent = box(*pipes.total_bounds).buffer(max_dist * 3)
    reach = shapely.unary_union(pipes.geometry[upstream_mask].buffer(max_dist).to_numpy())

    cells, owner = _voronoi_owner(pipes, carries_foul_mask, spacing, extent)
    foul = shapely.unary_union(cells[upstream_mask[owner]]).intersection(reach)

    cells, owner = _voronoi_owner(pipes, np.ones(len(pipes), dtype=bool), spacing, extent)
    take = (upstream_mask & drains_surface_mask)[owner]
    surf = shapely.unary_union(cells[take]).intersection(reach) if take.any() else shapely.Polygon()
    return foul, surf


def surface_areas(topo_classified, all_up, comb_up) -> pd.DataFrame:
    """Area (ha) of each surface class inside the catchment, and the
    impermeable area connected to the combined system."""
    _require_gis()
    areas = topo_classified.geometry.intersection(all_up).area
    comb = topo_classified.geometry.intersection(comb_up).area
    df = pd.DataFrame({"surface": topo_classified["surface"], "area_ha": areas / 1e4,
                       "comb_area_ha": comb / 1e4, "imp_frac": topo_classified["imp_frac"],
                       "connect_frac": topo_classified["connect_frac"]})
    df["imp_ha"] = df["comb_area_ha"] * df["imp_frac"]
    df["contrib_imp_ha"] = df["imp_ha"] * df["connect_frac"]
    df["perv_ha"] = np.where(df["surface"] == "water", 0.0, df["area_ha"] * (1 - df["imp_frac"]))
    return df.groupby("surface")[["area_ha", "imp_ha", "contrib_imp_ha", "perv_ha"]].sum()


def estimate_population(topo_classified, all_up, occupancy: float = 2.3, min_m2: float = 25,
                        max_m2: float = 250) -> float:
    """Rough population from dwelling-sized buildings in the catchment.

    MasterMap does not say what a building is used for; AddressBase or
    billing data should replace this where available.
    """
    b = topo_classified[topo_classified["surface"] == "roof"]
    b = b[(b.area >= min_m2) & (b.area <= max_m2)]
    inside = b.geometry.representative_point().within(all_up)
    return float(inside.sum() * occupancy)


# --------------------------------------------------------------------- one call


def catchment_priors(pipes, topo, cso_node, us_col="us_node", ds_col="ds_node", diameter_col=None,
                     diameter_units="mm", us_il_col=None, ds_il_col=None, length_col=None,
                     gradient_col=None, system_col=None, combined_systems=("combined",),
                     foul_systems=("foul",), continuation_id=None, id_col=None, stop_nodes=(),
                     population=None, max_dist=60.0, ks_mm=1.5, formula_a_kw=None) -> dict:
    """Everything the lumped model needs from GIS, plus a summary for reporting."""
    _require_gis()
    if us_col not in pipes.columns or ds_col not in pipes.columns:
        pipes = node_ids_from_geometry(pipes)
        us_col, ds_col = "us_node", "ds_node"
    pipes = pipes.reset_index(drop=True)
    us = pipes[us_col].astype(str).to_numpy()
    ds = pipes[ds_col].astype(str).to_numpy()
    up = trace_upstream(us, ds, str(cso_node), [str(n) for n in stop_nodes])
    if not up.any():
        raise ValueError(f"No pipes drain to node {cso_node!r}; check node ids and pipe direction.")

    if system_col:
        sys_ = pipes[system_col].astype(str).str.lower()
        drains_surface = sys_.isin([s.lower() for s in combined_systems]).to_numpy()
        carries_foul = drains_surface | sys_.isin([s.lower() for s in foul_systems]).to_numpy()
    else:
        drains_surface = carries_foul = np.ones(len(pipes), dtype=bool)

    all_up, comb_up = delineate(pipes, up & carries_foul, drains_surface, carries_foul, max_dist=max_dist)
    topo = classify_mastermap(topo)
    topo = topo[topo.intersects(all_up)]
    table = surface_areas(topo, all_up, comb_up)

    pop_est = estimate_population(topo, all_up)
    pop = population if population is not None else pop_est
    fa = formula_a(pop, **(formula_a_kw or {}))

    # Continuation pipe: the named one, or the smallest pipe leaving the CSO node.
    out_idx = np.flatnonzero(us == str(cso_node))
    cont = None
    if continuation_id is not None and id_col:
        hit = np.flatnonzero(pipes[id_col].astype(str).to_numpy() == str(continuation_id))
        cont = int(hit[0]) if len(hit) else None
    elif len(out_idx) and diameter_col:
        cont = int(out_idx[np.argmin(pipes.loc[out_idx, diameter_col].to_numpy())])
    cap = None
    if cont is not None and diameter_col:
        d = float(pipes.loc[cont, diameter_col]) / (1000 if diameter_units == "mm" else 1)
        if gradient_col:
            s = float(pipes.loc[cont, gradient_col])
        elif us_il_col and ds_il_col:
            length = float(pipes.loc[cont, length_col]) if length_col else pipes.geometry[cont].length
            s = (float(pipes.loc[cont, us_il_col]) - float(pipes.loc[cont, ds_il_col])) / length
        else:
            s = None
        if s is not None:
            cap = float(full_bore_capacity(d, s, ks_mm))

    # The throttle setting is normally near Formula A, but cannot exceed what
    # the continuation pipe carries full bore.
    options = [q for q in (fa["formula_a_m3s"] if pop > 0 else None, cap) if q]
    q_pff = min(options) if options else None
    if q_pff is None:
        raise ValueError("No pass-forward estimate: give a population or continuation pipe size/levels.")
    return {
        "imp_area_ha": float(table["contrib_imp_ha"].sum()),
        "perv_area_ha": float(table["perv_ha"].sum()),
        "catchment_area_ha": float(all_up.area / 1e4),
        "surface_table": table,
        "population": pop,
        "population_estimated": population is None,
        "formula_a": fa,
        "continuation_capacity_m3s": cap,
        "q_pff_m3s": float(q_pff),
        "upstream_pipes": int(up.sum()),
        "catchment_geom": all_up,
        "combined_geom": comb_up,
    }
