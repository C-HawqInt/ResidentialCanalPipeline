# -*- coding: utf-8 -*-
"""
CanalFootprintTools.pyt
=======================
ArcGIS Pro Python Toolbox for the Florida residential canal footprint
pipeline. Each tool is a thin wrapper around functions in `pipeline_core.py`.

Usage in Pro
------------
1. In the Catalog pane, right-click "Toolboxes" -> "Add Toolbox".
2. Browse to this `.pyt` file. It will appear as "Canal Footprint Tools".
3. Expand to see the ten tools. Run them individually, or use
   "4. Run Full Pipeline" to chain everything in one shot.

Outputs are cached in a file geodatabase (default: <project_folder>/data/
canal_footprint.gdb). Re-running a tool reads the cache unless the "Refresh"
box is checked.

Reloads
-------
.pyt files are cached by Pro. Sibling modules (data_sources, pipeline_core)
are explicitly reloaded each time the toolbox is opened so edits take effect
without restarting Pro.
"""

from __future__ import annotations

import importlib
import os
import sys

import arcpy

# Make sibling modules importable from the .pyt's folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import data_sources  # noqa: E402
import pipeline_core  # noqa: E402

importlib.reload(data_sources)
importlib.reload(pipeline_core)


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------
class Toolbox:
    def __init__(self):
        self.label = "Canal Footprint Tools"
        self.alias = "canal_footprint"
        self.tools = [
            FetchNhdCanals,
            FetchResidentialParcels,
            FilterCanalsResidential,
            RunFullPipeline,
            ExpandResidentialCanals,
            PolygonizeResidentialCanals,
            ExtractFromNHDArea,
            ExtractFromAltFlowlines,
            FilterPolygonCanals,
            UnionResidentialCanals,
        ]


# ---------------------------------------------------------------------------
# Shared parameter helpers
# ---------------------------------------------------------------------------
def _gdb_param(name: str = "out_gdb", label: str = "Output file geodatabase"):
    p = arcpy.Parameter(
        displayName=label,
        name=name,
        datatype="DEWorkspace",
        parameterType="Required",
        direction="Input",
    )
    p.filter.list = ["Local Database"]
    return p


def _refresh_param():
    p = arcpy.Parameter(
        displayName="Refresh (re-pull from REST, ignore cache)",
        name="refresh",
        datatype="GPBoolean",
        parameterType="Optional",
        direction="Input",
    )
    p.value = False
    return p


def _counties_param():
    p = arcpy.Parameter(
        displayName="Counties (leave empty for all 67)",
        name="counties",
        datatype="GPString",
        parameterType="Optional",
        direction="Input",
        multiValue=True,
    )
    p.filter.type = "ValueList"
    p.filter.list = list(data_sources.FLORIDA_COUNTIES.keys())
    return p


# ---------------------------------------------------------------------------
# Tool 1: Fetch NHD canals
# ---------------------------------------------------------------------------
class FetchNhdCanals:
    def __init__(self):
        self.label = "1. Fetch NHD Canals (Florida)"
        self.description = (
            "Pull NHDPlus HR flowlines classified as canal/ditch within "
            "Florida and cache them to the working file geodatabase."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        return [_gdb_param(), _refresh_param()]

    def execute(self, parameters, messages):
        gdb = parameters[0].valueAsText
        refresh = bool(parameters[1].value)
        pipeline_core.fetch_nhd_canals(gdb, refresh=refresh)


# ---------------------------------------------------------------------------
# Tool 2: Fetch residential parcels
# ---------------------------------------------------------------------------
class FetchResidentialParcels:
    def __init__(self):
        self.label = "2. Fetch Residential Parcels"
        self.description = (
            "Pull residential-classified parcels from the FL statewide "
            "cadastral service, county by county, and cache them."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        gdb = _gdb_param()
        counties = _counties_param()
        url = arcpy.Parameter(
            displayName="Parcel REST URL (override if FGIO URL changes)",
            name="parcel_url",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        url.value = data_sources.PARCEL_REST_URL
        return [gdb, counties, url, _refresh_param()]

    def execute(self, parameters, messages):
        gdb = parameters[0].valueAsText
        counties_raw = parameters[1].valueAsText
        parcel_url = parameters[2].valueAsText or data_sources.PARCEL_REST_URL
        refresh = bool(parameters[3].value)

        counties = None
        if counties_raw:
            # Pro returns multi-value strings as semicolon-delimited and
            # quotes any value containing spaces.
            counties = [
                c.strip("' ").strip()
                for c in counties_raw.split(";")
                if c.strip()
            ]
        pipeline_core.fetch_residential_parcels(
            gdb, counties=counties, parcel_url=parcel_url, refresh=refresh
        )


# ---------------------------------------------------------------------------
# Tool 3: Filter canals by residential adjacency
# ---------------------------------------------------------------------------
class FilterCanalsResidential:
    def __init__(self):
        self.label = "3. Filter Canals by Residential Adjacency"
        self.description = (
            "Keep canals whose buffered footprint is dominated by "
            "residential parcels."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        canals = arcpy.Parameter(
            displayName="Input canals feature class",
            name="canals_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Input residential parcels feature class",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output residential canals feature class",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        buf = arcpy.Parameter(
            displayName="Buffer distance (meters)",
            name="buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        buf.value = 50.0
        thr = arcpy.Parameter(
            displayName="Residential coverage threshold (0-1)",
            name="threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        thr.value = 0.5
        keep = arcpy.Parameter(
            displayName="Keep intermediate buffer + tabulation outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [canals, parcels, out_fc, buf, thr, keep]

    def execute(self, parameters, messages):
        pipeline_core.filter_canals_by_residential(
            canals_fc=parameters[0].valueAsText,
            parcels_fc=parameters[1].valueAsText,
            out_fc=parameters[2].valueAsText,
            buffer_m=float(parameters[3].value),
            threshold=float(parameters[4].value),
            keep_intermediate=bool(parameters[5].value),
        )


# ---------------------------------------------------------------------------
# Tool 4: Run full pipeline
# ---------------------------------------------------------------------------
class RunFullPipeline:
    def __init__(self):
        self.label = "4. Run Full Pipeline"
        self.description = (
            "End-to-end: fetch NHD canals, fetch residential parcels, "
            "and filter to residential-adjacent canals. Skips stages whose "
            "outputs are already cached unless Refresh is checked."
        )
        self.canRunInBackground = False
    def getParameterInfo(self):
        gdb = _gdb_param()
        counties = _counties_param()
        local_parcels = arcpy.Parameter(
            displayName="Local Parcels Feature Class (Optional)",
            name="local_parcels",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        buf = arcpy.Parameter(
            displayName="Buffer distance (meters)",
            name="buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        buf.value = 50.0
        thr = arcpy.Parameter(
            displayName="Residential coverage threshold (0-1)",
            name="threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        thr.value = 0.5
        return [gdb, counties, local_parcels, buf, thr, _refresh_param()]

    def execute(self, parameters, messages):
        # 1. Capture parameter values by their index in getParameterInfo
        gdb = parameters[0].valueAsText
        counties_raw = parameters[1].valueAsText
        local_path = parameters[2].valueAsText  # The new Local Parcels parameter
        buffer_m = float(parameters[3].value)
        threshold = float(parameters[4].value)
        refresh = bool(parameters[5].value)

        # 2. Process the counties string into a list
        counties = None


# ---------------------------------------------------------------------------
# Tool 5: Expand residential canals (catch overlooked)
# ---------------------------------------------------------------------------
class ExpandResidentialCanals:
    def __init__(self):
        self.label = "5. Expand Residential Canals (catch overlooked)"
        self.description = (
            "Walks outward from canals that passed the strict filter, "
            "rescuing network-connected canals that didn't quite meet the "
            "0.5 threshold but are clearly part of the residential network "
            "(edges of subdivisions, segments crossing common areas, etc)."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        full = arcpy.Parameter(
            displayName="Full NHD canals (statewide)",
            name="full_canals_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        passed = arcpy.Parameter(
            displayName="Canals that passed the primary filter",
            name="passed_canals_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Residential parcels",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output expanded residential canals",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        conn = arcpy.Parameter(
            displayName="Connectivity distance (meters)",
            name="connectivity_distance_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        conn.value = 5.0
        thr = arcpy.Parameter(
            displayName="Relaxed coverage threshold (0-1)",
            name="relaxed_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        thr.value = 0.25
        buf = arcpy.Parameter(
            displayName="Buffer distance for coverage tabulation (meters)",
            name="buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        buf.value = 50.0
        iters = arcpy.Parameter(
            displayName="Max expansion iterations",
            name="iterations",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
        )
        iters.value = 3
        keep = arcpy.Parameter(
            displayName="Keep intermediate buffer + tabulation outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [full, passed, parcels, out_fc, conn, thr, buf, iters, keep]

    def execute(self, parameters, messages):
        pipeline_core.expand_residential_canals(
            full_canals_fc=parameters[0].valueAsText,
            passed_canals_fc=parameters[1].valueAsText,
            parcels_fc=parameters[2].valueAsText,
            out_fc=parameters[3].valueAsText,
            connectivity_distance_m=float(parameters[4].value),
            relaxed_threshold=float(parameters[5].value),
            buffer_m=float(parameters[6].value),
            iterations=int(parameters[7].value),
            keep_intermediate=bool(parameters[8].value),
        )


# ---------------------------------------------------------------------------
# Tool 6: Polygonize residential canals
# ---------------------------------------------------------------------------
class PolygonizeResidentialCanals:
    def __init__(self):
        self.label = "6. Polygonize Residential Canals"
        self.description = (
            "Convert residential canal lines into water-surface polygons. "
            "Buffers each line by half-width and either intersects with "
            "NHDArea water polygons (if provided) or erases residential "
            "parcels (default). Output includes computed area_m2 and "
            "area_km2 attributes per polygon."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        canals = arcpy.Parameter(
            displayName="Input canal lines",
            name="canals_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Residential parcels",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output canal polygons",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        nhd_area = arcpy.Parameter(
            displayName="NHDArea water polygons (optional, for accurate "
                        "water-edge geometry)",
            name="nhdarea_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        buf = arcpy.Parameter(
            displayName="Half-width buffer distance (meters)",
            name="buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        buf.value = 25.0
        min_area = arcpy.Parameter(
            displayName="Minimum polygon area (square meters, drops slivers)",
            name="min_polygon_area_m2",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        min_area.value = 30.0
        endcap = arcpy.Parameter(
            displayName="Buffer line end-cap (FLAT avoids semicircle "
                        "artifacts at canal mouths; ROUND extends past "
                        "endpoints)",
            name="line_end_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        endcap.filter.type = "ValueList"
        endcap.filter.list = ["FLAT", "ROUND"]
        endcap.value = "FLAT"
        keep = arcpy.Parameter(
            displayName="Keep intermediate buffer / intersect / erase outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [canals, parcels, out_fc, nhd_area, buf, min_area,
                endcap, keep]

    def execute(self, parameters, messages):
        nhdarea_val = parameters[3].valueAsText
        pipeline_core.polygonize_residential_canals(
            canals_fc=parameters[0].valueAsText,
            parcels_fc=parameters[1].valueAsText,
            out_fc=parameters[2].valueAsText,
            nhdarea_fc=nhdarea_val if nhdarea_val else None,
            buffer_m=float(parameters[4].value),
            min_polygon_area_m2=float(parameters[5].value),
            line_end_type=(parameters[6].valueAsText or "FLAT").upper(),
            keep_intermediate=bool(parameters[7].value),
        )


# ---------------------------------------------------------------------------
# Tool 7: Extract residential canals from NHDArea (line-blind communities)
# ---------------------------------------------------------------------------
class ExtractFromNHDArea:
    def __init__(self):
        self.label = (
            "7. Extract Residential Canals from NHDArea (line-blind)"
        )
        self.description = (
            "For communities where canals aren't in NHDFlowline (Marco "
            "Island, parts of Naples, Keys), extract the canal portion "
            "from larger NHDArea water polygons by constraining to a "
            "residential parcel buffer and filtering elongated shapes."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        nhdarea = arcpy.Parameter(
            displayName="NHDArea polygons",
            name="nhdarea_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Residential parcels",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output canal polygons",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        counties = arcpy.Parameter(
            displayName="Counties to process (leave empty to loop all 67)",
            name="counties_t7",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        counties.filter.type = "ValueList"
        counties.filter.list = list(data_sources.FLORIDA_COUNTIES.keys())
        county_bdy = arcpy.Parameter(
            displayName="Florida counties boundary polygons (recommended "
                        "for local parcel layers where CO_NO isn't a "
                        "true county code)",
            name="counties_boundary_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        county_name_fld = arcpy.Parameter(
            displayName="County name field on the boundary layer",
            name="county_name_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        county_name_fld.value = "NAME"
        pbuf = arcpy.Parameter(
            displayName="Parcel buffer distance (meters)",
            name="parcel_buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        pbuf.value = 50.0
        fc_field = arcpy.Parameter(
            displayName="FCode field name",
            name="fcode_field",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        fc_field.value = "FCode"
        max_comp = arcpy.Parameter(
            displayName="Max compactness (drop above; canals < ~0.3)",
            name="max_compactness",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        max_comp.value = 0.3
        min_area = arcpy.Parameter(
            displayName="Minimum polygon area (square meters)",
            name="min_polygon_area_m2",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        min_area.value = 100.0
        max_width = arcpy.Parameter(
            displayName="Max polygon width meters (drops elongated-but-wide "
                        "false positives like bay edges, causeway shores; "
                        "leave empty to disable)",
            name="max_width_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        res_thresh = arcpy.Parameter(
            displayName="Residential coverage threshold (0-1; fraction of "
                        "each candidate's surrounding buffer that must be "
                        "residential parcels; 0 disables)",
            name="residential_coverage_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        res_thresh.value = 0.3
        existing = arcpy.Parameter(
            displayName="Existing polygons to subtract (optional, e.g., "
                        "Tool 6 output to avoid duplicates)",
            name="erase_existing_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        keep = arcpy.Parameter(
            displayName="Keep intermediate outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [nhdarea, parcels, out_fc, counties, county_bdy,
                county_name_fld, pbuf, fc_field, max_comp, min_area,
                max_width, res_thresh, existing, keep]

    def execute(self, parameters, messages):
        counties_raw = parameters[3].valueAsText
        counties_list = None
        if counties_raw:
            counties_list = [
                c.strip("' ").strip()
                for c in counties_raw.split(";")
                if c.strip()
            ]
        bdy_val = parameters[4].valueAsText
        max_w_param = parameters[10].value
        max_w = float(max_w_param) if max_w_param else None
        existing_val = parameters[12].valueAsText
        pipeline_core.extract_residential_canals_from_nhdarea(
            nhdarea_fc=parameters[0].valueAsText,
            parcels_fc=parameters[1].valueAsText,
            out_fc=parameters[2].valueAsText,
            counties=counties_list,
            counties_boundary_fc=bdy_val if bdy_val else None,
            county_name_field=parameters[5].valueAsText or "NAME",
            parcel_buffer_m=float(parameters[6].value),
            fcode_field=parameters[7].valueAsText or "FCode",
            max_compactness=float(parameters[8].value),
            min_polygon_area_m2=float(parameters[9].value),
            max_width_m=max_w,
            residential_coverage_threshold=float(parameters[11].value),
            erase_existing_fc=existing_val if existing_val else None,
            keep_intermediate=bool(parameters[13].value),
        )


# ---------------------------------------------------------------------------
# Tool 9: Extract canals from alternative NHDFlowline FCodes
# (55800 Artificial Path, 56600 Shoreline outlines)
# ---------------------------------------------------------------------------
class ExtractFromAltFlowlines:
    def __init__(self):
        self.label = (
            "9. Extract Canals from Alt Flowlines (55800 + 56600)"
        )
        self.description = (
            "Capture canals that NHD didn't classify as FCode 33600. "
            "Track A handles FCode 55800 (Artificial Path) centerlines "
            "via buffering, Track B handles FCode 56600 (Shoreline) "
            "outline polylines via FeatureToPolygon. Both apply the "
            "residential parcel buffer constraint plus geometric filters."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        flow = arcpy.Parameter(
            displayName="NHDFlowline source layer",
            name="nhdflowline_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Residential parcels",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output canal polygons",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        counties = arcpy.Parameter(
            displayName="Counties to process (leave empty to loop all 67)",
            name="counties_t9",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        counties.filter.type = "ValueList"
        counties.filter.list = list(data_sources.FLORIDA_COUNTIES.keys())
        county_bdy = arcpy.Parameter(
            displayName="Florida counties boundary polygons "
                        "(required: fl_counties)",
            name="counties_boundary_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        county_name_fld = arcpy.Parameter(
            displayName="County name field on the boundary layer",
            name="county_name_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        county_name_fld.value = "NAME"
        fc_field = arcpy.Parameter(
            displayName="FCode field name on NHDFlowline",
            name="fcode_field",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        fc_field.value = "fcode"
        line_buf = arcpy.Parameter(
            displayName="Half-width buffer for 55800 centerlines (m)",
            name="line_buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        line_buf.value = 25.0
        pbuf = arcpy.Parameter(
            displayName="Parcel buffer distance (m)",
            name="parcel_buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        pbuf.value = 50.0
        max_comp = arcpy.Parameter(
            displayName="Max compactness (0-1)",
            name="max_compactness",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        max_comp.value = 0.3
        min_area = arcpy.Parameter(
            displayName="Minimum polygon area (m^2)",
            name="min_polygon_area_m2",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        min_area.value = 100.0
        max_w = arcpy.Parameter(
            displayName="Max polygon width (m, optional)",
            name="max_width_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        res_thresh = arcpy.Parameter(
            displayName="Residential coverage threshold (0-1; fraction of "
                        "each candidate's surrounding buffer that must be "
                        "residential parcels; 0 disables)",
            name="residential_coverage_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        res_thresh.value = 0.3
        existing = arcpy.Parameter(
            displayName="Existing polygons to subtract (optional: pass "
                        "Tool 6/7 outputs to avoid duplicates)",
            name="erase_existing_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        keep = arcpy.Parameter(
            displayName="Keep intermediate outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [flow, parcels, out_fc, counties, county_bdy,
                county_name_fld, fc_field, line_buf, pbuf,
                max_comp, min_area, max_w, res_thresh, existing, keep]

    def execute(self, parameters, messages):
        counties_raw = parameters[3].valueAsText
        counties_list = None
        if counties_raw:
            counties_list = [
                c.strip("' ").strip()
                for c in counties_raw.split(";")
                if c.strip()
            ]
        max_w_val = parameters[11].value
        existing_val = parameters[13].valueAsText
        pipeline_core.extract_canals_from_alt_flowlines(
            nhdflowline_fc=parameters[0].valueAsText,
            parcels_fc=parameters[1].valueAsText,
            out_fc=parameters[2].valueAsText,
            counties=counties_list,
            counties_boundary_fc=parameters[4].valueAsText,
            county_name_field=parameters[5].valueAsText or "NAME",
            fcode_field=parameters[6].valueAsText or "fcode",
            line_buffer_m=float(parameters[7].value),
            parcel_buffer_m=float(parameters[8].value),
            max_compactness=float(parameters[9].value),
            min_polygon_area_m2=float(parameters[10].value),
            max_width_m=float(max_w_val) if max_w_val else None,
            residential_coverage_threshold=float(parameters[12].value),
            erase_existing_fc=existing_val if existing_val else None,
            keep_intermediate=bool(parameters[14].value),
        )


# ---------------------------------------------------------------------------
# Tool 10: Filter generic polygon canal sources by residential adjacency
# ---------------------------------------------------------------------------
class FilterPolygonCanals:
    def __init__(self):
        self.label = (
            "10. Filter Polygon Canals by Residential (FLUCCS, etc.)"
        )
        self.description = (
            "Apply the residential-adjacency filter to any pre-classified "
            "polygon canal dataset — FLUCCS canal classes ('Canals and "
            "Locks' + 'Channelized Waterways - Canals'), or other "
            "authoritative canal polygon sources. Outputs the canals "
            "adjacent to residential parcels, ready to union with Tool "
            "6/7/9 outputs via Tool 8."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        canals = arcpy.Parameter(
            displayName="Pre-classified canal polygons "
                        "(e.g., FLUCCS canal subset)",
            name="canal_polygons_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        parcels = arcpy.Parameter(
            displayName="Residential parcels",
            name="parcels_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        out_fc = arcpy.Parameter(
            displayName="Output filtered canal polygons",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        counties = arcpy.Parameter(
            displayName="Counties to process (leave empty to loop all 67)",
            name="counties_t10",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
        )
        counties.filter.type = "ValueList"
        counties.filter.list = list(data_sources.FLORIDA_COUNTIES.keys())
        county_bdy = arcpy.Parameter(
            displayName="Florida counties boundary polygons "
                        "(required: fl_counties)",
            name="counties_boundary_fc",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        county_name_fld = arcpy.Parameter(
            displayName="County name field on the boundary layer",
            name="county_name_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        county_name_fld.value = "NAME"
        pbuf = arcpy.Parameter(
            displayName="Parcel buffer distance (m)",
            name="parcel_buffer_m",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        pbuf.value = 50.0
        max_comp = arcpy.Parameter(
            displayName="Max compactness (0-1; 1.0 = disabled, "
                        "default for trusted canal sources)",
            name="max_compactness",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        max_comp.value = 1.0
        min_area = arcpy.Parameter(
            displayName="Minimum polygon area (m^2)",
            name="min_polygon_area_m2",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        min_area.value = 100.0
        max_w = arcpy.Parameter(
            displayName="Max polygon width (m, leave empty to disable)",
            name="max_width_m",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        res_thresh = arcpy.Parameter(
            displayName="Residential coverage threshold (0-1; fraction of "
                        "each candidate's surrounding buffer that must be "
                        "residential parcels; 0 disables)",
            name="residential_coverage_threshold",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        res_thresh.value = 0.3
        src_tag = arcpy.Parameter(
            displayName="SOURCE tag value to write on each output polygon",
            name="source_tag",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        src_tag.value = "fluccs_residential"
        existing = arcpy.Parameter(
            displayName="Existing polygons to subtract (optional: pass "
                        "Tool 6/7/9 outputs to avoid duplicates)",
            name="erase_existing_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        keep = arcpy.Parameter(
            displayName="Keep intermediate outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [canals, parcels, out_fc, counties, county_bdy,
                county_name_fld, pbuf, max_comp, min_area, max_w,
                res_thresh, src_tag, existing, keep]

    def execute(self, parameters, messages):
        counties_raw = parameters[3].valueAsText
        counties_list = None
        if counties_raw:
            counties_list = [
                c.strip("' ").strip()
                for c in counties_raw.split(";")
                if c.strip()
            ]
        max_w_val = parameters[9].value
        existing_val = parameters[12].valueAsText
        pipeline_core.filter_polygon_canals_by_residential(
            canal_polygons_fc=parameters[0].valueAsText,
            parcels_fc=parameters[1].valueAsText,
            out_fc=parameters[2].valueAsText,
            counties=counties_list,
            counties_boundary_fc=parameters[4].valueAsText,
            county_name_field=parameters[5].valueAsText or "NAME",
            parcel_buffer_m=float(parameters[6].value),
            max_compactness=float(parameters[7].value),
            min_polygon_area_m2=float(parameters[8].value),
            max_width_m=float(max_w_val) if max_w_val else None,
            residential_coverage_threshold=float(parameters[10].value),
            source_tag=parameters[11].valueAsText or "fluccs_residential",
            erase_existing_fc=existing_val if existing_val else None,
            keep_intermediate=bool(parameters[13].value),
        )


# ---------------------------------------------------------------------------
# Tool 8: Union N polygon inputs into a unified residential canal layer
# ---------------------------------------------------------------------------
class UnionResidentialCanals:
    def __init__(self):
        self.label = "8. Union Canal Polygons (multi-input)"
        self.description = (
            "Combine any number of residential canal polygon feature "
            "classes (Tool 6, Tool 7, Tool 9, or other) into a single "
            "unified layer. Each polygon is tagged with SOURCE_TOOL "
            "(derived from the input FC basename). Optionally dissolves "
            "overlaps for an unambiguous area total."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        inputs = arcpy.Parameter(
            displayName="Input polygon feature classes (Tool 6, 7, 9, 10)",
            name="input_fcs",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
            multiValue=True,
        )
        out_fc = arcpy.Parameter(
            displayName="Output unified canal polygons",
            name="out_fc",
            datatype="DEFeatureClass",
            parameterType="Required",
            direction="Output",
        )
        priority = arcpy.Parameter(
            displayName="Priority source (optional: this FC's polygons "
                        "stay unmodified; other inputs are erased by it "
                        "before merge — typically FLUCCS / Tool 10 output)",
            name="priority_fc",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input",
        )
        dissolve = arcpy.Parameter(
            displayName="Dissolve overlaps (clean area total; leave "
                        "unchecked for QC-friendly output)",
            name="dissolve_overlaps",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        dissolve.value = False
        keep = arcpy.Parameter(
            displayName="Keep intermediate tagged + merged outputs",
            name="keep_intermediate",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        keep.value = False
        return [inputs, out_fc, priority, dissolve, keep]

    def execute(self, parameters, messages):
        raw = parameters[0].valueAsText
        paths: list[str] = []
        if raw:
            for chunk in raw.split(";"):
                p = chunk.strip("' ").strip()
                if p:
                    paths.append(p)
        priority_val = parameters[2].valueAsText
        pipeline_core.union_residential_canal_polygons(
            input_fcs=paths,
            out_fc=parameters[1].valueAsText,
            priority_fc=priority_val if priority_val else None,
            dissolve_overlaps=bool(parameters[3].value),
            keep_intermediate=bool(parameters[4].value),
        )
