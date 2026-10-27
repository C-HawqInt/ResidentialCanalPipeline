# -*- coding: utf-8 -*-
"""
canal_distance_core.py  (rebuilt 2026-10-21; v4 2026-10-22)
--------------------------------------------
Per-canal in-water distance-from-mouth rasters for the C-HAWQ canal typology
project. Rebuilt from the toolbox interface (CanalDistanceRasters.pyt), the
existing run logs (_manifest.csv, _checkpoint_done.csv) and the output naming
convention, because the original core module was lost.

Method, per canal polygon
  1. Set the processing extent to the polygon's extent plus `buffer_m`.
  2. Rasterize the polygon at `cell_size` (cell-center rule). Every water cell
     gets cost 1 (uniform cost; land and everything outside = NoData barrier).
  3. Select mouth lines: by ID (mouth_id_field == canal ID) or, if no ID field
     is given, every mouth within `touch_tolerance_m` of the polygon.
  4. Rasterize the mouths, grow them by one cell, and keep only the cells that
     fall on water. FIX vs. the original run: mouths drawn exactly on the
     polygon edge often rasterized onto the land side, leaving no source cell
     in water and producing an all-NoData raster. The one-cell growth + water
     mask guarantees the source starts in the canal (shift <= 1 cell).
  5. Distance Accumulation from the source over the cost surface (path stays in
     water and routes around land).
  6. Save as <CHAWQ_ID>_<County>_<TypeForm>_Distance.tif in a county subfolder;
     log mean / max distance to _manifest.csv and the ID to _checkpoint_done.csv.

Requires ArcGIS Pro with the Spatial Analyst extension.
"""

import csv
import datetime as _dt
import os
import re

import arcpy
import numpy as np

VERSION = ("rebuilt v4 (2026-10-22): v3 + mouth growth up to the touch tolerance "
           "and an empty-source guard")

MANIFEST_COLS = ["CHAWQ_ID", "UID", "County", "TYP_FORM", "SYSTEM_ID",
                 "n_mouths_touch", "cell_size_m", "dist_mean_m", "dist_max_m",
                 "raster_name", "status", "timestamp"]


def _safe(s):
    """'St. Johns' -> 'St_Johns', 'Branching/Dendritic' -> 'Branching_Dendritic'."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s or "NA")).strip("_") or "NA"


def _parse_ids(id_subset):
    if not id_subset:
        return None
    txt = open(id_subset).read() if os.path.isfile(id_subset) else id_subset
    return {int(float(t)) for t in re.split(r"[,\s;]+", txt) if t.strip()}


def _append_csv(path, cols, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in cols})


def _done_ids(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="") as fh:
        return {int(float(r["CHAWQ_ID"])) for r in csv.DictReader(fh)
                if r.get("CHAWQ_ID")}


def _fmt_q(field_name, value, layer):
    """SQL for field = value, quoting text fields."""
    f = [x for x in arcpy.ListFields(layer) if x.name == field_name][0]
    fld = arcpy.AddFieldDelimiters(layer, field_name)
    if f.type in ("String", "Guid", "GlobalID"):
        return "{} = '{}'".format(fld, value)
    return "{} = {}".format(fld, value)


def _distance_for_one(poly_lyr, mouth_lyr, out_tif, cell_size, buffer_m, scratch,
                      touch_tolerance_m=3.0):
    """Build cost + source rasters for the selected polygon / mouths and run
    Distance Accumulation. Returns (mean, max) or (None, None) if empty."""
    from arcpy.sa import (Con, IsNull, SetNull, FocalStatistics,
                          NbrRectangle, DistanceAccumulation)

    # Extent of the selected polygon itself. (Describe(layer).extent returns the
    # extent of the WHOLE dataset, which made the first version try to build a
    # statewide 1 m raster and hang.)
    with arcpy.da.SearchCursor(poly_lyr, ["SHAPE@"]) as cur:
        ext = next(cur)[0].extent
    arcpy.env.extent = arcpy.Extent(ext.XMin - buffer_m, ext.YMin - buffer_m,
                                    ext.XMax + buffer_m, ext.YMax + buffer_m)
    arcpy.env.cellSize = cell_size
    arcpy.env.snapRaster = None

    water_r = os.path.join(scratch, "cd_water")
    mouth_fc = os.path.join(scratch, "cd_mouth_fc")
    mouth_r = os.path.join(scratch, "cd_mouth")
    for p in (water_r, mouth_fc, mouth_r):
        if arcpy.Exists(p):
            arcpy.management.Delete(p)

    arcpy.AddMessage("    extent: {:.0f} x {:.0f} m".format(
        ext.XMax - ext.XMin + 2 * buffer_m, ext.YMax - ext.YMin + 2 * buffer_m))

    # 2. cost surface: 1 on water, NoData elsewhere
    arcpy.AddMessage("    rasterizing canal...")
    oid = arcpy.Describe(poly_lyr).OIDFieldName
    arcpy.conversion.PolygonToRaster(poly_lyr, oid, water_r, "CELL_CENTER",
                                     "NONE", cell_size)
    cost = Con(~IsNull(arcpy.Raster(water_r)), 1)

    # 4. source: mouth cells grown onto water, kept only where there is water.
    # v4: grow by 1 cell first (same as v3); if no water cell is reached, keep
    # growing one cell at a time up to the mouth-selection tolerance, so any
    # mouth that was selected (within touch_tolerance_m) can reach the canal.
    # If the source is still empty, return (None, None) instead of letting
    # DistanceAccumulation crash with ERROR 999999.
    arcpy.AddMessage("    rasterizing mouth(s)...")
    arcpy.management.CopyFeatures(mouth_lyr, mouth_fc)
    arcpy.management.AddField(mouth_fc, "SRCV", "SHORT")
    arcpy.management.CalculateField(mouth_fc, "SRCV", "1", "PYTHON3")
    arcpy.conversion.PolylineToRaster(mouth_fc, "SRCV", mouth_r,
                                      "MAXIMUM_LENGTH", "NONE", cell_size)
    source_r = os.path.join(scratch, "cd_source")
    max_grow = max(1, int(np.ceil(float(touch_tolerance_m) / float(cell_size))))
    n_src, grow_used = 0, None
    for g in range(1, max_grow + 1):
        win = 2 * g + 1
        grown = FocalStatistics(arcpy.Raster(mouth_r), NbrRectangle(win, win, "CELL"),
                                "MAXIMUM", "DATA")
        source = SetNull(IsNull(cost) | IsNull(grown), 1)
        if arcpy.Exists(source_r):
            arcpy.management.Delete(source_r)
        source.save(source_r)
        all_nd = arcpy.management.GetRasterProperties(source_r, "ALLNODATA").getOutput(0)
        if str(all_nd).strip() != "1":
            src_arr = arcpy.RasterToNumPyArray(arcpy.Raster(source_r), nodata_to_value=0)
            n_src = int((src_arr > 0).sum())
            grow_used = g
            break
    if not n_src:
        arcpy.AddWarning("    no water cell within {} cell(s) of the mouth line; "
                         "the mouth is too far from the canal.".format(max_grow))
        return None, None, None
    arcpy.AddMessage("    source: {} water cell(s), mouth grown {} cell(s)"
                     .format(n_src, grow_used))

    # 5. in-water distance from the mouth
    arcpy.AddMessage("    distance accumulation...")
    dist = DistanceAccumulation(arcpy.Raster(source_r), in_cost_raster=cost)
    dist.save(out_tif)

    arr = arcpy.RasterToNumPyArray(arcpy.Raster(out_tif), nodata_to_value=np.nan)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None, None, grow_used
    return float(arr.mean()), float(arr.max()), grow_used


def run(canals, mouths, out_folder, id_field="CHAWQ_ID", county_field="County",
        form_field="TYP_FORM", system_field="SYSTEM_ID",
        acres_field="area_acres", water_value_field="WaterVal",
        cell_size=1.0, buffer_m=50.0, touch_tolerance_m=3.0,
        county_subfolders=True, resume=True, id_subset=None,
        mouth_id_field=""):
    """Per-canal distance-from-mouth rasters, counties processed from the
    fewest canals to the most (ties broken by total acres).

    water_value_field is accepted for compatibility with the original toolbox;
    the cost is set to 1 on every water cell regardless of its value."""
    arcpy.AddMessage("canal_distance_core " + VERSION)
    arcpy.AddMessage("  loaded from: " + os.path.abspath(__file__))
    if arcpy.CheckExtension("Spatial") != "Available":
        raise RuntimeError("Spatial Analyst extension is not available.")
    arcpy.CheckOutExtension("Spatial")
    arcpy.env.overwriteOutput = True
    arcpy.env.outputCoordinateSystem = arcpy.Describe(canals).spatialReference
    scratch = arcpy.env.scratchGDB

    os.makedirs(out_folder, exist_ok=True)
    manifest = os.path.join(out_folder, "_manifest.csv")
    checkpoint = os.path.join(out_folder, "_checkpoint_done.csv")
    wanted = _parse_ids(id_subset)
    done = _done_ids(checkpoint) if resume else set()

    # Collect canals and rank counties
    fields = [id_field, county_field, form_field, acres_field]
    has_sys = system_field in [f.name for f in arcpy.ListFields(canals)]
    if has_sys:
        fields.append(system_field)
    recs = []
    with arcpy.da.SearchCursor(canals, fields) as cur:
        for row in cur:
            recs.append(dict(zip(fields, row)))
    county_n, county_ac = {}, {}
    for r in recs:
        c = r[county_field] or "NA"
        county_n[c] = county_n.get(c, 0) + 1
        county_ac[c] = county_ac.get(c, 0.0) + float(r[acres_field] or 0)
    order = sorted(county_n, key=lambda c: (county_n[c], county_ac[c]))
    rank = {c: i for i, c in enumerate(order)}

    todo = []
    for r in recs:
        cid = int(r[id_field] or 0)
        if cid == 0:
            arcpy.AddWarning("Skipping a polygon with {} = 0 (assign an ID first)."
                             .format(id_field))
            continue
        if wanted is not None and cid not in wanted:
            continue
        if cid in done:
            continue
        todo.append(r)
    todo.sort(key=lambda r: (rank[r[county_field] or "NA"], int(r[id_field])))
    if wanted:
        missing = wanted - {int(r[id_field] or 0) for r in recs}
        if missing:
            arcpy.AddWarning("IDs not found in the canal layer: {}".format(
                sorted(missing)))
    arcpy.AddMessage("{} canal(s) to process.".format(len(todo)))
    if todo:
        arcpy.AddMessage("First: {} ({})".format(int(todo[0][id_field]),
                                                todo[0][county_field]))

    poly_lyr, mouth_lyr = "cd_poly_lyr", "cd_mouth_lyr"
    for i, r in enumerate(todo, 1):
        cid = int(r[id_field])
        county = r[county_field] or "NA"
        name = "{}_{}_{}_Distance.tif".format(cid, _safe(county), _safe(r[form_field]))
        folder = os.path.join(out_folder, _safe(county)) if county_subfolders else out_folder
        os.makedirs(folder, exist_ok=True)
        out_tif = os.path.join(folder, name)
        row = {"CHAWQ_ID": cid, "County": county, "TYP_FORM": r[form_field],
               "SYSTEM_ID": r.get(system_field, "") if has_sys else "",
               "cell_size_m": cell_size,
               "timestamp": _dt.datetime.now().isoformat(timespec="seconds")}
        try:
            for lyr in (poly_lyr, mouth_lyr):
                if arcpy.Exists(lyr):
                    arcpy.management.Delete(lyr)
            arcpy.env.extent = None
            arcpy.AddMessage("[{}/{}] {} {}: selecting canal and mouth(s)..."
                             .format(i, len(todo), cid, county))
            arcpy.management.MakeFeatureLayer(canals, poly_lyr,
                                              _fmt_q(id_field, cid, canals))
            if mouth_id_field:
                arcpy.management.MakeFeatureLayer(
                    mouths, mouth_lyr, _fmt_q(mouth_id_field, cid, mouths))
            else:
                arcpy.management.MakeFeatureLayer(mouths, mouth_lyr)
                arcpy.management.SelectLayerByLocation(
                    mouth_lyr, "WITHIN_A_DISTANCE", poly_lyr,
                    "{} Meters".format(touch_tolerance_m), "NEW_SELECTION")
            n_m = int(arcpy.management.GetCount(mouth_lyr).getOutput(0))
            row["n_mouths_touch"] = n_m
            arcpy.AddMessage("[{}/{}] {} {}: {} mouth(s), computing..."
                             .format(i, len(todo), cid, county, n_m))
            if n_m == 0:
                row["status"] = "skipped_no_mouth"
                arcpy.AddWarning("[{}/{}] {}: no mouth found; skipped."
                                 .format(i, len(todo), cid))
                _append_csv(manifest, MANIFEST_COLS, row)
                continue
            mean_d, max_d, grow_used = _distance_for_one(
                poly_lyr, mouth_lyr, out_tif, cell_size, buffer_m, scratch,
                touch_tolerance_m)
            if grow_used and grow_used > 1:
                arcpy.AddWarning("[{}/{}] {}: mouth needed {} cells of growth to "
                                 "reach water (more than the usual 1)."
                                 .format(i, len(todo), cid, grow_used))
            if mean_d is None:
                row["status"] = "empty_no_source_on_water"
                row["raster_name"] = name if grow_used else ""
                arcpy.AddWarning("[{}/{}] {}: raster is empty (mouth does not "
                                 "reach a water cell). Check the mouth line."
                                 .format(i, len(todo), cid))
            else:
                row.update(status="done" if (grow_used or 1) <= 1
                           else "done_grow{}".format(grow_used),
                           raster_name=name,
                           dist_mean_m=round(mean_d, 4), dist_max_m=round(max_d, 4))
                _append_csv(checkpoint, ["CHAWQ_ID", "status"],
                            {"CHAWQ_ID": cid, "status": "done"})
                arcpy.AddMessage("[{}/{}] {} {}: mean {:.1f} m, max {:.1f} m"
                                 .format(i, len(todo), cid, county, mean_d, max_d))
        except Exception as e:  # keep going; log the failure
            row["status"] = "error: {}".format(str(e)[:150])
            arcpy.AddWarning("[{}/{}] {} failed: {}".format(i, len(todo), cid, e))
        _append_csv(manifest, MANIFEST_COLS, row)

    arcpy.env.extent = None
    arcpy.AddMessage("Finished. Log: {}".format(manifest))


def run_merged(**kwargs):
    raise NotImplementedError("Merged-system tool was not rebuilt (never used in "
                              "the final run).")


def run_connected(**kwargs):
    raise NotImplementedError("Connected-via-FLUCCS tool was not rebuilt; the "
                              "Lake Okeechobee rasters from 2026-10-13 are complete.")
