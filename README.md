# Residential Canal Pipeline

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23700657.svg)](https://doi.org/10.5281/zenodo.23700657)

ArcGIS Pro tools that map artificial residential waterways (ARWs) across Florida: dredged canals and basins fronted by residential property that are large enough to hold a boat and connect by water to a natural water body. The pipeline pulls canal features from public hydrography and land-cover data, keeps those fronted by residential parcels, converts them to polygons, and merges the sources into one statewide candidate layer for manual review.

Developed by the Coastal Habitat and Water Quality Initiative (C-HAWQ), a Florida nonprofit. The code supports the manuscript *[manuscript title and citation to add on publication]*.

## Workflow

1. **Canal lines.** Query NHDPlus High Resolution flowlines with canal and ditch feature codes from the USGS REST service (Tool 1).
2. **Residential filter.** Keep lines where residential parcels (Florida Department of Revenue land-use codes 000–009) cover a set share of a buffer around the line (Tools 2 and 3, or Tool 4 to run both).
3. **Network expansion.** Add connected canal segments that pass a relaxed residential threshold, to catch reaches the first filter missed (Tool 5).
4. **Polygons.** Buffer the lines to a half-width and clip them to NHD water areas (Tool 6).
5. **Polygon sources.** Extract residential canals from NHD area features (Tool 7) and from FLUCCS land-cover canal classes (Tool 10). Tool 9 (alternate NHD flowline codes) is included but was not used for the manuscript.
6. **Union.** Merge all polygon sources, give priority to a chosen source where they overlap, and tag each polygon with its source (Tool 8).

The output is a candidate layer. The published layer also reflects manual review against aerial imagery, which this repository does not automate.

## Repository layout

```
ResidentialCanalPipeline/
├── README.md
├── LICENSE                     MIT (code only; see docs/DATA_SOURCES.md for data terms)
├── CITATION.cff                How to cite this software
├── toolbox/
│   ├── CanalFootprintTools.pyt     ArcGIS Pro Python toolbox (Tools 1–10)
│   ├── pipeline_core.py            Processing functions called by the toolbox
│   └── data_sources.py             REST endpoints, FCodes, DOR land-use codes, county list
├── postprocessing/
│   ├── centerlines/
│   │   ├── CHAWQ_Canal_Centerline_Tools.pyt   Medial-axis centerlines, length summaries, zonal statistics
│   │   └── centerline_sensitivity.py        Sensitivity of centerline length to the tool settings
│   └── distance/
│       ├── CanalDistanceRasters.pyt          Per-canal in-water distance-from-mouth rasters
│       └── canal_distance_core.py            Processing functions called by that toolbox
├── analysis/
│   ├── chawq_residential_canal_analysis_v2.R   Footprint, distance and canal-form statistics
│   └── chawq_canal_figures_v2.R                Figures and summary tables
└── docs/
    ├── DATA_SOURCES.md         Where to download each dataset and how to cite it
    └── MANUSCRIPT_SETTINGS.md  Parameter values used for the published layer
```

## Requirements

- ArcGIS Pro 3.x with an Advanced license (tested on 3.5). The Distance Accumulation step in post-processing also needs Spatial Analyst.
- Python packages bundled with ArcGIS Pro: `arcpy`, `requests`, `numpy`, `scipy`, `matplotlib`.
- For the analysis scripts: R 4.5 or later with `sf`, `dplyr`, `tidyr`, `readr`, `stringr`, `ggplot2` and `scales` (optional: `writexl`, `lwgeom`).
- Internet access for the USGS and Florida Geographic Information Office REST services.

## Quick start

1. Download or clone this repository.
2. In ArcGIS Pro, open the Catalog pane, right-click **Toolboxes**, choose **Add Toolbox**, and select `toolbox/CanalFootprintTools.pyt`. Keep the three files in `toolbox/` together; the toolbox imports the other two.
3. Download the local datasets listed in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) (NHD area features, FLUCCS land cover, and optionally a local copy of the statewide parcels).
4. Run **4. Run Full Pipeline** with an output file geodatabase, then Tools 5, 6, 7 and 10, and combine the polygon outputs with **8. Union Canal Polygons**. Outputs are cached in the geodatabase; check **Refresh** to re-query the REST services.
5. To reproduce the published layer, use the values in [docs/MANUSCRIPT_SETTINGS.md](docs/MANUSCRIPT_SETTINGS.md). Several differ from the tool defaults.

All processing is done in NAD83(2011) Florida GDL Albers (EPSG:6439), an equal-area projection, so areas and lengths are in square meters and meters.

## Post-processing and analysis

- **Centerlines.** `postprocessing/centerlines/CHAWQ_Canal_Centerline_Tools.pyt`, tool *Polygons to Centerlines*. Boundary points every 3 m, medial axis from the Voronoi diagram of those points, side branches pruned below 1.5 times the local half-width or 5 m, lines simplified to 0.5 m, dead ends extended to the canal terminus. The geometry functions use only `numpy` and `scipy`, so they can be tested outside ArcGIS.
- **Centerline sensitivity.** `postprocessing/centerlines/centerline_sensitivity.py` reruns the centerline geometry outside ArcGIS with alternative point spacing and pruning settings and reports the change in total length.
- **Distance from the mouth.** `postprocessing/distance/CanalDistanceRasters.pyt` builds one cost-distance raster per canal system with Spatial Analyst Distance Accumulation (1 m cells; water cost 1, land impassable), starting from hand-drawn mouth lines. Mouth cells are grown onto water by one cell, or up to the 3 m touch tolerance if needed, so every run starts inside the canal. Keep `canal_distance_core.py` beside the toolbox. Rasters are then summarized per polygon with the *Zonal Stats from County Rasters* tool.
- **Statistics and figures.** Edit `ms_dir` at the top of each R script (or set the `CHAWQ_MS_DIR` environment variable) to point at your data folder, then run the analysis script before the figures script.

## Data

No data are included. See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for every source, its download link and its citation. Please credit the data providers in any work that uses this code.

## Citing

If you use this code, please cite it:

> Hewitt, N. (2026). *Residential Canal Pipeline: ArcGIS Pro tools for mapping Florida's artificial residential waterways* (Version 1.0.0) [Computer software]. Zenodo. https://doi.org/10.5281/zenodo.23700657

Please also cite the manuscript once published, and credit the data providers listed in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## License

Code: MIT License, © 2026 Nathan Hewitt. Data: each dataset is subject to its provider's terms.
