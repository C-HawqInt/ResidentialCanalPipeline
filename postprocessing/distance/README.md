# Distance from the mouth

`CanalDistanceRasters.pyt` (tool *Per-Canal Distance Rasters*) builds one in-water distance-from-mouth raster per canal system. Keep `canal_distance_core.py` in the same folder; the toolbox imports it. Requires ArcGIS Pro with Spatial Analyst.

For each canal polygon the tool:

1. Sets the processing extent to the polygon plus a buffer (50 m).
2. Rasterizes the polygon at 1 m. Water cells cost 1; land and everything outside is a barrier.
3. Selects the mouth lines that touch the polygon (within 3 m) and rasterizes them.
4. Grows the mouth cells onto water by one cell, or up to the touch tolerance if needed, so every run starts inside the canal.
5. Runs Distance Accumulation, so paths stay in water and route around land.
6. Saves `<CHAWQ_ID>_<County>_<Form>_Distance.tif` in a county subfolder and logs mean and maximum distance to `_manifest.csv`, with completed IDs in `_checkpoint_done.csv` so interrupted runs can resume.

Summarize the rasters per polygon with *Zonal Stats from County Rasters* in `../centerlines/CHAWQ_Canal_Centerline_Tools.pyt`.
