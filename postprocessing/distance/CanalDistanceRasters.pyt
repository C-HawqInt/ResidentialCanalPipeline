# -*- coding: utf-8 -*-
"""
CanalDistanceRasters.pyt
------------------------
ArcGIS Pro Python toolbox: per-canal in-water distance-from-mouth rasters
(flushing / residence-time proxy) for the C-HAWQ canal typology project.

Add this .pyt to ArcGIS Pro (Catalog > Toolboxes > Add) and run the single
tool "Per-Canal Distance Rasters (county-ranked)". The heavy lifting lives in
canal_distance_core.py in the same folder; keep the two files together.

Rebuilt 2026-10-21: only the per-canal tool is included. The core module was
rewritten (original lost) and now grows each mouth by one cell onto water so
mouths drawn on the polygon edge always seed the distance calculation.

Requires the Spatial Analyst extension.
"""

import os
import sys
import importlib

# make sure the core module (same folder as this .pyt) is importable
_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import arcpy  # noqa: E402


class Toolbox(object):
    def __init__(self):
        self.label = "C-HAWQ Canal Distance Rasters"
        self.alias = "chawqCanalDist"
        self.tools = [PerCanalDistanceRasters]


class PerCanalDistanceRasters(object):
    def __init__(self):
        self.label = "Per-Canal Distance Rasters (county-ranked)"
        self.description = (
            "For each residential-canal polygon, compute an in-water "
            "distance-from-mouth raster with Distance Accumulation (flow path "
            "around land, not straight-line). Counties are processed least -> "
            "most canals (polygon count, ties broken by total acres). Rasters "
            "are named CHAWQID_County_TypeForm_Distance.tif and the run is "
            "checkpointed so it can resume."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = []

        canals = arcpy.Parameter(
            displayName="Residential canal polygons",
            name="canals", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        canals.filter.list = ["Polygon"]
        p.append(canals)

        mouths = arcpy.Parameter(
            displayName="Mouth lines",
            name="mouths", datatype="GPFeatureLayer",
            parameterType="Required", direction="Input")
        mouths.filter.list = ["Polyline"]
        p.append(mouths)

        out_folder = arcpy.Parameter(
            displayName="Output folder (rasters + logs)",
            name="out_folder", datatype="DEFolder",
            parameterType="Required", direction="Input")
        p.append(out_folder)

        id_field = arcpy.Parameter(
            displayName="Canal ID field (unique)",
            name="id_field", datatype="Field",
            parameterType="Required", direction="Input")
        id_field.parameterDependencies = [canals.name]
        id_field.value = "CHAWQ_ID"
        p.append(id_field)

        county_field = arcpy.Parameter(
            displayName="County field",
            name="county_field", datatype="Field",
            parameterType="Required", direction="Input")
        county_field.parameterDependencies = [canals.name]
        county_field.value = "County"
        p.append(county_field)

        form_field = arcpy.Parameter(
            displayName="Planform field (for the raster name)",
            name="form_field", datatype="Field",
            parameterType="Required", direction="Input")
        form_field.parameterDependencies = [canals.name]
        form_field.value = "TYP_FORM"
        p.append(form_field)

        system_field = arcpy.Parameter(
            displayName="System ID field (logged only)",
            name="system_field", datatype="Field",
            parameterType="Optional", direction="Input")
        system_field.parameterDependencies = [canals.name]
        system_field.value = "SYSTEM_ID"
        p.append(system_field)

        acres_field = arcpy.Parameter(
            displayName="Area field in acres (for county ranking)",
            name="acres_field", datatype="Field",
            parameterType="Required", direction="Input")
        acres_field.parameterDependencies = [canals.name]
        acres_field.value = "area_acres"
        p.append(acres_field)

        water_field = arcpy.Parameter(
            displayName="Constant water-value field (=1; uniform cost)",
            name="water_field", datatype="Field",
            parameterType="Required", direction="Input")
        water_field.parameterDependencies = [canals.name]
        water_field.value = "WaterVal"
        p.append(water_field)

        cell = arcpy.Parameter(
            displayName="Cell size (m)",
            name="cell_size", datatype="GPDouble",
            parameterType="Required", direction="Input")
        cell.value = 1.0
        p.append(cell)

        buffer_m = arcpy.Parameter(
            displayName="Processing buffer around each polygon (m)",
            name="buffer_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        buffer_m.value = 50.0
        p.append(buffer_m)

        tol = arcpy.Parameter(
            displayName="Mouth touch tolerance (m)",
            name="touch_tolerance_m", datatype="GPDouble",
            parameterType="Required", direction="Input")
        tol.value = 3.0
        p.append(tol)

        subfolders = arcpy.Parameter(
            displayName="Organize rasters into per-county subfolders",
            name="county_subfolders", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        subfolders.value = True
        p.append(subfolders)

        resume = arcpy.Parameter(
            displayName="Resume (skip canals already in the checkpoint)",
            name="resume", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        resume.value = True
        p.append(resume)

        id_subset = arcpy.Parameter(
            displayName="Only these CHAWQ_IDs (file or comma list; blank = all)",
            name="id_subset", datatype="GPString",
            parameterType="Optional", direction="Input")
        p.append(id_subset)

        mouth_id_field = arcpy.Parameter(
            displayName="Mouth ID field (match mouths by ID; blank = spatial touch)",
            name="mouth_id_field", datatype="Field",
            parameterType="Optional", direction="Input")
        mouth_id_field.parameterDependencies = [mouths.name]
        p.append(mouth_id_field)

        return p

    def isLicensed(self):
        try:
            return arcpy.CheckExtension("Spatial") == "Available"
        except Exception:
            return False

    def execute(self, parameters, messages):
        vals = {prm.name: prm for prm in parameters}
        import canal_distance_core as core
        importlib.reload(core)  # pick up edits without restarting Pro

        core.run(
            canals=vals["canals"].valueAsText,
            mouths=vals["mouths"].valueAsText,
            out_folder=vals["out_folder"].valueAsText,
            id_field=vals["id_field"].valueAsText,
            county_field=vals["county_field"].valueAsText,
            form_field=vals["form_field"].valueAsText,
            system_field=(vals["system_field"].valueAsText or "SYSTEM_ID"),
            acres_field=vals["acres_field"].valueAsText,
            water_value_field=vals["water_field"].valueAsText,
            cell_size=float(vals["cell_size"].value),
            buffer_m=float(vals["buffer_m"].value),
            touch_tolerance_m=float(vals["touch_tolerance_m"].value),
            county_subfolders=bool(vals["county_subfolders"].value),
            resume=bool(vals["resume"].value),
            id_subset=(vals["id_subset"].valueAsText or None),
            mouth_id_field=(vals["mouth_id_field"].valueAsText or ""),
        )
