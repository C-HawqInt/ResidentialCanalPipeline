"""
centerline_sensitivity.py
-------------------------
Sensitivity of total centerline length to the Polygons to Centerlines settings.
Runs the toolbox's own geometry code outside ArcGIS (numpy, scipy, matplotlib,
geopandas), so no arcpy license is needed.

Usage:
    python centerline_sensitivity.py <canal_polygons.shp> [out.csv]

Settings tested: boundary point spacing 2, 3 and 5 m (pruning factor 1.5), and
pruning factors 1.0, 1.5 and 2.0 (spacing 3 m). Minimum spur 5 m, simplify
0.5 m and dead-end extension are held at the manuscript values.
"""
import math
import multiprocessing as mp
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import MultiPolygon

# Load the geometry functions from the toolbox without importing arcpy
_PYT = Path(__file__).with_name("CHAWQ_Canal_Centerline_Tools.pyt")
_src = _PYT.read_text(encoding="utf-8")
_core = _src[_src.index("def _signed_area"):_src.index("# arcpy helpers")]
_ns = {"math": math, "np": np}
exec(compile(_core, str(_PYT), "exec"), _ns)
centerlines_for_part = _ns["centerlines_for_part"]

CONFIGS = {
    "baseline":  (3.0, 1.5, 5.0, 0.5),
    "spacing_2": (2.0, 1.5, 5.0, 0.5),
    "spacing_5": (5.0, 1.5, 5.0, 0.5),
    "prune_1.0": (3.0, 1.0, 5.0, 0.5),
    "prune_2.0": (3.0, 2.0, 5.0, 0.5),
}


def polygon_length(args):
    geom, spacing, spur, min_spur, simplify = args
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    total = 0.0
    for p in parts:
        rings = [np.asarray(p.exterior.coords)] + [np.asarray(r.coords) for r in p.interiors]
        for ln in centerlines_for_part(rings, spacing, spur, min_spur, simplify, True):
            total += float(np.sum(np.hypot(*np.diff(ln, axis=0).T)))
    return total


def main(shp, out_csv="centerline_sensitivity.csv"):
    gdf = gpd.read_file(shp)
    assert gdf.crs and gdf.crs.axis_info[0].unit_name in ("metre", "meter"), "Use a projected CRS in meters"
    lengths = {}
    with mp.Pool() as pool:
        for name, cfg in CONFIGS.items():
            lengths[name] = np.array(pool.map(polygon_length, [(g,) + cfg for g in gdf.geometry], chunksize=8))
            print(f"{name}: {lengths[name].sum() / 1000:,.1f} km")
    base = lengths["baseline"]
    rows = [{"config": k, "spacing_m": CONFIGS[k][0], "prune_factor": CONFIGS[k][1],
             "total_km": v.sum() / 1000, "pct_change_vs_baseline": 100 * (v.sum() / base.sum() - 1),
             "spearman_rho_per_polygon": pd.Series(v).corr(pd.Series(base), method="spearman")}
            for k, v in lengths.items()]
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(pd.DataFrame(rows).round(3).to_string(index=False))


if __name__ == "__main__":
    main(*sys.argv[1:])
