"""
pipeline_core.py
----------------
Core arcpy processing for the Florida residential canal footprint pipeline.

Stages
------
1. fetch_nhd_canals          - pull NHD canal/ditch features for FL
2. fetch_residential_parcels - pull residential-classified parcels by county
3. filter_canals_residential - keep only canals whose buffer is dominated
                               by residential parcels
4. run_full_pipeline         - chain 1-3 with caching

All outputs land in a single file geodatabase. Each stage checks for an
existing cached feature class and skips work unless `refresh=True`.

Designed for ArcGIS Pro 3.x, Advanced license. Runs end-to-end inside Pro
(invoke from the toolbox or the Python window).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import arcpy

from data_sources import (
    CANAL_FCODES,
    COUNTY_FIELD,
    DOR_FIELD,
    FLORIDA_COUNTIES,
    NHD_FLOWLINE_LAYER,
    NHD_SERVICE,
    PARCEL_REST_URL,
    REST_OUT_SR,
    RESIDENTIAL_DOR_CODES,
    PARCEL_OUT_FIELDS,
    RESIDENTIAL_DOR_WHERE,
    WORKING_SR_EPSG,
    features_to_json_file,
    get_layer_metadata,
    query_paginated,
    query_by_ids,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Florida bounding envelope in WGS84. Used to spatially filter the NHD pull
# rather than pulling the full national dataset and clipping after.
FL_ENVELOPE_WGS84: dict = {
    "xmin": -87.65, "ymin": 24.40,
    "xmax": -79.95, "ymax": 31.05,
    "spatialReference": {"wkid": 4326},
}

# Output feature class names inside the cache gdb.
FC_NHD_CANALS_RAW = "nhd_canals_fl_raw"
FC_NHD_CANALS = "nhd_canals_fl"
FC_PARCELS = "residential_parcels_fl"
FC_CANALS_RESIDENTIAL = "canals_residential"


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------
def ensure_gdb(gdb_path: str | Path) -> str:
    """Create the file geodatabase if it doesn't exist. Return its path."""
    gdb_path = str(gdb_path)
    if not gdb_path.lower().endswith(".gdb"):
        raise ValueError(f"Expected a .gdb path, got: {gdb_path}")
    if arcpy.Exists(gdb_path):
        return gdb_path
    parent = os.path.dirname(gdb_path) or "."
    name = os.path.basename(gdb_path)
    Path(parent).mkdir(parents=True, exist_ok=True)
    arcpy.management.CreateFileGDB(parent, name)
    arcpy.AddMessage(f"Created file geodatabase: {gdb_path}")
    return gdb_path


def _fc_path(gdb: str, name: str) -> str:
    return os.path.join(gdb, name)


def _delete_if_exists(path: str) -> None:
    if arcpy.Exists(path):
        arcpy.management.Delete(path)


def _project_to_working_sr(in_fc: str, out_fc: str) -> str:
    """Project to the working spatial reference (FL Albers) for area math."""
    sr = arcpy.SpatialReference(WORKING_SR_EPSG)
    arcpy.management.Project(in_fc, out_fc, sr)
    return out_fc


# ---------------------------------------------------------------------------
# Stage 1: NHD canals
# ---------------------------------------------------------------------------
def fetch_nhd_canals(
    gdb: str,
    refresh: bool = False,
    aoi_envelope: dict | None = None,
) -> str:
    """
    Pull NHD flowlines classified as canal/ditch within Florida and write them
    to the cache geodatabase. Returns the path to the cached feature class.

    Parameters
    ----------
    gdb : str
        Path to a file geodatabase. Created if it doesn't exist.
    refresh : bool
        If True, re-pull even if the cached feature class already exists.
    aoi_envelope : dict or None
        Optional Esri JSON envelope to limit the query. Defaults to the
        Florida statewide envelope.
    """
    ensure_gdb(gdb)
    out_fc = _fc_path(gdb, FC_NHD_CANALS)
    raw_fc = _fc_path(gdb, FC_NHD_CANALS_RAW)

    if arcpy.Exists(out_fc) and not refresh:
        arcpy.AddMessage(f"NHD canals cached: {out_fc} (use refresh to re-pull)")
        return out_fc

    layer_url = f"{NHD_SERVICE}/{NHD_FLOWLINE_LAYER}"
    aoi = aoi_envelope or FL_ENVELOPE_WGS84
    fcode_list = ",".join(str(c) for c in CANAL_FCODES)
    where = f"FCode IN ({fcode_list})"

    arcpy.AddMessage(f"Querying NHD flowlines: where={where}")
    arcpy.AddMessage(f"  service: {layer_url}")

    # Pull layer metadata so we can mirror the field schema in the JSON file.
    meta = get_layer_metadata(layer_url)
    fields = meta.get("fields", [])
    geom_type = meta.get("geometryType", "esriGeometryPolyline")

    # Stream features to JSON, then convert to a feature class.
    json_path = Path(arcpy.env.scratchFolder) / "nhd_canals_fl.json"
    feats = query_paginated(
        layer_url,
        where=where,
        geometry=aoi,
        out_sr=REST_OUT_SR,
        page_size=1000,
    )
    n = 0
    feat_list = []
    for f in feats:
        feat_list.append(f)
        n += 1
        if n % 5000 == 0:
            arcpy.AddMessage(f"  fetched {n:,} canal features...")
    arcpy.AddMessage(f"  total fetched: {n:,}")

    if n == 0:
        raise RuntimeError(
            "NHD query returned 0 features. Check the service URL and FCode "
            "filter in data_sources.py."
        )

    features_to_json_file(
        feat_list,
        spatial_reference=REST_OUT_SR,
        geometry_type=geom_type,
        fields=fields,
        out_path=json_path,
    )

    _delete_if_exists(raw_fc)
    arcpy.conversion.JSONToFeatures(str(json_path), raw_fc)

    # Reproject into the working spatial reference (FL Albers, equal-area).
    _delete_if_exists(out_fc)
    _project_to_working_sr(raw_fc, out_fc)
    _delete_if_exists(raw_fc)
    try:
        json_path.unlink()
    except OSError:
        pass

    count = int(arcpy.management.GetCount(out_fc).getOutput(0))
    arcpy.AddMessage(f"NHD canals cached: {out_fc} ({count:,} features)")
    return out_fc


# ---------------------------------------------------------------------------
# Stage 2: Residential parcels
# ---------------------------------------------------------------------------
def fetch_residential_parcels(
    gdb: str,
    counties: Iterable[str] | None = None,
    parcel_url: str = PARCEL_REST_URL,
    refresh: bool = False,
) -> str:
    """
    Pull residential-classified parcels for the requested counties. Appends
    each county's results to a single output feature class. Skips counties
    that already exist in the output unless `refresh=True`.

    Parameters
    ----------
    gdb : str
        Path to the cache file geodatabase.
    counties : iterable of str or None
        County names (matching keys in FLORIDA_COUNTIES). None = all 67.
    parcel_url : str
        REST URL for the statewide cadastral feature service. Override here
        if the FGIO/FDOT URL changes.
    refresh : bool
        If True, drop any existing parcel cache and re-pull from scratch.
    """
    ensure_gdb(gdb)
    out_fc = _fc_path(gdb, FC_PARCELS)
    counties = list(counties) if counties else list(FLORIDA_COUNTIES.keys())

    if refresh:
        _delete_if_exists(out_fc)

    # Determine which counties are already cached so we can resume.
    # CO_NO is Double in the source layer; coerce to int for comparison.
    completed: set[int] = set()
    if arcpy.Exists(out_fc):
        with arcpy.da.SearchCursor(out_fc, [COUNTY_FIELD]) as cur:
            for row in cur:
                if row[0] is not None:
                    completed.add(int(row[0]))

    todo = [c for c in counties if FLORIDA_COUNTIES[c] not in completed]
    if not todo:
        arcpy.AddMessage(f"All requested counties already cached: {out_fc}")
        return out_fc

    arcpy.AddMessage(f"Fetching parcels for {len(todo)} counties: {', '.join(todo)}")

    meta = get_layer_metadata(parcel_url)
    all_fields = meta.get("fields", [])
    requested = set(PARCEL_OUT_FIELDS)
    fields = [f for f in all_fields if f.get("name") in requested]
    geom_type = meta.get("geometryType", "esriGeometryPolygon")

    for county_name in todo:
        co_no = FLORIDA_COUNTIES[county_name]  # int (FIPS)
        # CO_NO is numeric in the FDOR Cadastral layer — no quotes around it.
        # Use the compact BETWEEN expression instead of a 308-element IN list
        # to keep request payloads small.
        where = f"{COUNTY_FIELD} = {co_no} AND {RESIDENTIAL_DOR_WHERE}"
        arcpy.AddMessage(f"  {county_name} (CO_NO={co_no}): {where}")

        feats = list(query_by_ids(
            parcel_url,
            where=where,
            geometry=FL_ENVELOPE_WGS84,  # limit the query to Florida's extent
            out_fields=",".join(PARCEL_OUT_FIELDS),
            out_sr=REST_OUT_SR,
            batch_size=200,
        ))
        if not feats:
            arcpy.AddWarning(
                f"  {county_name}: 0 features returned. Check DOR_FIELD / "
                f"COUNTY_FIELD names in data_sources.py."
            )
            continue

        json_path = Path(arcpy.env.scratchFolder) / f"parcels_{co_no}.json"
        features_to_json_file(
            feats,
            spatial_reference=REST_OUT_SR,
            geometry_type=geom_type,
            fields=fields,
            out_path=json_path,
        )
        tmp_fc = _fc_path(arcpy.env.scratchGDB, f"parcels_{co_no}_raw")
        _delete_if_exists(tmp_fc)
        arcpy.conversion.JSONToFeatures(str(json_path), tmp_fc)

        # Project and append/initialize.
        proj_fc = _fc_path(arcpy.env.scratchGDB, f"parcels_{co_no}_proj")
        _delete_if_exists(proj_fc)
        _project_to_working_sr(tmp_fc, proj_fc)

        if arcpy.Exists(out_fc):
            arcpy.management.Append(proj_fc, out_fc, schema_type="NO_TEST")
        else:
            arcpy.management.CopyFeatures(proj_fc, out_fc)

        # Cleanup
        _delete_if_exists(tmp_fc)
        _delete_if_exists(proj_fc)
        try:
            json_path.unlink()
        except OSError:
            pass

        n = len(feats)
        arcpy.AddMessage(f"  {county_name}: cached {n:,} residential parcels")

    total = int(arcpy.management.GetCount(out_fc).getOutput(0))
    arcpy.AddMessage(f"Residential parcels cached: {out_fc} ({total:,} features)")
    return out_fc


# ---------------------------------------------------------------------------
# Stage 3: Spatial filter — keep canals dominated by residential parcels
# ---------------------------------------------------------------------------
def filter_canals_by_residential(
    canals_fc: str,
    parcels_fc: str,
    out_fc: str,
    buffer_m: float = 50.0,
    threshold: float = 0.5,
    keep_intermediate: bool = False,
) -> str:
    """
    Keep canals whose buffered footprint is dominated by residential parcels.

    Algorithm:
      1. Buffer each canal feature by `buffer_m` meters (FULL, ROUND ends),
         no dissolve so each canal keeps its identity.
      2. TabulateIntersection of buffer (zone) vs. parcels (class) — gives
         the percentage of each buffer covered by residential parcels.
      3. Select canals whose buffer percentage exceeds `threshold` * 100.

    Parameters
    ----------
    canals_fc : str
        Input canal feature class (output of fetch_nhd_canals).
    parcels_fc : str
        Input residential parcels feature class.
    out_fc : str
        Output feature class for canals that pass the threshold.
    buffer_m : float
        Buffer distance in meters.
    threshold : float
        Minimum residential-coverage fraction (0-1) to keep a canal.
    keep_intermediate : bool
        If True, leave the buffer and tabulation table in place (debugging).

    Returns
    -------
    str
        Path to the output feature class.
    """
    if not arcpy.Exists(canals_fc):
        raise FileNotFoundError(f"Canals feature class not found: {canals_fc}")
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels feature class not found: {parcels_fc}")

    workspace = arcpy.env.scratchGDB
    buffer_fc = os.path.join(workspace, "canal_buffers_tmp")
    tab_table = os.path.join(workspace, "canal_residential_tab")
    _delete_if_exists(buffer_fc)
    _delete_if_exists(tab_table)
    _delete_if_exists(out_fc)

    arcpy.AddMessage(f"Buffering canals at {buffer_m} m...")
    arcpy.analysis.Buffer(
        in_features=canals_fc,
        out_feature_class=buffer_fc,
        buffer_distance_or_field=f"{buffer_m} Meters",
        line_side="FULL",
        line_end_type="ROUND",
        dissolve_option="NONE",
    )

    # OBJECTID survives buffer and serves as the per-canal zone key.
    arcpy.AddMessage("Tabulating intersection with residential parcels...")
    arcpy.analysis.TabulateIntersection(
        in_zone_features=buffer_fc,
        zone_fields=["ORIG_FID"] if _has_field(buffer_fc, "ORIG_FID") else ["OBJECTID"],
        in_class_features=parcels_fc,
        out_table=tab_table,
    )
    zone_field = "ORIG_FID" if _has_field(tab_table, "ORIG_FID") else "OBJECTID_1"

    threshold_pct = threshold * 100.0
    passing_ids: set[int] = set()
    with arcpy.da.SearchCursor(tab_table, [zone_field, "PERCENTAGE"]) as cur:
        for row in cur:
            if row[1] is not None and row[1] >= threshold_pct:
                passing_ids.add(int(row[0]))

    arcpy.AddMessage(
        f"{len(passing_ids):,} canals passed threshold "
        f"({threshold_pct:.1f}% residential coverage)"
    )

    if not passing_ids:
        # Make an empty copy with the same schema and exit cleanly.
        arcpy.management.CreateFeatureclass(
            os.path.dirname(out_fc),
            os.path.basename(out_fc),
            template=canals_fc,
            spatial_reference=arcpy.Describe(canals_fc).spatialReference,
            geometry_type="POLYLINE",
        )
        return out_fc

    # Mark passing canals via an in-memory selection on the original FC.
    # Iterating rows is fine for ~tens of thousands of canals; we don't
    # build a giant SQL IN list.
    canals_layer = "canals_filter_lyr"
    if arcpy.Exists(canals_layer):
        arcpy.management.Delete(canals_layer)
    arcpy.management.MakeFeatureLayer(canals_fc, canals_layer)

    # Use a join-by-attribute approach: write passing ids to a tiny scratch
    # table, then add join on OBJECTID and copy the joined-non-null rows.
    pass_table = os.path.join(workspace, "passing_ids")
    _delete_if_exists(pass_table)
    arcpy.management.CreateTable(workspace, "passing_ids")
    arcpy.management.AddField(pass_table, "CANAL_OID", "LONG")
    with arcpy.da.InsertCursor(pass_table, ["CANAL_OID"]) as ic:
        for oid in passing_ids:
            ic.insertRow((oid,))

    canals_oid_field = arcpy.Describe(canals_fc).OIDFieldName
    arcpy.management.AddJoin(
        canals_layer, canals_oid_field,
        pass_table, "CANAL_OID",
        join_type="KEEP_COMMON",
    )
    arcpy.management.CopyFeatures(canals_layer, out_fc)
    arcpy.management.RemoveJoin(canals_layer)

    if not keep_intermediate:
        _delete_if_exists(buffer_fc)
        _delete_if_exists(tab_table)
        _delete_if_exists(pass_table)

    n_out = int(arcpy.management.GetCount(out_fc).getOutput(0))
    arcpy.AddMessage(f"Residential canals written: {out_fc} ({n_out:,} features)")
    return out_fc


def _has_field(table: str, field_name: str) -> bool:
    return any(f.name == field_name for f in arcpy.ListFields(table))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 4 (optional): Network expansion — catch overlooked canals
# ---------------------------------------------------------------------------
def expand_residential_canals(
    full_canals_fc: str,
    passed_canals_fc: str,
    parcels_fc: str,
    out_fc: str,
    *,
    connectivity_distance_m: float = 5.0,
    relaxed_threshold: float = 0.25,
    buffer_m: float = 50.0,
    iterations: int = 3,
    keep_intermediate: bool = False,
) -> str:
    """
    Catch residential canals that the primary filter overlooked by walking
    outward through the canal network.

    Why this exists
    ---------------
    The strict 0.5 residential-coverage threshold misses canals at the edge
    of residential developments where one bank faces a preserve, park, or
    common-area parcel that isn't classified residential. These canals are
    clearly part of the residential network — their downstream and upstream
    neighbors are residential — but their isolated coverage drops below 0.5.

    Algorithm
    ---------
    1. Pre-compute residential parcel coverage for every NHD canal once
       (single buffer + TabulateIntersection pass over the full set).
    2. Initialize the result with all canals from `passed_canals_fc`.
    3. Each iteration:
         a. Find candidates: canals in `full_canals_fc` not yet in the
            result but spatially within `connectivity_distance_m` of one.
         b. Add candidates whose pre-computed residential coverage is at
            least `relaxed_threshold`.
         c. Stop early if no new additions in an iteration.
    4. Combine and tag every output feature with SOURCE = "primary" (came
       from passed_canals_fc) or SOURCE = "network" (rescued by expansion).

    Parameters
    ----------
    full_canals_fc : str
        All NHD canal/ditch flowlines (the unfiltered universe — typically
        nhd_canals_fl from Stage 1).
    passed_canals_fc : str
        Canals that already passed Tool 3 (canals_residential).
    parcels_fc : str
        Residential parcels feature class.
    out_fc : str
        Where to write the expanded result.
    connectivity_distance_m : float
        Distance (meters) within which a candidate is considered network-
        connected to a passed canal. NHD line endpoints are usually exact;
        5 m is a safe slack against minor digitization gaps.
    relaxed_threshold : float
        Minimum residential coverage (0-1) for a network-connected
        candidate to be accepted. Default 0.25.
    buffer_m : float
        Buffer distance for the residential-coverage tabulation. Should
        match Tool 3 for consistency (default 50 m).
    iterations : int
        Maximum expansion rounds. Most networks settle in 2-3 rounds.
    keep_intermediate : bool
        Leave the buffer / tabulation intermediates in scratchGDB for
        debugging.
    """
    if not arcpy.Exists(full_canals_fc):
        raise FileNotFoundError(f"Full canals not found: {full_canals_fc}")
    if not arcpy.Exists(passed_canals_fc):
        raise FileNotFoundError(f"Passed canals not found: {passed_canals_fc}")
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels not found: {parcels_fc}")

    workspace = arcpy.env.scratchGDB
    full_oid_field = arcpy.Describe(full_canals_fc).OIDFieldName

    # ---- Step 1: pre-compute residential coverage for every canal ----
    arcpy.AddMessage(
        f"Pre-computing residential coverage for all canals "
        f"(buffer={buffer_m} m)..."
    )
    all_buf = os.path.join(workspace, "exp_all_buf")
    all_tab = os.path.join(workspace, "exp_all_tab")
    _delete_if_exists(all_buf)
    _delete_if_exists(all_tab)
    arcpy.analysis.Buffer(
        full_canals_fc, all_buf, f"{buffer_m} Meters",
        line_side="FULL", line_end_type="ROUND", dissolve_option="NONE",
    )
    zone_field = "ORIG_FID" if _has_field(all_buf, "ORIG_FID") else "OBJECTID"
    arcpy.analysis.TabulateIntersection(
        in_zone_features=all_buf, zone_fields=[zone_field],
        in_class_features=parcels_fc, out_table=all_tab,
    )
    tab_zone_field = (
        "ORIG_FID" if _has_field(all_tab, "ORIG_FID") else "OBJECTID_1"
    )
    pct_by_oid: dict[int, float] = {}
    with arcpy.da.SearchCursor(all_tab, [tab_zone_field, "PERCENTAGE"]) as cur:
        for oid, pct in cur:
            pct_by_oid[int(oid)] = float(pct or 0.0)
    arcpy.AddMessage(
        f"  coverage computed for {len(pct_by_oid):,} canals"
    )

    # ---- Step 2: initialize the result set with the primary OIDs ----
    # Identify which OIDs in full_canals_fc correspond to passed canals via
    # a spatial match. Tolerance is small to avoid grabbing parallel canals.
    full_lyr = "exp_full_lyr"
    if arcpy.Exists(full_lyr):
        arcpy.management.Delete(full_lyr)
    arcpy.management.MakeFeatureLayer(full_canals_fc, full_lyr)
    arcpy.management.SelectLayerByLocation(
        full_lyr, "INTERSECT", passed_canals_fc,
        search_distance="0.5 Meters",
    )
    primary_oids: set[int] = set()
    with arcpy.da.SearchCursor(full_lyr, [full_oid_field]) as cur:
        for row in cur:
            primary_oids.add(int(row[0]))
    arcpy.AddMessage(
        f"Primary set: {len(primary_oids):,} canals from passed_canals_fc"
    )
    if not primary_oids:
        raise RuntimeError(
            "No primary canals matched. Check that full_canals_fc and "
            "passed_canals_fc come from the same NHD source."
        )

    accepted: set[int] = set(primary_oids)
    threshold_pct = relaxed_threshold * 100.0

    # ---- Step 3: iterate network expansion ----
    for it in range(1, iterations + 1):
        # Make a layer of currently accepted canals.
        ids_csv = ",".join(str(x) for x in accepted)
        accepted_lyr = "exp_accepted_lyr"
        if arcpy.Exists(accepted_lyr):
            arcpy.management.Delete(accepted_lyr)
        arcpy.management.MakeFeatureLayer(
            full_canals_fc, accepted_lyr,
            where_clause=f"{full_oid_field} IN ({ids_csv})",
        )

        # Candidates: canals in full set within connectivity distance of
        # the accepted set, MINUS the already-accepted ones.
        if arcpy.Exists(full_lyr):
            arcpy.management.Delete(full_lyr)
        arcpy.management.MakeFeatureLayer(full_canals_fc, full_lyr)
        arcpy.management.SelectLayerByLocation(
            full_lyr, "WITHIN_A_DISTANCE", accepted_lyr,
            search_distance=f"{connectivity_distance_m} Meters",
        )
        # Apply attribute filter to drop already-accepted OIDs.
        arcpy.management.SelectLayerByAttribute(
            full_lyr, "REMOVE_FROM_SELECTION",
            where_clause=f"{full_oid_field} IN ({ids_csv})",
        )

        n_candidates = int(
            arcpy.management.GetCount(full_lyr).getOutput(0)
        )
        added: set[int] = set()
        if n_candidates:
            with arcpy.da.SearchCursor(full_lyr, [full_oid_field]) as cur:
                for row in cur:
                    oid = int(row[0])
                    if pct_by_oid.get(oid, 0.0) >= threshold_pct:
                        added.add(oid)

        arcpy.AddMessage(
            f"  iter {it}: {n_candidates:,} candidates, "
            f"{len(added):,} accepted"
        )
        if not added:
            break
        accepted.update(added)

    network_oids = accepted - primary_oids
    arcpy.AddMessage(
        f"Network expansion total: {len(accepted):,} canals "
        f"(primary {len(primary_oids):,} + network {len(network_oids):,})"
    )

    # ---- Step 4: write output with SOURCE attribute ----
    _delete_if_exists(out_fc)
    accepted_csv = ",".join(str(x) for x in accepted)
    final_lyr = "exp_final_lyr"
    if arcpy.Exists(final_lyr):
        arcpy.management.Delete(final_lyr)
    arcpy.management.MakeFeatureLayer(
        full_canals_fc, final_lyr,
        where_clause=f"{full_oid_field} IN ({accepted_csv})",
    )
    arcpy.management.CopyFeatures(final_lyr, out_fc)

    arcpy.management.AddField(out_fc, "SOURCE", "TEXT", field_length=12)
    out_oid_field = arcpy.Describe(out_fc).OIDFieldName
    # Store the original full-canal OID so we can label SOURCE. CopyFeatures
    # copies geometry plus shape attrs but generates new OIDs, so we re-match
    # by spatial INTERSECT against the original layers — slow for large
    # outputs but only happens once. For a faster approach, future work can
    # carry the OID via an attribute join before CopyFeatures.
    primary_set_lyr = "exp_primary_lyr"
    if arcpy.Exists(primary_set_lyr):
        arcpy.management.Delete(primary_set_lyr)
    primary_csv = ",".join(str(x) for x in primary_oids)
    arcpy.management.MakeFeatureLayer(
        full_canals_fc, primary_set_lyr,
        where_clause=f"{full_oid_field} IN ({primary_csv})",
    )
    out_lyr = "exp_out_lyr"
    if arcpy.Exists(out_lyr):
        arcpy.management.Delete(out_lyr)
    arcpy.management.MakeFeatureLayer(out_fc, out_lyr)
    arcpy.management.SelectLayerByLocation(
        out_lyr, "INTERSECT", primary_set_lyr,
        search_distance="0.5 Meters",
    )
    arcpy.management.CalculateField(out_lyr, "SOURCE", "'primary'")
    arcpy.management.SelectLayerByAttribute(out_lyr, "SWITCH_SELECTION")
    arcpy.management.CalculateField(out_lyr, "SOURCE", "'network'")
    arcpy.management.SelectLayerByAttribute(out_lyr, "CLEAR_SELECTION")

    n_out = int(arcpy.management.GetCount(out_fc).getOutput(0))
    arcpy.AddMessage(
        f"Expanded residential canals written: {out_fc} ({n_out:,} features)"
    )

    if not keep_intermediate:
        for tmp in [all_buf, all_tab]:
            _delete_if_exists(tmp)
    return out_fc



# ---------------------------------------------------------------------------
# Stage 5 (optional): Polygonize residential canals
# ---------------------------------------------------------------------------
def polygonize_residential_canals(
    canals_fc: str,
    parcels_fc: str,
    out_fc: str,
    *,
    nhdarea_fc: str | None = None,
    buffer_m: float = 25.0,
    min_polygon_area_m2: float = 30.0,
    line_end_type: str = "FLAT",
    keep_intermediate: bool = False,
) -> str:
    """
    Convert residential canal lines into water-surface polygons.

    Strategy
    --------
    1. Buffer each canal line by `buffer_m` (half-width). This defines the
       canal corridor — a swept polygon following the line.
    2. Refine the corridor to actual water surface:
         * If `nhdarea_fc` is provided, intersect the corridor with NHDArea
           water polygons. Output follows the real water shape clipped to
           the corridor (variable width, accurate edges).
         * Otherwise, erase residential parcels from the corridor. Water
           can't lie inside a residential parcel, so the residual is the
           water + roads in the corridor (close enough for canal-width
           communities where parcels run to water's edge).
    3. Drop polygons smaller than `min_polygon_area_m2` to suppress slivers.
    4. Compute area_m2 and area_km2 attributes on the output.

    Parameters
    ----------
    canals_fc : str
        Input canal lines (typically canals_residential_expanded from Tool 5,
        or canals_residential from Tool 3).
    parcels_fc : str
        Residential parcels feature class (used as fallback erase when
        nhdarea_fc isn't provided).
    out_fc : str
        Output polygon feature class.
    nhdarea_fc : str or None
        Optional NHDArea water polygons. When provided, used as the water
        mask within the canal corridor. Tool intersects buffer with NHDArea;
        any FCode is accepted because the spatial corridor constraint
        already filters out non-canal water.
    buffer_m : float
        Half-width buffer distance. Default 25 m matches typical Florida
        residential canal half-widths (mainline canals: 30-40 m wide; finger
        canals: 15-20 m). Tune per region as needed.
    min_polygon_area_m2 : float
        Drop polygons smaller than this. Default 30 m² removes slivers from
        the buffer/erase operations without dropping legitimate canal
        segments.
    keep_intermediate : bool
        If True, leave scratch buffer/intersect/erase outputs in place for
        debugging.

    Returns
    -------
    str
        Path to the output polygon feature class.
    """
    if not arcpy.Exists(canals_fc):
        raise FileNotFoundError(f"Canals not found: {canals_fc}")
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels not found: {parcels_fc}")
    if nhdarea_fc and not arcpy.Exists(nhdarea_fc):
        arcpy.AddWarning(
            f"NHDArea feature class not found ({nhdarea_fc}); "
            "falling back to parcel-erase strategy."
        )
        nhdarea_fc = None

    workspace = arcpy.env.scratchGDB
    buf_fc = os.path.join(workspace, "polyg_buffer")
    inter_fc = os.path.join(workspace, "polyg_intersect")
    erased_fc = os.path.join(workspace, "polyg_erased")
    single_fc = os.path.join(workspace, "polyg_single")
    for tmp in (buf_fc, inter_fc, erased_fc, single_fc):
        _delete_if_exists(tmp)
    _delete_if_exists(out_fc)

    # ---- Step 1: buffer canal lines ----
    arcpy.AddMessage(
        f"Buffering canal lines at {buffer_m} m (half-width)..."
    )
    arcpy.analysis.Buffer(
        in_features=canals_fc,
        out_feature_class=buf_fc,
        buffer_distance_or_field=f"{buffer_m} Meters",
        line_side="FULL",
        line_end_type=line_end_type,
        dissolve_option="NONE",
    )

    # ---- Step 2: refine corridor ----
    if nhdarea_fc:
        arcpy.AddMessage(
            f"Intersecting corridor with NHDArea water polygons "
            f"({nhdarea_fc})..."
        )
        arcpy.analysis.Intersect(
            in_features=[buf_fc, nhdarea_fc],
            out_feature_class=inter_fc,
            join_attributes="ALL",
        )
        refined_fc = inter_fc
    else:
        arcpy.AddMessage(
            "Erasing residential parcels from corridor "
            "(no NHDArea provided)..."
        )
        arcpy.analysis.Erase(
            in_features=buf_fc,
            erase_features=parcels_fc,
            out_feature_class=erased_fc,
        )
        refined_fc = erased_fc

    # ---- Step 3: multipart -> singlepart, drop slivers ----
    arcpy.management.MultipartToSinglepart(refined_fc, single_fc)
    arcpy.management.MakeFeatureLayer(
        single_fc, "polyg_filt_lyr",
        where_clause=f"Shape_Area >= {min_polygon_area_m2}",
    )
    arcpy.management.CopyFeatures("polyg_filt_lyr", out_fc)
    arcpy.management.Delete("polyg_filt_lyr")

    # ---- Step 4: area attributes + summary ----
    if not _has_field(out_fc, "area_m2"):
        arcpy.management.AddField(out_fc, "area_m2", "DOUBLE")
    if not _has_field(out_fc, "area_km2"):
        arcpy.management.AddField(out_fc, "area_km2", "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        out_fc,
        [["area_m2", "AREA"]],
        area_unit="SQUARE_METERS",
    )
    arcpy.management.CalculateField(
        out_fc, "area_km2", "!area_m2! / 1000000.0", "PYTHON3",
    )

    n_out = int(arcpy.management.GetCount(out_fc).getOutput(0))
    total_m2 = 0.0
    with arcpy.da.SearchCursor(out_fc, ["area_m2"]) as cur:
        for row in cur:
            total_m2 += float(row[0] or 0.0)

    arcpy.AddMessage(
        f"Polygons written: {n_out:,} -> {out_fc}"
    )
    arcpy.AddMessage(
        f"Total residential canal area: "
        f"{total_m2:,.0f} m^2 ({total_m2/1e6:.3f} km^2)"
    )

    if not keep_intermediate:
        for tmp in (buf_fc, inter_fc, erased_fc, single_fc):
            _delete_if_exists(tmp)

    return out_fc



# ---------------------------------------------------------------------------
# Stage 6 (optional): Extract residential canals from NHDArea polygons
# (handles communities where canals aren't in NHDFlowline)
# ---------------------------------------------------------------------------
# Tool 7 focuses exclusively on NHD canal/ditch polygons (FCode 33600
# family). The earlier Track-B approach (sea/ocean / bay-inlet / etc.)
# was a workaround for NHDArea's lumping behaviour; FLUCCS (Tool 10)
# now handles those cases more reliably, so we no longer try to extract
# canals from non-canal water FCodes.
DEFAULT_WATER_FCODES: list[int] = [
    33600, 33601, 33603,   # Canal/Ditch family
]



def _local_copy_county_boundary(
    counties_boundary_fc: str | None,
    out_gdb: str,
    local_name: str = "_local_county_boundary",
) -> str | None:
    """Copy a (possibly read-only or remote) counties boundary FC into the
    output gdb so per-county spatial selections don't inherit
    workspace-level read-only locks from the source. Returns the local
    path, or None if no boundary was supplied."""
    if not counties_boundary_fc:
        return None
    if not arcpy.Exists(counties_boundary_fc):
        return None
    local_path = os.path.join(out_gdb, local_name)
    _delete_if_exists(local_path)
    arcpy.AddMessage(
        f"Caching counties boundary locally: "
        f"{counties_boundary_fc} -> {local_path}"
    )
    arcpy.management.CopyFeatures(counties_boundary_fc, local_path)
    return local_path

def _t7_extract_one_aoi(
    water_fc: str,
    parcels_src: str,
    out_partial_fc: str,
    *,
    parcel_buffer_m: float,
    max_compactness: float,
    min_polygon_area_m2: float,
    max_width_m: float | None,
    tag: str,
    source_tag: str = "nhdarea_extracted",
    residential_coverage_threshold: float = 0.0,
    erase_parcels: bool = True,
) -> int:
    """Run buffer-intersect-erase-filter for a single parcel AOI. Returns
    the polygon count written to out_partial_fc.

    Uses the in-RAM "memory" workspace for intermediates. Always writable
    (avoids ERROR 160385 when scratchGDB lives in a read-only path) and
    faster than disk-backed scratch.
    """
    # Clear any stale lock on the memory workspace from a prior operation.
    # ERROR 160385 hit on the first county in a multi-county run before
    # this; clearing memory each iteration is the cheap fix.
    try:
        arcpy.management.Delete("memory")
    except Exception:
        pass
    workspace = "memory"
    # parcel_buf needs to live in a workspace untouched by the
    # projection and county-boundary cache writes earlier in the run,
    # because out_gdb develops a read-lock during the first iteration
    # after those writes. We create a dedicated _toolscratch.gdb beside
    # out_gdb and write parcel_buf there. The smaller intermediates stay
    # in memory for speed.
    out_gdb_parent = os.path.dirname(os.path.dirname(out_partial_fc))
    scratch_gdb = os.path.join(out_gdb_parent, "_toolscratch.gdb")
    if not arcpy.Exists(scratch_gdb):
        arcpy.management.CreateFileGDB(out_gdb_parent, "_toolscratch.gdb")
    parcel_buf = os.path.join(scratch_gdb, f"_pbuf_{tag}")
    intersected = f"{workspace}/t7_inter_{tag}"
    erased = f"{workspace}/t7_erased_{tag}"
    single = f"{workspace}/t7_single_{tag}"
    for tmp in (parcel_buf, intersected, erased, single):
        _delete_if_exists(tmp)
    _delete_if_exists(out_partial_fc)

    # Buffer parcels. PairwiseBuffer parallelizes; classic Buffer is the
    # fallback. We wrap in retry logic because Pro's first PairwiseBuffer
    # call in a session reproducibly fails with ERROR 160385 (workspace
    # read-only) — a cold-start issue independent of the actual target
    # workspace. The retry succeeds for the same reason later iterations
    # do: whatever lock state caused the failure clears after the failed
    # call returns.
    import time as _t  # local import; std lib
    _buf_attempt = 0
    _buf_done = False
    while not _buf_done and _buf_attempt < 3:
        _buf_attempt += 1
        try:
            arcpy.analysis.PairwiseBuffer(
                in_features=parcels_src,
                out_feature_class=parcel_buf,
                buffer_distance_or_field=f"{parcel_buffer_m} Meters",
                dissolve_option="ALL",
            )
            _buf_done = True
        except AttributeError:
            # Older Pro without PairwiseBuffer; one-shot classic Buffer.
            arcpy.analysis.Buffer(
                in_features=parcels_src,
                out_feature_class=parcel_buf,
                buffer_distance_or_field=f"{parcel_buffer_m} Meters",
                line_side="FULL",
                line_end_type="ROUND",
                dissolve_option="ALL",
            )
            _buf_done = True
        except arcpy.ExecuteError as _e:
            msg = str(_e)
            if "160385" in msg and _buf_attempt < 3:
                arcpy.AddWarning(
                    f"    PairwiseBuffer hit 160385 on attempt "
                    f"{_buf_attempt}; clearing locks and retrying..."
                )
                # Clear workspace cache and any leftover output
                try:
                    arcpy.management.ClearWorkspaceCache_management()
                except Exception:
                    pass
                _delete_if_exists(parcel_buf)
                _t.sleep(2)
                continue
            # Final attempt fell back to classic Buffer
            if _buf_attempt >= 3:
                arcpy.AddWarning(
                    f"    PairwiseBuffer failed after retries; "
                    "falling back to classic Buffer."
                )
                _delete_if_exists(parcel_buf)
                arcpy.analysis.Buffer(
                    in_features=parcels_src,
                    out_feature_class=parcel_buf,
                    buffer_distance_or_field=f"{parcel_buffer_m} Meters",
                    line_side="FULL",
                    line_end_type="ROUND",
                    dissolve_option="ALL",
                )
                _buf_done = True
            else:
                raise
    n_buf = int(arcpy.management.GetCount(parcel_buf).getOutput(0))
    arcpy.AddMessage(f"    buffer: {n_buf:,} feature(s)")

    if hasattr(arcpy.analysis, "PairwiseIntersect"):
        arcpy.analysis.PairwiseIntersect(
            in_features=[water_fc, parcel_buf],
            out_feature_class=intersected,
            join_attributes="ALL",
        )
    else:
        arcpy.analysis.Intersect(
            in_features=[water_fc, parcel_buf],
            out_feature_class=intersected,
            join_attributes="ALL",
        )
    n_inter = int(arcpy.management.GetCount(intersected).getOutput(0))
    arcpy.AddMessage(f"    intersect: {n_inter:,} feature(s)")
    if n_inter == 0:
        arcpy.AddWarning(
            "    Intersect returned 0 features. Common cause: water and "
            "parcels are in different CRSes. Check the main function's "
            "projection step ran."
        )

    if erase_parcels:
        if hasattr(arcpy.analysis, "PairwiseErase"):
            arcpy.analysis.PairwiseErase(intersected, parcels_src, erased)
        else:
            arcpy.analysis.Erase(intersected, parcels_src, erased)
        n_eras = int(arcpy.management.GetCount(erased).getOutput(0))
        arcpy.AddMessage(f"    erase: {n_eras:,} feature(s)")
    else:
        # Trusted canal source — parcel boundaries can legitimately
        # extend over canal water (boat slips, riparian rights). Don't
        # erase; that would clip out real canal area.
        arcpy.management.CopyFeatures(intersected, erased)
        n_eras = int(arcpy.management.GetCount(erased).getOutput(0))
        arcpy.AddMessage(
            f"    erase (skipped — trusted source): {n_eras:,} feature(s)"
        )

    arcpy.management.MultipartToSinglepart(erased, single)
    n_single = int(arcpy.management.GetCount(single).getOutput(0))
    arcpy.AddMessage(f"    singlepart: {n_single:,} feature(s)")

    if not _has_field(single, "area_m2"):
        arcpy.management.AddField(single, "area_m2", "DOUBLE")
    if not _has_field(single, "compactness"):
        arcpy.management.AddField(single, "compactness", "DOUBLE")
    # AREA_GEODESIC works on both projected and geographic CRS; plain
    # AREA fails when intermediate FC inherits a geographic SR (e.g., from
    # NHDArea).
    arcpy.management.CalculateGeometryAttributes(
        single, [["area_m2", "AREA_GEODESIC"]], area_unit="SQUARE_METERS",
    )
    arcpy.management.CalculateField(
        single, "compactness",
        "4 * 3.141592653589793 * !Shape_Area! / "
        "(!Shape_Length! * !Shape_Length!)",
        "PYTHON3",
    )

    filt_lyr = f"t7_filt_lyr_{tag}"
    if arcpy.Exists(filt_lyr):
        arcpy.management.Delete(filt_lyr)
    arcpy.management.MakeFeatureLayer(
        single, filt_lyr,
        where_clause=(
            f"area_m2 >= {min_polygon_area_m2} "
            f"AND compactness <= {max_compactness}"
        ),
    )
    n_filt = int(arcpy.management.GetCount(filt_lyr).getOutput(0))
    arcpy.AddMessage(
        f"    after area/compactness filter: {n_filt:,} feature(s)"
    )

    # Optional residential-coverage threshold. Buffer each candidate polygon
    # outward by parcel_buffer_m, tabulate intersection with parcels, and
    # keep only candidates whose surrounding buffer is dominated by
    # residential parcels. This mirrors Tool 3's methodology so all canal
    # sources share the same operational definition of "residential canal".
    if residential_coverage_threshold > 0 and n_filt > 0:
        # Use disk scratch (parent gdb of out_partial_fc) — TabulateIntersection
        # doesn't reliably write tables to memory. Use an explicit
        # T7_CAND_ID field for matching because OIDs aren't guaranteed to
        # align across CopyFeatures + Buffer.
        disk_ws = os.path.dirname(out_partial_fc)
        cand_fc = os.path.join(disk_ws, f"_t7cov_cand_{tag}")
        cand_buf = os.path.join(disk_ws, f"_t7cov_buf_{tag}")
        cand_tab = os.path.join(disk_ws, f"_t7cov_tab_{tag}")
        for tmp in (cand_fc, cand_buf, cand_tab):
            _delete_if_exists(tmp)

        arcpy.management.CopyFeatures(filt_lyr, cand_fc)
        arcpy.management.AddField(cand_fc, "T7_CAND_ID", "LONG")
        cand_oid = arcpy.Describe(cand_fc).OIDFieldName
        arcpy.management.CalculateField(
            cand_fc, "T7_CAND_ID", f"!{cand_oid}!", "PYTHON3",
        )

        try:
            arcpy.analysis.PairwiseBuffer(
                cand_fc, cand_buf,
                f"{parcel_buffer_m} Meters",
                dissolve_option="NONE",
            )
        except AttributeError:
            arcpy.analysis.Buffer(
                cand_fc, cand_buf,
                f"{parcel_buffer_m} Meters",
                line_side="FULL", line_end_type="ROUND",
                dissolve_option="NONE",
            )

        arcpy.analysis.TabulateIntersection(
            in_zone_features=cand_buf,
            zone_fields=["T7_CAND_ID"],
            in_class_features=parcels_src,
            out_table=cand_tab,
        )

        pct_by_id: dict[int, float] = {}
        with arcpy.da.SearchCursor(
            cand_tab, ["T7_CAND_ID", "PERCENTAGE"]
        ) as cur:
            for z, p in cur:
                if z is not None and p is not None:
                    pct_by_id[int(z)] = float(p)

        threshold_pct = residential_coverage_threshold * 100.0
        passing_ids: set[int] = set()
        with arcpy.da.SearchCursor(cand_fc, ["T7_CAND_ID"]) as cur:
            for row in cur:
                cid = int(row[0])
                if pct_by_id.get(cid, 0.0) >= threshold_pct:
                    passing_ids.add(cid)

        arcpy.management.Delete(filt_lyr)
        arcpy.management.MakeFeatureLayer(cand_fc, filt_lyr)
        if passing_ids:
            ids_csv = ",".join(str(x) for x in passing_ids)
            arcpy.management.SelectLayerByAttribute(
                filt_lyr, "NEW_SELECTION",
                where_clause=f"T7_CAND_ID IN ({ids_csv})",
            )
        else:
            arcpy.management.SelectLayerByAttribute(
                filt_lyr, "NEW_SELECTION",
                where_clause="1=0",
            )

        n_cov = int(arcpy.management.GetCount(filt_lyr).getOutput(0))
        arcpy.AddMessage(
            f"    after residential coverage filter "
            f"({threshold_pct:.0f}%): {n_cov:,} feature(s)"
        )
        # cand_fc stays on disk so filt_lyr remains valid until CopyFeatures;
        # the bottom-of-helper cleanup deletes it.
        for tmp in (cand_buf, cand_tab):
            _delete_if_exists(tmp)

    # Optional max-width filter. Canals are narrow; bay edges and causeway
    # shorelines can be elongated AND wide. Use the short dimension of the
    # minimum-area bounding rectangle as a proxy for polygon width.
    if max_width_m is not None and max_width_m > 0 and n_filt > 0:
        mbg = f"{workspace}/t7_mbg_{tag}"
        _delete_if_exists(mbg)
        arcpy.management.MinimumBoundingGeometry(
            filt_lyr, mbg,
            geometry_type="RECTANGLE_BY_WIDTH",
            group_option="NONE",
            mbg_fields_option="MBG_FIELDS",
        )
        # Build map from input OBJECTID -> MBG_Width via ORIG_FID
        width_by_oid: dict[int, float] = {}
        with arcpy.da.SearchCursor(mbg, ["ORIG_FID", "MBG_Width"]) as cur:
            for oid, w in cur:
                if oid is not None and w is not None:
                    width_by_oid[int(oid)] = float(w)
        narrow_oids = {
            oid for oid, w in width_by_oid.items() if w <= max_width_m
        }
        oid_field = arcpy.Describe(filt_lyr).OIDFieldName
        # Reduce the layer to the narrow set.
        if narrow_oids:
            ids_csv = ",".join(str(x) for x in narrow_oids)
            arcpy.management.SelectLayerByAttribute(
                filt_lyr, "NEW_SELECTION",
                where_clause=f"{oid_field} IN ({ids_csv})",
            )
        else:
            arcpy.management.SelectLayerByAttribute(
                filt_lyr, "NEW_SELECTION",
                where_clause="1=0",
            )
        n_narrow = int(arcpy.management.GetCount(filt_lyr).getOutput(0))
        arcpy.AddMessage(
            f"    after max-width filter ({max_width_m} m): "
            f"{n_narrow:,} feature(s)"
        )
        _delete_if_exists(mbg)

    arcpy.management.CopyFeatures(filt_lyr, out_partial_fc)
    arcpy.management.Delete(filt_lyr)

    n_out = int(arcpy.management.GetCount(out_partial_fc).getOutput(0))
    if n_out > 0:
        if not _has_field(out_partial_fc, "SOURCE"):
            arcpy.management.AddField(
                out_partial_fc, "SOURCE", "TEXT", field_length=32,
            )
        arcpy.management.CalculateField(
            out_partial_fc, "SOURCE", f"\'{source_tag}\'", "PYTHON3",
        )
    for tmp in (parcel_buf, intersected, erased, single):
        _delete_if_exists(tmp)
    # Clean up the coverage-filter scratch FC and the parcel-buf disk
    # leftover (both live on disk in out_gdb).
    cand_fc_path = os.path.join(
        os.path.dirname(out_partial_fc), f"_t7cov_cand_{tag}"
    )
    _delete_if_exists(cand_fc_path)
    return n_out


def extract_residential_canals_from_nhdarea(
    nhdarea_fc: str,
    parcels_fc: str,
    out_fc: str,
    *,
    counties: Iterable[str] | None = None,
    counties_boundary_fc: str | None = None,
    county_name_field: str = "NAME",
    parcel_buffer_m: float = 50.0,
    fcode_field: str = "FCode",
    water_fcodes: list[int] | None = None,
    max_compactness: float = 0.3,
    min_polygon_area_m2: float = 100.0,
    max_width_m: float | None = None,
    residential_coverage_threshold: float = 0.0,
    erase_existing_fc: str | None = None,
    keep_intermediate: bool = False,
) -> str:
    """
    Extract residential canal polygons from NHDArea water polygons in
    communities where NHDFlowline has no canal lines (e.g., Marco Island,
    Naples Bay neighborhoods, Florida Keys).

    Iterates per-county for safety: each county is processed independently
    and its result appended to the output. A failure in one county doesn't
    lose work from earlier counties.

    Parameters
    ----------
    counties : iterable of county names, or None
        Which counties to process. If None, the function loops over ALL 67
        Florida counties — this can still take a long time but is broken
        into resumable chunks. Provide a list like ["Lee", "Collier"] for
        a focused run.
    counties_boundary_fc : str or None
        Optional Florida counties polygon feature class. When provided,
        the per-county parcel selection uses SelectByLocation against the
        county boundary (HAVE_THEIR_CENTER_IN), bypassing the CO_NO field
        entirely. Use this when the local parcel layer's CO_NO is not a
        true county code.
    county_name_field : str
        Name of the text field on counties_boundary_fc that holds the
        county name (e.g., "NAME" for TIGER/Living Atlas). Default "NAME".
    parcel_buffer_m : float
        Distance around residential parcels treated as canal candidate
        zone. Default 50 m.
    fcode_field : str
        FCode field name on the NHDArea layer (default "FCode").
    water_fcodes : list[int] or None
        Which FCodes to treat as water. None uses DEFAULT_WATER_FCODES.
    max_compactness : float
        Polsby-Popper threshold; polygons above it are dropped as bay-like.
        Default 0.3.
    min_polygon_area_m2 : float
        Drop polygons smaller than this. Default 100 m^2.
    erase_existing_fc : str or None
        If provided, subtract these polygons (e.g., Tool 6 output) from
        the final merged result so you only see net-new additions.
    keep_intermediate : bool
        Leave scratch buffer/intersect/erase outputs in place for debugging.
    """
    if not arcpy.Exists(nhdarea_fc):
        raise FileNotFoundError(f"NHDArea not found: {nhdarea_fc}")
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels not found: {parcels_fc}")

    fcodes = water_fcodes or DEFAULT_WATER_FCODES
    fcode_csv = ",".join(str(f) for f in fcodes)

    # Per-county intermediates use the in-RAM "memory" workspace (fast,
    # always writable). The water layer itself lives in the output gdb
    # because arcpy.Project can't write to memory (ERROR 000944).
    workspace = "memory"
    out_gdb = os.path.dirname(out_fc)
    water_fc = os.path.join(out_gdb, "t7_water_proj")
    _delete_if_exists(water_fc)

    # ---- Filter NHDArea once, shared across counties ----
    arcpy.AddMessage(
        f"Filtering NHDArea to water FCodes ({len(fcodes)} codes)..."
    )
    arcpy.management.MakeFeatureLayer(
        nhdarea_fc, "t7_nhdarea_lyr",
        where_clause=f"{fcode_field} IN ({fcode_csv})",
    )

    # Project water to match the parcels CRS. Silent-empty intersects in
    # the per-county loop are usually CRS mismatches. Disk output here
    # works around arcpy.Project's memory-workspace restriction.
    parcels_sr = arcpy.Describe(parcels_fc).spatialReference
    water_sr = arcpy.Describe("t7_nhdarea_lyr").spatialReference
    if parcels_sr.factoryCode and water_sr.factoryCode and \
       parcels_sr.factoryCode != water_sr.factoryCode:
        arcpy.AddMessage(
            f"Projecting water polygons from "
            f"{water_sr.name} (EPSG:{water_sr.factoryCode}) "
            f"to {parcels_sr.name} (EPSG:{parcels_sr.factoryCode})..."
        )
        arcpy.management.Project(
            "t7_nhdarea_lyr", water_fc, parcels_sr,
        )
    else:
        arcpy.AddMessage(
            f"Water and parcels share CRS "
            f"({parcels_sr.name}); no projection needed"
        )
        arcpy.management.CopyFeatures("t7_nhdarea_lyr", water_fc)
    arcpy.management.Delete("t7_nhdarea_lyr")

    n_water = int(arcpy.management.GetCount(water_fc).getOutput(0))
    arcpy.AddMessage(f"  {n_water:,} water polygons in scope")

    # Tool 7 processes only NHD-classified canal polygons. The Track-B
    # approach (extracting canals from sea/ocean/bay polygons) has been
    # retired in favour of FLUCCS (Tool 10), which provides
    # authoritative canal classification for those cases.
    arcpy.AddMessage(
        f"  Canal-classified water polygons: {n_water:,}"
    )

    # Cache the counties boundary FC locally (it's frequently a read-only
    # ArcGIS Online layer or hosted service; using it directly leaks
    # workspace locks into per-county PairwiseBuffer calls).
    counties_boundary_fc = _local_copy_county_boundary(
        counties_boundary_fc, out_gdb, "_t7_county_boundary_local",
    )

    # ---- Build per-county iteration list ----
    county_list: list[tuple[str, int]]
    if counties:
        county_list = [(name, FLORIDA_COUNTIES[name]) for name in counties]
    else:
        arcpy.AddMessage(
            "No counties specified - processing all 67 Florida counties "
            "one at a time. This will take a while but is resumable."
        )
        county_list = sorted(FLORIDA_COUNTIES.items(), key=lambda kv: kv[0])

    # Prime the in-RAM memory workspace before the per-county loop.
    # Without this, the first PairwiseBuffer call (Collier Track A in our
    # ordering) sometimes hits ERROR 160385: Workspace or data source is
    # read only. A trivial create+delete forces memory into a writable
    # state so the first real write succeeds.
    try:
        prime_fc = "memory/_t7_prime"
        _delete_if_exists(prime_fc)
        arcpy.management.CreateFeatureclass(
            "memory", "_t7_prime", "POINT",
            spatial_reference=arcpy.SpatialReference(4326),
        )
        arcpy.management.Delete(prime_fc)
    except Exception as e:
        arcpy.AddMessage(f"  (memory prime skipped: {e})")

    # ---- Per-county processing ----
    partial_fcs: list[str] = []
    failed: list[str] = []
    for idx, (county_name, co_no) in enumerate(county_list, 1):
        # Tag identifies the iteration in scratch path names; preferred is
        # the county name (sanitized), with co_no as a fallback.
        safe_tag = (
            "".join(c if c.isalnum() else "_" for c in (county_name or ""))
            or f"co{co_no}"
        )

        parcels_lyr = f"t7_parcels_{safe_tag}"
        if arcpy.Exists(parcels_lyr):
            arcpy.management.Delete(parcels_lyr)

        if counties_boundary_fc and arcpy.Exists(counties_boundary_fc):
            # Spatial selection: pull the county boundary polygon, then
            # select parcels whose centroid is inside it. Robust against
            # broken/missing CO_NO codes in the local parcel layer.
            bdy_lyr = f"t7_bdy_{safe_tag}"
            if arcpy.Exists(bdy_lyr):
                arcpy.management.Delete(bdy_lyr)
            arcpy.management.MakeFeatureLayer(
                counties_boundary_fc, bdy_lyr,
                where_clause=(
                    f"{county_name_field} = '{county_name}'"
                ),
            )
            n_bdy = int(arcpy.management.GetCount(bdy_lyr).getOutput(0))
            if n_bdy == 0:
                arcpy.AddWarning(
                    f"[{idx}/{len(county_list)}] {county_name}: "
                    f"no boundary polygon matched "
                    f"{county_name_field}='{county_name}'; skip"
                )
                arcpy.management.Delete(bdy_lyr)
                continue
            arcpy.AddMessage(
                f"[{idx}/{len(county_list)}] {county_name} "
                f"(spatial select)"
            )
            arcpy.management.MakeFeatureLayer(parcels_fc, parcels_lyr)
            arcpy.management.SelectLayerByLocation(
                parcels_lyr, "HAVE_THEIR_CENTER_IN", bdy_lyr,
            )
            arcpy.management.Delete(bdy_lyr)
        else:
            # Legacy path: attribute filter on CO_NO. Works when the parcel
            # layer's CO_NO is a true county code (e.g., FDOR REST pull).
            arcpy.AddMessage(
                f"[{idx}/{len(county_list)}] {county_name} (CO_NO={co_no})"
            )
            arcpy.management.MakeFeatureLayer(
                parcels_fc, parcels_lyr,
                where_clause=f"CO_NO = {co_no}",
            )

        n_parcels = int(arcpy.management.GetCount(parcels_lyr).getOutput(0))
        if n_parcels == 0:
            arcpy.AddMessage(f"  (no residential parcels in county; skip)")
            arcpy.management.Delete(parcels_lyr)
            continue
        arcpy.AddMessage(f"  {n_parcels:,} residential parcels")

        # Per-track partials live on disk (out_gdb) so memory clears
        # between iterations don't wipe earlier counties' results.
        # Single-track: FCode 33600 NHDArea canal polygons.
        partial = os.path.join(out_gdb, f"t7_partial_{safe_tag}")
        n_county = 0
        try:
            n_county = _t7_extract_one_aoi(
                water_fc=water_fc,
                parcels_src=parcels_lyr,
                out_partial_fc=partial,
                parcel_buffer_m=parcel_buffer_m,
                # Trust NHD's canal classification — geometric filters
                # mainly serve to drop slivers; coverage threshold
                # enforces residential adjacency.
                max_compactness=1.0,
                min_polygon_area_m2=min_polygon_area_m2,
                max_width_m=None,
                tag=safe_tag,
                source_tag="nhdarea_classified_canal",
                residential_coverage_threshold=residential_coverage_threshold,
                # NHD FCode 33600 is already canal water; parcel
                # overlaps reflect riparian rights, not land. Skip
                # parcel erase to preserve canal extent.
                erase_parcels=False,
            )
            arcpy.AddMessage(f"  -> {n_county:,} polygons")
            if n_county > 0:
                partial_fcs.append(partial)
            else:
                _delete_if_exists(partial)
        except Exception as e:
            arcpy.AddWarning(
                f"  county {county_name} failed: {e}"
            )
            _delete_if_exists(partial)
            failed.append(county_name)

        if n_county == 0 and county_name not in failed:
            failed.append(county_name)
        arcpy.management.Delete(parcels_lyr)

    if not partial_fcs:
        raise RuntimeError(
            "No counties produced output. "
            f"Failed: {failed}"
        )

    # ---- Merge per-county partials ----
    _delete_if_exists(out_fc)
    if len(partial_fcs) == 1:
        arcpy.management.CopyFeatures(partial_fcs[0], out_fc)
    else:
        arcpy.management.Merge(partial_fcs, out_fc)
    arcpy.AddMessage(
        f"Merged {len(partial_fcs)} county partials into {out_fc}"
    )

    # ---- Optionally erase existing polygons (Tool 6 output) ----
    if erase_existing_fc and arcpy.Exists(erase_existing_fc):
        arcpy.AddMessage(f"Erasing existing polygons from {erase_existing_fc}...")
        erased = f"{workspace}/t7_final_erased"
        _delete_if_exists(erased)
        arcpy.analysis.Erase(out_fc, erase_existing_fc, erased)
        _delete_if_exists(out_fc)
        arcpy.management.CopyFeatures(erased, out_fc)
        if not keep_intermediate:
            _delete_if_exists(erased)

    # ---- Recompute area_km2 (SOURCE is already set per-track inside
    # the helper, with values "nhdarea_classified_canal" or
    # "nhdarea_extracted"). ----
    if not _has_field(out_fc, "area_km2"):
        arcpy.management.AddField(out_fc, "area_km2", "DOUBLE")
    arcpy.management.CalculateField(
        out_fc, "area_km2", "!area_m2! / 1000000.0", "PYTHON3",
    )

    # ---- Summary ----
    n_out_total = int(arcpy.management.GetCount(out_fc).getOutput(0))
    total_m2 = 0.0
    with arcpy.da.SearchCursor(out_fc, ["area_m2"]) as cur:
        for row in cur:
            total_m2 += float(row[0] or 0.0)
    arcpy.AddMessage(
        f"Polygons written: {n_out_total:,} -> {out_fc}"
    )
    arcpy.AddMessage(
        f"Total NHDArea-extracted residential canal area: "
        f"{total_m2:,.0f} m^2 ({total_m2/1e6:.3f} km^2)"
    )
    if failed:
        arcpy.AddWarning(
            f"Counties that failed and were skipped: {failed}"
        )

    # ---- Cleanup ----
    if not keep_intermediate:
        for p in partial_fcs:
            _delete_if_exists(p)
        _delete_if_exists(water_fc)

    return out_fc



# ---------------------------------------------------------------------------
# Stage 8 (optional): Tool 9 — extract canals from alternative NHDFlowline
# FCodes (55800 Artificial Path, 56600 Shoreline outlines).
# ---------------------------------------------------------------------------
def extract_canals_from_alt_flowlines(
    nhdflowline_fc: str,
    parcels_fc: str,
    out_fc: str,
    *,
    counties: Iterable[str] | None = None,
    counties_boundary_fc: str | None = None,
    county_name_field: str = "NAME",
    fcode_field: str = "fcode",
    line_buffer_m: float = 25.0,
    parcel_buffer_m: float = 50.0,
    max_compactness: float = 0.3,
    min_polygon_area_m2: float = 100.0,
    max_width_m: float | None = None,
    residential_coverage_threshold: float = 0.0,
    erase_existing_fc: str | None = None,
    keep_intermediate: bool = False,
) -> str:
    """
    Extract canal polygons from NHDFlowline features classified as anything
    other than FCode 33600 — specifically:

      Track A — FCode 55800 (Artificial Path): centerlines through complex
        water bodies. Treated like Tool 6 — buffer by `line_buffer_m`, then
        apply the residential-parcel-buffer + compactness/width pipeline.
      Track B — FCode 56600 (Shoreline): closed-loop outline polylines
        wrapping canal complexes (e.g., Marco Island). Converted to filled
        polygons via FeatureToPolygon, then the same downstream pipeline
        as Tool 7.

    Both tracks reuse Tool 7's per-AOI helper, so the buffer/intersect/
    erase/filter logic is shared.

    Parameters
    ----------
    nhdflowline_fc : str
        Source NHDFlowline feature class.
    parcels_fc : str
        Residential parcels.
    out_fc : str
        Output unified polygon feature class.
    counties : iterable of str or None
        Which counties to process. None = all 67.
    counties_boundary_fc : str or None
        Required. Florida counties polygon layer; spatial selection is the
        only reliable county filter for the local CADASTRAL_DOR data.
    county_name_field : str
        Field on counties_boundary_fc holding the county name. Default "NAME".
    fcode_field : str
        FCode field name on NHDFlowline. Default "fcode" (matches the
        local layer convention).
    line_buffer_m : float
        Half-width buffer for 55800 centerlines. Default 25 m.
    parcel_buffer_m : float
        Residential parcel buffer distance for the AOI constraint. Default
        50 m.
    max_compactness : float
        Polsby-Popper threshold for dropping blob-shaped polygons.
    min_polygon_area_m2 : float
        Minimum polygon area to keep.
    max_width_m : float or None
        Optional max-width filter (bounding-rectangle short dimension).
    erase_existing_fc : str or None
        Optional FC of polygons to erase from the final output (e.g.,
        Tool 6 or Tool 7 results) to avoid duplicates.
    keep_intermediate : bool
        Leave scratch outputs in place for debugging.
    """
    if not arcpy.Exists(nhdflowline_fc):
        raise FileNotFoundError(f"NHDFlowline not found: {nhdflowline_fc}")
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels not found: {parcels_fc}")

    out_gdb = os.path.dirname(out_fc)
    _delete_if_exists(out_fc)

    # ---- Project NHDFlowline to parcels CRS (if needed) ----
    parcels_sr = arcpy.Describe(parcels_fc).spatialReference
    flow_sr = arcpy.Describe(nhdflowline_fc).spatialReference
    flowlines_aligned = os.path.join(out_gdb, "_t9_flowlines_proj")
    _delete_if_exists(flowlines_aligned)
    if parcels_sr.factoryCode and flow_sr.factoryCode and \
       parcels_sr.factoryCode != flow_sr.factoryCode:
        arcpy.AddMessage(
            f"Projecting NHDFlowline from {flow_sr.name} "
            f"(EPSG:{flow_sr.factoryCode}) to {parcels_sr.name} "
            f"(EPSG:{parcels_sr.factoryCode})..."
        )
        arcpy.management.Project(nhdflowline_fc, flowlines_aligned, parcels_sr)
    else:
        arcpy.AddMessage(
            f"NHDFlowline already in parcels CRS ({parcels_sr.name})"
        )
        arcpy.management.CopyFeatures(nhdflowline_fc, flowlines_aligned)

    # ---- Build Track A water polygons: buffer FCode 55800 centerlines ----
    arcpy.AddMessage(f"Filtering NHDFlowline to {fcode_field} = 55800...")
    arcpy.management.MakeFeatureLayer(
        flowlines_aligned, "_t9_55800_lyr",
        where_clause=f"{fcode_field} = 55800",
    )
    n_55800 = int(arcpy.management.GetCount("_t9_55800_lyr").getOutput(0))
    arcpy.AddMessage(f"  {n_55800:,} Artificial Path lines in scope")
    water_55800_fc = os.path.join(out_gdb, "_t9_water_55800")
    _delete_if_exists(water_55800_fc)
    if n_55800 > 0:
        arcpy.AddMessage(
            f"Buffering Artificial Path lines by {line_buffer_m} m "
            f"(half-width) to make canal-corridor polygons..."
        )
        try:
            arcpy.analysis.PairwiseBuffer(
                "_t9_55800_lyr", water_55800_fc,
                f"{line_buffer_m} Meters",
                dissolve_option="NONE",
            )
        except AttributeError:
            arcpy.analysis.Buffer(
                "_t9_55800_lyr", water_55800_fc,
                f"{line_buffer_m} Meters",
                line_side="FULL", line_end_type="FLAT",
                dissolve_option="NONE",
            )
    arcpy.management.Delete("_t9_55800_lyr")

    # ---- Build Track B water polygons: FeatureToPolygon on 56600 ----
    arcpy.AddMessage(f"Filtering NHDFlowline to {fcode_field} = 56600...")
    arcpy.management.MakeFeatureLayer(
        flowlines_aligned, "_t9_56600_lyr",
        where_clause=f"{fcode_field} = 56600",
    )
    n_56600 = int(arcpy.management.GetCount("_t9_56600_lyr").getOutput(0))
    arcpy.AddMessage(f"  {n_56600:,} Shoreline lines in scope")
    water_56600_fc = os.path.join(out_gdb, "_t9_water_56600")
    _delete_if_exists(water_56600_fc)
    if n_56600 > 0:
        arcpy.AddMessage(
            "Converting closed-loop Shorelines to polygons "
            "(FeatureToPolygon)..."
        )
        arcpy.management.FeatureToPolygon(
            "_t9_56600_lyr", water_56600_fc,
        )
    arcpy.management.Delete("_t9_56600_lyr")
    _delete_if_exists(flowlines_aligned)

    n_track_a_water = (
        int(arcpy.management.GetCount(water_55800_fc).getOutput(0))
        if arcpy.Exists(water_55800_fc) else 0
    )
    n_track_b_water = (
        int(arcpy.management.GetCount(water_56600_fc).getOutput(0))
        if arcpy.Exists(water_56600_fc) else 0
    )
    arcpy.AddMessage(
        f"  Track A water polygons (from 55800 buffered): {n_track_a_water:,}"
    )
    arcpy.AddMessage(
        f"  Track B water polygons (from 56600 enclosed): {n_track_b_water:,}"
    )

    if n_track_a_water == 0 and n_track_b_water == 0:
        raise RuntimeError(
            "No alternative-flowline features found. Check fcode_field "
            f"value (currently {fcode_field!r}) and that the NHDFlowline "
            "layer has 55800/56600 features."
        )

    # Cache the counties boundary FC locally (avoids read-only locks
    # from hosted services).
    counties_boundary_fc = _local_copy_county_boundary(
        counties_boundary_fc, out_gdb, "_t9_county_boundary_local",
    )

    # ---- Build per-county iteration list ----
    if counties:
        county_list = [(name, FLORIDA_COUNTIES.get(name, 0))
                       for name in counties]
    else:
        county_list = sorted(FLORIDA_COUNTIES.items(), key=lambda kv: kv[0])

    # ---- Prime memory workspace ----
    try:
        prime_fc = "memory/_t9_prime"
        _delete_if_exists(prime_fc)
        arcpy.management.CreateFeatureclass(
            "memory", "_t9_prime", "POINT",
            spatial_reference=arcpy.SpatialReference(4326),
        )
        arcpy.management.Delete(prime_fc)
    except Exception:
        pass

    # ---- Per-county processing ----
    partial_fcs: list[str] = []
    failed: list[str] = []
    for idx, (county_name, _) in enumerate(county_list, 1):
        safe_tag = (
            "".join(c if c.isalnum() else "_" for c in (county_name or ""))
            or f"co{idx}"
        )
        parcels_lyr = f"_t9_parcels_{safe_tag}"
        if arcpy.Exists(parcels_lyr):
            arcpy.management.Delete(parcels_lyr)

        if counties_boundary_fc and arcpy.Exists(counties_boundary_fc):
            bdy_lyr = f"_t9_bdy_{safe_tag}"
            if arcpy.Exists(bdy_lyr):
                arcpy.management.Delete(bdy_lyr)
            arcpy.management.MakeFeatureLayer(
                counties_boundary_fc, bdy_lyr,
                where_clause=f"{county_name_field} = '{county_name}'",
            )
            if int(arcpy.management.GetCount(bdy_lyr).getOutput(0)) == 0:
                arcpy.AddWarning(
                    f"[{idx}/{len(county_list)}] {county_name}: no "
                    "boundary polygon; skip"
                )
                arcpy.management.Delete(bdy_lyr)
                continue
            arcpy.AddMessage(
                f"[{idx}/{len(county_list)}] {county_name} (spatial select)"
            )
            arcpy.management.MakeFeatureLayer(parcels_fc, parcels_lyr)
            arcpy.management.SelectLayerByLocation(
                parcels_lyr, "HAVE_THEIR_CENTER_IN", bdy_lyr,
            )
            arcpy.management.Delete(bdy_lyr)
        else:
            arcpy.AddWarning(
                f"[{idx}/{len(county_list)}] {county_name}: no boundary "
                "layer provided; skip"
            )
            continue

        n_parcels = int(arcpy.management.GetCount(parcels_lyr).getOutput(0))
        if n_parcels == 0:
            arcpy.AddMessage("  (no parcels in county; skip)")
            arcpy.management.Delete(parcels_lyr)
            continue
        arcpy.AddMessage(f"  {n_parcels:,} residential parcels")

        # Track A: 55800 buffered centerlines
        if n_track_a_water > 0:
            partial_a = os.path.join(
                out_gdb, f"_t9_partial_a_{safe_tag}"
            )
            try:
                arcpy.AddMessage("  Track A (55800 Artificial Path):")
                n_a = _t7_extract_one_aoi(
                    water_fc=water_55800_fc,
                    parcels_src=parcels_lyr,
                    out_partial_fc=partial_a,
                    parcel_buffer_m=parcel_buffer_m,
                    max_compactness=max_compactness,
                    min_polygon_area_m2=min_polygon_area_m2,
                    max_width_m=max_width_m,
                    tag=f"55800_{safe_tag}",
                    source_tag="flowline_artificial_path",
                    residential_coverage_threshold=residential_coverage_threshold,
                )
                arcpy.AddMessage(f"  -> Track A: {n_a:,} polygons")
                if n_a > 0:
                    partial_fcs.append(partial_a)
                else:
                    _delete_if_exists(partial_a)
            except Exception as e:
                arcpy.AddWarning(
                    f"  {county_name} Track A failed: {e}"
                )
                _delete_if_exists(partial_a)

        # Track B: 56600 shoreline polygons (already filled)
        if n_track_b_water > 0:
            partial_b = os.path.join(
                out_gdb, f"_t9_partial_b_{safe_tag}"
            )
            try:
                arcpy.AddMessage("  Track B (56600 Shoreline outline):")
                n_b = _t7_extract_one_aoi(
                    water_fc=water_56600_fc,
                    parcels_src=parcels_lyr,
                    out_partial_fc=partial_b,
                    parcel_buffer_m=parcel_buffer_m,
                    max_compactness=max_compactness,
                    min_polygon_area_m2=min_polygon_area_m2,
                    max_width_m=max_width_m,
                    tag=f"56600_{safe_tag}",
                    source_tag="flowline_shoreline_polygon",
                    residential_coverage_threshold=residential_coverage_threshold,
                )
                arcpy.AddMessage(f"  -> Track B: {n_b:,} polygons")
                if n_b > 0:
                    partial_fcs.append(partial_b)
                else:
                    _delete_if_exists(partial_b)
            except Exception as e:
                arcpy.AddWarning(
                    f"  {county_name} Track B failed: {e}"
                )
                _delete_if_exists(partial_b)

        arcpy.management.Delete(parcels_lyr)

    if not partial_fcs:
        raise RuntimeError(f"Tool 9 produced no output. Failed: {failed}")

    # ---- Merge partials ----
    if len(partial_fcs) == 1:
        arcpy.management.CopyFeatures(partial_fcs[0], out_fc)
    else:
        arcpy.management.Merge(partial_fcs, out_fc)
    arcpy.AddMessage(f"Merged {len(partial_fcs)} partials into {out_fc}")

    # ---- Optional erase existing ----
    if erase_existing_fc and arcpy.Exists(erase_existing_fc):
        erased = os.path.join(out_gdb, "_t9_final_erased")
        _delete_if_exists(erased)
        arcpy.analysis.Erase(out_fc, erase_existing_fc, erased)
        _delete_if_exists(out_fc)
        arcpy.management.CopyFeatures(erased, out_fc)
        _delete_if_exists(erased)

    # ---- Compute area_km2 ----
    if not _has_field(out_fc, "area_km2"):
        arcpy.management.AddField(out_fc, "area_km2", "DOUBLE")
    arcpy.management.CalculateField(
        out_fc, "area_km2", "!area_m2! / 1000000.0", "PYTHON3",
    )

    # ---- Summary ----
    n_total = int(arcpy.management.GetCount(out_fc).getOutput(0))
    total_m2 = 0.0
    with arcpy.da.SearchCursor(out_fc, ["area_m2"]) as cur:
        for row in cur:
            total_m2 += float(row[0] or 0.0)
    arcpy.AddMessage(
        f"Tool 9 output: {n_total:,} polygons | "
        f"{total_m2:,.0f} m^2 ({total_m2/1e6:.3f} km^2)"
    )

    # ---- Cleanup ----
    if not keep_intermediate:
        for p in partial_fcs:
            _delete_if_exists(p)
        _delete_if_exists(water_55800_fc)
        _delete_if_exists(water_56600_fc)

    return out_fc



# ---------------------------------------------------------------------------
# Stage 9 (optional): Tool 10 — filter generic polygon canal sources
# (e.g., FLUCCS) by residential adjacency.
# ---------------------------------------------------------------------------
def filter_polygon_canals_by_residential(
    canal_polygons_fc: str,
    parcels_fc: str,
    out_fc: str,
    *,
    counties: Iterable[str] | None = None,
    counties_boundary_fc: str | None = None,
    county_name_field: str = "NAME",
    parcel_buffer_m: float = 50.0,
    max_compactness: float = 1.0,
    min_polygon_area_m2: float = 100.0,
    max_width_m: float | None = None,
    residential_coverage_threshold: float = 0.0,
    source_tag: str = "fluccs_residential",
    erase_existing_fc: str | None = None,
    keep_intermediate: bool = False,
) -> str:
    """
    Filter a pre-classified polygon canal dataset (FLUCCS canal classes,
    or any other authoritative canal polygon source) to canals adjacent
    to residential parcels.

    Reuses Tool 7's per-AOI helper — same buffer / intersect / erase /
    sliver-filter pipeline — but starts from a polygon canal layer the
    caller already trusts as canal-classified, so the geometric proof
    filters default to disabled (max_compactness=1.0, max_width_m=None).

    Parameters
    ----------
    canal_polygons_fc : str
        Pre-classified canal polygon source. E.g., FLUCCS classes filtered
        to "Canals and Locks" + "Channelized Waterways - Canals".
    parcels_fc : str
        Residential parcels.
    out_fc : str
        Output filtered canal polygons.
    counties : iterable of str or None
        Counties to process; None = all 67 from FLORIDA_COUNTIES.
    counties_boundary_fc : str or None
        Florida counties boundary (required for spatial county selection).
    county_name_field : str
        County name field on the boundary layer. Default "NAME".
    parcel_buffer_m : float
        Residential parcel buffer distance. Default 50 m.
    max_compactness : float
        Default 1.0 (effectively disabled — trust the canal classification).
    min_polygon_area_m2 : float
        Drop slivers below this. Default 100 m^2.
    max_width_m : float or None
        Optional max width filter. Default None (disabled).
    source_tag : str
        Value written to the SOURCE field on each output polygon.
        Default "fluccs_residential".
    erase_existing_fc : str or None
        Optional FC to erase from output (e.g., Tool 6/7/9 results) to
        avoid duplicates.
    keep_intermediate : bool
        Leave scratch outputs in place for debugging.
    """
    if not arcpy.Exists(canal_polygons_fc):
        raise FileNotFoundError(
            f"Canal polygons not found: {canal_polygons_fc}"
        )
    if not arcpy.Exists(parcels_fc):
        raise FileNotFoundError(f"Parcels not found: {parcels_fc}")

    out_gdb = os.path.dirname(out_fc)
    _delete_if_exists(out_fc)

    # ---- Project canal polygons to parcels CRS if needed ----
    parcels_sr = arcpy.Describe(parcels_fc).spatialReference
    canals_sr = arcpy.Describe(canal_polygons_fc).spatialReference
    canals_aligned = os.path.join(out_gdb, "_t10_canals_proj")
    _delete_if_exists(canals_aligned)
    if parcels_sr.factoryCode and canals_sr.factoryCode and \
       parcels_sr.factoryCode != canals_sr.factoryCode:
        arcpy.AddMessage(
            f"Projecting canal polygons from "
            f"{canals_sr.name} (EPSG:{canals_sr.factoryCode}) to "
            f"{parcels_sr.name} (EPSG:{parcels_sr.factoryCode})..."
        )
        arcpy.management.Project(
            canal_polygons_fc, canals_aligned, parcels_sr
        )
    else:
        arcpy.AddMessage(
            f"Canal polygons already in parcels CRS "
            f"({parcels_sr.name})"
        )
        arcpy.management.CopyFeatures(canal_polygons_fc, canals_aligned)

    n_canals = int(
        arcpy.management.GetCount(canals_aligned).getOutput(0)
    )
    arcpy.AddMessage(f"  {n_canals:,} canal polygons in scope")
    if n_canals == 0:
        raise RuntimeError("No canal polygons in the input feature class.")

    # Cache the counties boundary FC locally (avoids read-only locks
    # from hosted services).
    counties_boundary_fc = _local_copy_county_boundary(
        counties_boundary_fc, out_gdb, "_t10_county_boundary_local",
    )

    # ---- Build per-county iteration list ----
    if counties:
        county_list = [(name, FLORIDA_COUNTIES.get(name, 0))
                       for name in counties]
    else:
        county_list = sorted(FLORIDA_COUNTIES.items(), key=lambda kv: kv[0])

    # ---- Prime memory workspace ----
    try:
        prime_fc = "memory/_t10_prime"
        _delete_if_exists(prime_fc)
        arcpy.management.CreateFeatureclass(
            "memory", "_t10_prime", "POINT",
            spatial_reference=arcpy.SpatialReference(4326),
        )
        arcpy.management.Delete(prime_fc)
    except Exception:
        pass

    # ---- Per-county processing ----
    partial_fcs: list[str] = []
    for idx, (county_name, _) in enumerate(county_list, 1):
        safe_tag = (
            "".join(c if c.isalnum() else "_" for c in (county_name or ""))
            or f"co{idx}"
        )
        parcels_lyr = f"_t10_parcels_{safe_tag}"
        if arcpy.Exists(parcels_lyr):
            arcpy.management.Delete(parcels_lyr)

        if not counties_boundary_fc or not arcpy.Exists(counties_boundary_fc):
            arcpy.AddWarning(
                f"[{idx}/{len(county_list)}] {county_name}: no boundary "
                "layer provided; skip"
            )
            continue

        bdy_lyr = f"_t10_bdy_{safe_tag}"
        if arcpy.Exists(bdy_lyr):
            arcpy.management.Delete(bdy_lyr)
        arcpy.management.MakeFeatureLayer(
            counties_boundary_fc, bdy_lyr,
            where_clause=f"{county_name_field} = '{county_name}'",
        )
        if int(arcpy.management.GetCount(bdy_lyr).getOutput(0)) == 0:
            arcpy.AddWarning(
                f"[{idx}/{len(county_list)}] {county_name}: no boundary "
                "polygon; skip"
            )
            arcpy.management.Delete(bdy_lyr)
            continue
        arcpy.AddMessage(
            f"[{idx}/{len(county_list)}] {county_name} (spatial select)"
        )
        arcpy.management.MakeFeatureLayer(parcels_fc, parcels_lyr)
        arcpy.management.SelectLayerByLocation(
            parcels_lyr, "HAVE_THEIR_CENTER_IN", bdy_lyr,
        )
        arcpy.management.Delete(bdy_lyr)

        n_parcels = int(
            arcpy.management.GetCount(parcels_lyr).getOutput(0)
        )
        if n_parcels == 0:
            arcpy.AddMessage("  (no parcels in county; skip)")
            arcpy.management.Delete(parcels_lyr)
            continue
        arcpy.AddMessage(f"  {n_parcels:,} residential parcels")

        partial = os.path.join(out_gdb, f"_t10_partial_{safe_tag}")
        try:
            n_out = _t7_extract_one_aoi(
                water_fc=canals_aligned,
                parcels_src=parcels_lyr,
                out_partial_fc=partial,
                parcel_buffer_m=parcel_buffer_m,
                max_compactness=max_compactness,
                min_polygon_area_m2=min_polygon_area_m2,
                max_width_m=max_width_m,
                tag=safe_tag,
                source_tag=source_tag,
                residential_coverage_threshold=residential_coverage_threshold,
                # FLUCCS (or any explicitly canal-classified source) is
                # already canal water; parcel-over-water cases (boat
                # slips, riparian rights) should not be erased.
                erase_parcels=False,
            )
            arcpy.AddMessage(f"  -> {n_out:,} polygons")
            if n_out > 0:
                partial_fcs.append(partial)
            else:
                _delete_if_exists(partial)
        except Exception as e:
            arcpy.AddWarning(f"  {county_name} failed: {e}")
            _delete_if_exists(partial)
        finally:
            arcpy.management.Delete(parcels_lyr)

    if not partial_fcs:
        raise RuntimeError("Tool 10 produced no output.")

    # ---- Merge partials ----
    if len(partial_fcs) == 1:
        arcpy.management.CopyFeatures(partial_fcs[0], out_fc)
    else:
        arcpy.management.Merge(partial_fcs, out_fc)
    arcpy.AddMessage(f"Merged {len(partial_fcs)} partials into {out_fc}")

    # ---- Optional erase existing ----
    if erase_existing_fc and arcpy.Exists(erase_existing_fc):
        erased = os.path.join(out_gdb, "_t10_final_erased")
        _delete_if_exists(erased)
        arcpy.analysis.Erase(out_fc, erase_existing_fc, erased)
        _delete_if_exists(out_fc)
        arcpy.management.CopyFeatures(erased, out_fc)
        _delete_if_exists(erased)

    # ---- area_km2 ----
    if not _has_field(out_fc, "area_km2"):
        arcpy.management.AddField(out_fc, "area_km2", "DOUBLE")
    arcpy.management.CalculateField(
        out_fc, "area_km2", "!area_m2! / 1000000.0", "PYTHON3",
    )

    # ---- Summary ----
    n_total = int(arcpy.management.GetCount(out_fc).getOutput(0))
    total_m2 = 0.0
    with arcpy.da.SearchCursor(out_fc, ["area_m2"]) as cur:
        for row in cur:
            total_m2 += float(row[0] or 0.0)
    arcpy.AddMessage(
        f"Tool 10 output: {n_total:,} polygons | "
        f"{total_m2:,.0f} m^2 ({total_m2/1e6:.3f} km^2)"
    )

    # ---- Cleanup ----
    if not keep_intermediate:
        for p in partial_fcs:
            _delete_if_exists(p)
        _delete_if_exists(canals_aligned)

    return out_fc


def run_full_pipeline(
    gdb: str,
    counties: Iterable[str] | None = None,
    buffer_m: float = 50.0,
    threshold: float = 0.5,
    refresh: bool = False,
    local_parcels_path: str = None  # Add this as an optional parameter
) -> dict:
    ensure_gdb(gdb)
    
    # Stage 1: Always fetch NHD canals
    canals_fc = fetch_nhd_canals(gdb, refresh=refresh)
    
    # Stage 2: Logic for Local vs. REST
    if local_parcels_path and arcpy.Exists(local_parcels_path):
        arcpy.AddMessage(f"Using local parcel data: {local_parcels_path}")
        parcels_fc = local_parcels_path
    else:
        # Fallback to the REST service if no local path provided
        parcels_fc = fetch_res

# ---------------------------------------------------------------------------
# Stage 7: Union Tool 6 + Tool 7 polygons into a unified deliverable
# ---------------------------------------------------------------------------
def union_residential_canal_polygons(
    input_fcs: Iterable | None = None,
    out_fc: str = "",
    *,
    tool6_fc: str | None = None,
    tool7_fc: str | None = None,
    priority_fc: str | None = None,
    dissolve_overlaps: bool = False,
    keep_intermediate: bool = False,
) -> str:
    """
    Combine multiple residential canal polygon feature classes into a
    single unified layer.

    Each output polygon carries a SOURCE_TOOL attribute identifying its
    origin (derived from the input feature class basename, or from any
    pre-existing SOURCE_TOOL field).

    Parameters
    ----------
    input_fcs : iterable of str or tuple, or None
        Either:
          * a list of feature class paths (SOURCE_TOOL inferred from basename)
          * a list of (fc_path, source_tag) tuples for explicit tagging
        Pass None and use tool6_fc / tool7_fc for backwards compatibility.
    out_fc : str
        Output unified feature class.
    tool6_fc, tool7_fc : str or None
        Backwards-compatible shortcuts; if input_fcs is None, these two
        paths are used.
    dissolve_overlaps : bool
        If True, dissolve overlapping polygons so each canal area is one
        polygon. SOURCE_TOOL is set to "multiple" where multiple tools
        contributed to the same area. Default False (preserves polygon
        identity for QC).
    keep_intermediate : bool
        Leave the tagged + merged scratch outputs in place for debugging.

    Returns
    -------
    str
        Path to the output feature class.
    """
    # Normalize input list to [(path, tag), ...]
    normalized: list[tuple[str, str]] = []
    if input_fcs is None:
        # Backwards-compat path
        if tool6_fc:
            normalized.append((tool6_fc, "tool_6_line_derived"))
        if tool7_fc:
            normalized.append((tool7_fc, "tool_7_nhdarea"))
    else:
        for entry in input_fcs:
            if isinstance(entry, (list, tuple)):
                path, tag = entry[0], entry[1]
            else:
                path = str(entry)
                # Derive tag from basename, sanitized.
                base = os.path.basename(path)
                tag = (
                    "".join(c if c.isalnum() or c == "_" else "_"
                            for c in base)[:32]
                )
                tag = f"from_{tag}"
            normalized.append((path, tag))

    if not normalized:
        raise ValueError(
            "At least one input feature class must be provided "
            "(via input_fcs or tool6_fc / tool7_fc)."
        )
    for path, _ in normalized:
        if not arcpy.Exists(path):
            raise FileNotFoundError(f"Input fc not found: {path}")

    out_gdb = os.path.dirname(out_fc)
    _delete_if_exists(out_fc)

    # ---- Stage 1: tag each input with SOURCE_TOOL ----
    # When priority_fc is provided, that source's polygons stay
    # unmodified; every other input is Erased by priority_fc first so
    # the priority source's boundaries always win and other sources
    # only contribute where they have no overlap with priority.
    if priority_fc and not arcpy.Exists(priority_fc):
        raise FileNotFoundError(
            f"Priority feature class not found: {priority_fc}"
        )
    if priority_fc:
        arcpy.AddMessage(
            f"Priority source: {priority_fc}. Other inputs will be "
            "erased by it before merge."
        )

    tagged_fcs: list[str] = []
    for src_fc, tag in normalized:
        tagged = os.path.join(out_gdb, f"_t8_tagged_{tag[:24]}")
        _delete_if_exists(tagged)
        is_priority = (
            priority_fc is not None
            and os.path.normpath(src_fc) == os.path.normpath(priority_fc)
        )
        if priority_fc and not is_priority:
            erased = os.path.join(out_gdb, f"_t8_erased_{tag[:24]}")
            _delete_if_exists(erased)
            try:
                arcpy.analysis.PairwiseErase(src_fc, priority_fc, erased)
            except AttributeError:
                arcpy.analysis.Erase(src_fc, priority_fc, erased)
            arcpy.management.CopyFeatures(erased, tagged)
            _delete_if_exists(erased)
            n_in = int(arcpy.management.GetCount(src_fc).getOutput(0))
            n_after = int(arcpy.management.GetCount(tagged).getOutput(0))
            arcpy.AddMessage(
                f"  {tag}: {n_in:,} input -> {n_after:,} after erase "
                "by priority"
            )
        else:
            arcpy.management.CopyFeatures(src_fc, tagged)
            n_after = int(arcpy.management.GetCount(tagged).getOutput(0))
            label = "(priority)" if is_priority else ""
            arcpy.AddMessage(
                f"  {tag}: {n_after:,} polygons tagged {label}"
            )

        if not _has_field(tagged, "SOURCE_TOOL"):
            arcpy.management.AddField(
                tagged, "SOURCE_TOOL", "TEXT", field_length=48,
            )
        arcpy.management.CalculateField(
            tagged, "SOURCE_TOOL", f"'{tag}'", "PYTHON3",
        )
        tagged_fcs.append(tagged)

    # ---- Stage 2: merge ----
    arcpy.AddMessage(f"Merging {len(tagged_fcs)} input(s)...")
    if len(tagged_fcs) == 1:
        merged = tagged_fcs[0]
    else:
        merged = os.path.join(out_gdb, "_t8_merged")
        _delete_if_exists(merged)
        arcpy.management.Merge(tagged_fcs, merged)

    # ---- Stage 3: optionally dissolve overlaps ----
    if dissolve_overlaps:
        arcpy.AddMessage(
            "Dissolving overlapping polygons; SOURCE_TOOL set to "
            "'multiple' where more than one input contributed..."
        )
        dissolved = os.path.join(out_gdb, "_t8_dissolved")
        _delete_if_exists(dissolved)
        if hasattr(arcpy.management, "PairwiseDissolve"):
            arcpy.management.PairwiseDissolve(
                merged, dissolved,
                dissolve_field=[],
                multi_part="SINGLE_PART",
            )
        else:
            arcpy.management.Dissolve(
                merged, dissolved,
                dissolve_field=[],
                multi_part="SINGLE_PART",
            )
        arcpy.management.AddField(
            dissolved, "SOURCE_TOOL", "TEXT", field_length=48,
        )
        oid_field = arcpy.Describe(dissolved).OIDFieldName
        # Determine, per dissolved polygon, which inputs contributed.
        contribs: dict[int, set[str]] = {}
        for tagged_fc, (_, tag) in zip(tagged_fcs, normalized):
            lyr = "_t8_diss_lyr"
            if arcpy.Exists(lyr):
                arcpy.management.Delete(lyr)
            arcpy.management.MakeFeatureLayer(dissolved, lyr)
            arcpy.management.SelectLayerByLocation(
                lyr, "INTERSECT", tagged_fc,
                search_distance="0.1 Meters",
            )
            with arcpy.da.SearchCursor(lyr, [oid_field]) as cur:
                for row in cur:
                    contribs.setdefault(int(row[0]), set()).add(tag)
            arcpy.management.Delete(lyr)
        with arcpy.da.UpdateCursor(
            dissolved, [oid_field, "SOURCE_TOOL"]
        ) as cur:
            for row in cur:
                tags = contribs.get(int(row[0]), set())
                if len(tags) == 0:
                    row[1] = "unknown"
                elif len(tags) == 1:
                    row[1] = next(iter(tags))
                else:
                    row[1] = "multiple"
                cur.updateRow(row)
        arcpy.management.CopyFeatures(dissolved, out_fc)
        if not keep_intermediate:
            _delete_if_exists(dissolved)
    else:
        arcpy.management.CopyFeatures(merged, out_fc)

    # ---- Stage 4: ensure area attributes ----
    if not _has_field(out_fc, "area_m2"):
        arcpy.management.AddField(out_fc, "area_m2", "DOUBLE")
    if not _has_field(out_fc, "area_km2"):
        arcpy.management.AddField(out_fc, "area_km2", "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        out_fc, [["area_m2", "AREA_GEODESIC"]],
        area_unit="SQUARE_METERS",
    )
    arcpy.management.CalculateField(
        out_fc, "area_km2", "!area_m2! / 1000000.0", "PYTHON3",
    )

    # ---- Stage 5: summary ----
    n_total = int(arcpy.management.GetCount(out_fc).getOutput(0))
    by_source: dict[str, tuple[int, float]] = {}
    total_m2 = 0.0
    with arcpy.da.SearchCursor(out_fc, ["SOURCE_TOOL", "area_m2"]) as cur:
        for src_t, a in cur:
            a = float(a or 0.0)
            total_m2 += a
            cnt, ar = by_source.get(src_t, (0, 0.0))
            by_source[src_t] = (cnt + 1, ar + a)

    arcpy.AddMessage("")
    arcpy.AddMessage("=== Unified residential canal polygons ===")
    arcpy.AddMessage(f"Output: {out_fc}")
    arcpy.AddMessage(f"Total polygons: {n_total:,}")
    arcpy.AddMessage(
        f"Total area: {total_m2:,.0f} m^2 ({total_m2/1e6:.3f} km^2)"
    )
    arcpy.AddMessage("Breakdown by SOURCE_TOOL:")
    for src_t in sorted(by_source.keys()):
        cnt, ar = by_source[src_t]
        arcpy.AddMessage(
            f"  {src_t}: {cnt:,} polygons | {ar/1e6:.3f} km^2"
        )

    # Cleanup
    if not keep_intermediate:
        for tagged in tagged_fcs:
            if tagged != out_fc:
                _delete_if_exists(tagged)
        merged_path = os.path.join(out_gdb, "_t8_merged")
        if merged_path != out_fc:
            _delete_if_exists(merged_path)

    return out_fc


