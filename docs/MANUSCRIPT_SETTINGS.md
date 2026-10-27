# Settings used for the manuscript run

The statewide run behind the manuscript (May 2026) did not use every tool default. This table records the values reconstructed from the ArcGIS Pro geoprocessing history and the layer metadata. Use it to reproduce the published layer.

| Step | Tool | Setting | Tool default | Manuscript value |
| --- | --- | --- | --- | --- |
| 1 | 1. Fetch NHD Canals | NHDPlus HR FCodes | 33600, 33601, 33603 | Same |
| 2 | 3. Filter Canals by Residential Adjacency (run through 4. Run Full Pipeline) | Buffer distance | 50 m | 50 m |
| | | Residential coverage threshold | 0.5 | **0.15** |
| | | Parcels | REST query | Local statewide parcel layer filtered to residential DOR land-use codes 000–009 (2025 tax roll) |
| 3 | 5. Expand Residential Canals | Connectivity distance | 5 m | 5 m |
| | | Relaxed coverage threshold | 0.25 | 0.25 |
| | | Buffer distance | 50 m | 50 m |
| | | Iterations | 3 | 3 |
| 4 | (ArcGIS Select By Location) | Keep expanded lines that intersect a residential parcel | n/a | Applied |
| 5 | 6. Polygonize Residential Canals | Half-width buffer | 25 m | 25 m |
| | | End cap | FLAT | FLAT |
| | | Clip to NHDArea water | Optional | Applied |
| | | Minimum area | 30 m² | 30 m² |
| 6 | 7. Extract Residential Canals from NHDArea | Minimum area | 100 m² | 100 m² |
| | | Residential coverage threshold | 0.3 | **0.15** |
| | | Parcel buffer | 50 m | 50 m |
| | | Subtract existing polygons | Optional | Not used |
| | | Maximum compactness | 0.3 | 0.3 |
| 7 | 10. Filter Polygon Canals (FLUCCS) | FLUCCS Level 3 classes | n/a | 5120 (Channelized waterways, canals) and 8160 (Canals and locks), from `lu_l3_state_jul25` |
| | | Residential coverage threshold | 0.3 | **0.15** |
| | | Parcel buffer, minimum area | 50 m, 100 m² | 50 m, 100 m² |
| | | County reruns | n/a | Charlotte, Glades, Hendry; Broward, Miami-Dade, Palm Beach |
| 8 | 8. Union Canal Polygons | Priority source | Optional | FLUCCS (Tool 10 output) |
| | | Dissolve overlaps | Off | On |
| – | 9. Extract Canals from Alt Flowlines | FCodes 55800 and 56600 | n/a | Run in testing, then dropped |

Filtering was tuned through the residential coverage thresholds, minimum areas and buffers above; the geometric filters (compactness, maximum width) were left at their defaults.

Software: ArcGIS Pro 3.5 (Advanced license, Spatial Analyst).

## Post-processing

| Step | Tool | Settings used |
| --- | --- | --- |
| Centerlines | `postprocessing/centerlines/CHAWQ_Canal_Centerline_Tools.pyt`, Polygons to Centerlines | Point spacing 3 m, pruning factor 1.5, minimum spur 5 m, simplify 0.5 m, extend dead ends |
| Distance from the mouth | `postprocessing/distance/CanalDistanceRasters.pyt`, Per-Canal Distance Rasters | Cell size 1 m, processing buffer 50 m, mouth touch tolerance 3 m; water cost 1, land impassable |
| Zonal statistics | `postprocessing/centerlines/CHAWQ_Canal_Centerline_Tools.pyt`, Zonal Stats from County Rasters | Field prefix `DIST_`, county field `County` |

Lake Okeechobee systems that reach open water only through flood-control conveyance canals were run with the FLUCCS conveyance canals added to the water surface and mouths drawn at the lake; their rasters were then masked to the canal polygon.

Result of the union: 3,473 candidate polygons covering 164.3 km². The published layer of 3,129 artificial residential waterway systems followed manual review against Esri World Imagery; that review is not automated and is described in the manuscript.

## Trial settings (informal sensitivity)

Before the statewide run, the Tool 3 threshold was lowered from 0.50 to 0.15, the Tool 7 threshold was tested at 0.25 and 0.30 before 0.15, the parcel buffer was raised from 30 to 50 m, and a 5 m Tool 6 half-width was tried on Sanibel.
