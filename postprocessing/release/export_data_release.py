"""Build the public data release for the Florida artificial residential waterway (ARW) layer.

Writes one GeoPackage (polygons, centerlines, mouth lines), a per-canal CSV
attribute table and a data dictionary CSV. Needs geopandas (>= 0.14) and pandas;
runs outside ArcGIS.

Usage:
    python export_data_release.py --polygons ResCanal_Polygons_10212026_WMDs.shp \
        --centerlines ResCanal_Centerlines_10212026_WMDs.shp \
        --distance DistanceStats_Final_by_CHAWQ_ID_20261026.csv \
        --mouth-gdb CanalTypology_PC.gdb --out-dir Data_Release_v1
"""
import argparse
import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

VERSION = "1.0.0"
MOUTH_LAYERS = ["Mouths_ReExport", "LakeO_Mouths"]
MOUTH_TOL_M = 3.0  # same as the distance tool's mouth touch tolerance

FORM = {"single channel": "Single channel", "finger": "Finger",
        "branching/dendritic": "Branching/Dendritic", "open basin": "Open basin"}
PROVENANCE = {1: "pipeline", 99: "edited"}

DICTIONARY = [
    # layer, field, type, units, description
    ("arw_polygons", "CHAWQ_ID", "integer", "", "Unique canal polygon ID. Not consecutive (IDs of polygons removed during review are not reused)."),
    ("arw_polygons", "SYSTEM_ID", "integer", "", "ID of the canal system the polygon belongs to; polygons split for distance processing share a SYSTEM_ID."),
    ("arw_polygons", "IS_SPLIT", "integer", "", "1 if the system was split into more than one polygon, else 0."),
    ("arw_polygons", "SYS_NPOLY", "integer", "", "Number of polygons in the system."),
    ("arw_polygons", "county", "text", "", "County containing the polygon."),
    ("arw_polygons", "wmd", "text", "", "Water management district: NWFWMD, SRWMD, SJRWMD, SWFWMD or SFWMD."),
    ("arw_polygons", "wbid", "text", "", "FDEP Waterbody ID (WBID) of the assessment unit the polygon falls in."),
    ("arw_polygons", "waterbody", "text", "", "FDEP waterbody name for that WBID."),
    ("arw_polygons", "huc8", "text", "", "8-digit hydrologic unit code."),
    ("arw_polygons", "canal_form", "text", "", "Planform class: Single channel, Finger, Branching/Dendritic or Open basin."),
    ("arw_polygons", "provenance", "text", "", "pipeline = polygon kept as produced by the extraction pipeline; edited = reshaped, merged, split or digitized by hand during review."),
    ("arw_polygons", "area_km2", "real", "km2", "Polygon area, NAD83(2011) Florida GDL Albers (EPSG:6439)."),
    ("arw_polygons", "perim_km", "real", "km", "Polygon perimeter (shoreline length), EPSG:6439."),
    ("arw_polygons", "centerline_km", "real", "km", "Total length of the polygon's medial-axis centerline."),
    ("arw_polygons", "dist_mean_m", "real", "m", "Mean in-water distance from the mouth over the polygon's 1 m cells."),
    ("arw_polygons", "dist_median_m", "real", "m", "Median in-water distance from the mouth."),
    ("arw_polygons", "dist_max_m", "real", "m", "Maximum in-water distance from the mouth."),
    ("arw_polygons", "dist_cover_pct", "real", "%", "Share of the polygon's cells with a distance value."),
    ("arw_centerlines", "CHAWQ_ID", "integer", "", "Polygon the centerline segment belongs to."),
    ("arw_centerlines", "length_m", "real", "m", "Segment length, EPSG:6439."),
    ("arw_mouths", "CHAWQ_ID", "integer", "", "Polygon the mouth line touches (nearest polygon within 3 m); empty if none within 3 m."),
    ("arw_mouths", "length_m", "real", "m", "Mouth line length, EPSG:6439."),
    ("arw_mouths", "source", "text", "", "Mouths_ReExport = canal mouths; LakeO_Mouths = mouths drawn at Lake Okeechobee for systems reaching open water only through flood-control canals."),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--polygons", required=True)
    ap.add_argument("--centerlines", required=True)
    ap.add_argument("--distance", required=True)
    ap.add_argument("--mouth-gdb", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    poly_src = gpd.read_file(a.polygons)
    assert poly_src.crs.to_epsg() == 6439
    dist = pd.read_csv(a.distance)

    poly = gpd.GeoDataFrame({
        "CHAWQ_ID": poly_src.CHAWQ_ID.astype(int),
        "SYSTEM_ID": poly_src.SYSTEM_ID.astype(int),
        "IS_SPLIT": poly_src.IS_SPLIT.astype(int),
        "SYS_NPOLY": poly_src.SYS_NPOLY.astype(int),
        "county": poly_src.County,
        "wmd": poly_src.WMDNAME,
        "wbid": poly_src.WBID,
        "waterbody": poly_src.WATERBODY_,
        "huc8": poly_src.HUC,
        "canal_form": poly_src.TYP_FORM.str.strip().str.lower().map(FORM),
        "provenance": poly_src.OriOrMod.map(PROVENANCE),
        "area_km2": poly_src.Area_km2,
        "perim_km": poly_src.peri_km,
    }, geometry=poly_src.geometry, crs=poly_src.crs)
    assert poly.CHAWQ_ID.is_unique
    assert poly.canal_form.notna().all() and poly.provenance.notna().all()

    d = dist.rename(columns={"DIST_MEAN": "dist_mean_m", "DIST_MED": "dist_median_m",
                             "DIST_MAX": "dist_max_m", "DIST_COVR": "dist_cover_pct"})
    d["CHAWQ_ID"] = d.CHAWQ_ID.astype(int)
    poly = poly.merge(d[["CHAWQ_ID", "centerline_km", "dist_mean_m", "dist_median_m",
                         "dist_max_m", "dist_cover_pct"]], on="CHAWQ_ID", how="left", validate="1:1")
    assert poly.dist_mean_m.notna().all()
    cols = [c for c in poly.columns if c != "geometry"] + ["geometry"]
    poly = poly[cols]

    # Centerlines: SRC_OID is the 0-based row of the polygon shapefile
    cl_src = gpd.read_file(a.centerlines)
    cl = gpd.GeoDataFrame({
        "CHAWQ_ID": poly_src.CHAWQ_ID.astype(int).to_numpy()[cl_src.SRC_OID.astype(int)],
        "length_m": cl_src.geometry.length,
    }, geometry=cl_src.geometry, crs=cl_src.crs)

    # Mouths: tag each line with the nearest polygon within the touch tolerance
    parts = []
    for lyr in MOUTH_LAYERS:
        m = gpd.read_file(a.mouth_gdb, layer=lyr).to_crs(6439)
        m = gpd.GeoDataFrame({"source": lyr}, index=m.index, geometry=m.geometry.force_2d(), crs=6439)
        parts.append(m)
    mouths = pd.concat(parts, ignore_index=True)
    mouths["length_m"] = mouths.geometry.length
    j = gpd.sjoin_nearest(mouths, poly[["CHAWQ_ID", "geometry"]], how="left",
                          max_distance=MOUTH_TOL_M, distance_col="_d")
    j = j[~j.index.duplicated(keep="first")]
    mouths["CHAWQ_ID"] = j["CHAWQ_ID"].astype("Int64")
    mouths = mouths[["CHAWQ_ID", "length_m", "source", "geometry"]]

    # Build the GeoPackage in a local temp folder, then copy it: SQLite can fail
    # on network, cloud-synced or mounted drives.
    name = f"FL_ARW_v{VERSION}.gpkg"
    with tempfile.TemporaryDirectory() as tmp:
        gpkg = Path(tmp) / name
        poly.to_file(gpkg, layer="arw_polygons", driver="GPKG")
        cl.to_file(gpkg, layer="arw_centerlines", driver="GPKG")
        mouths.to_file(gpkg, layer="arw_mouths", driver="GPKG")
        shutil.copyfile(gpkg, out / name)
    poly.drop(columns="geometry").to_csv(out / f"FL_ARW_attributes_v{VERSION}.csv", index=False)
    pd.DataFrame(DICTIONARY, columns=["layer", "field", "type", "units", "description"]).to_csv(
        out / "data_dictionary.csv", index=False)

    # Checks printed for the README
    print(f"polygons {len(poly)}  area {poly.area_km2.sum():.2f} km2  perimeter {poly.perim_km.sum():.0f} km  "
          f"centerline {cl.length_m.sum()/1000:.1f} km")
    print(poly.groupby("wmd").agg(n=("CHAWQ_ID", "size"), area_km2=("area_km2", "sum")).round(2).to_string())
    print(poly.canal_form.value_counts().to_string())
    print(poly.provenance.value_counts().to_string())
    print(f"centerline segments {len(cl)}; mouth lines {len(mouths)}, "
          f"unmatched {mouths.CHAWQ_ID.isna().sum()}, polygons with a mouth {mouths.CHAWQ_ID.nunique()}")


if __name__ == "__main__":
    main()
