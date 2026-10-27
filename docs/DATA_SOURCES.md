# Data sources

No data are stored in this repository. Every input is public, and each source should be downloaded or queried from its provider and credited with the provider's own citation. The pipeline queries the two REST services below directly; the other layers are downloaded once and passed to the tools as local feature classes.

| Dataset | Provider | Used by | How to get it | Version used in the manuscript |
| --- | --- | --- | --- | --- |
| NHDPlus High Resolution flowlines (FCodes 33600, 33601, 33603) | U.S. Geological Survey | Tools 1, 3, 4, 5, 6 | Queried automatically from the USGS National Map REST service (`NHD_SERVICE` in `toolbox/data_sources.py`) | National Release 1; queried 2026-05-06 |
| National Hydrography Dataset (NHD) area features, `NHDArea` | U.S. Geological Survey | Tools 6 and 7 | USGS National Map Downloader, Florida state extract | Florida state shapefile, published 2024-01-16 |
| Statewide Land Use Land Cover (FLUCCS Level 3; codes 5120 and 8160) | Florida Department of Environmental Protection (FDEP) and the water management districts | Tool 10 | FDEP Geospatial Open Data | `lu_l3_state_jul25` |
| Florida statewide parcels (cadastral, DOR land-use codes) | Florida Department of Revenue, Property Tax Oversight Program | Tools 2–10 | Queried automatically from the Florida Geographic Information Office REST service (`PARCEL_REST_URL`), or downloaded and passed as a local layer | 2025 tax roll |
| Water Management District boundaries | South Florida Water Management District, via FDEP | Reporting (after the pipeline) | FDEP Map Direct open data | Accessed 2026-10-14 |
| World Imagery basemap | Esri and contributors | Manual review and digitizing (not used by the code) | ArcGIS Pro basemap | Accessed 2026-05-04 to 2026-09-15 |

## Links

- NHDPlus HR REST service: <https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer>
- NHDPlus HR data release: <https://doi.org/10.5066/P9WFOBQI>
- USGS National Map Downloader (NHD state extracts): <https://apps.nationalmap.gov/downloader/>
- FDEP Statewide Land Use Land Cover: <https://geodata.dep.state.fl.us/datasets/FDEP::statewide-land-use-land-cover/about>
- Florida statewide parcels (FGIO search): <https://geodata.floridagio.gov/search?q=parcels>
- Water Management District boundaries: <https://mapdirect-fdep.opendata.arcgis.com/datasets/sfwmd::water-management-district-boundaries/about>

## Citations

- Florida Department of Revenue, Property Tax Oversight Program. (2026). *Florida statewide parcels*. Florida Geographic Information Office. Accessed May, 2026 at <https://geodata.floridagio.gov/search?q=parcels>.
- U.S. Geological Survey. (2022). *USGS National Hydrography Dataset Plus High Resolution National Release 1 FileGDB: U.S. Geological Survey*. Accessed May, 2026 at <https://doi.org/10.5066/P9WFOBQI>.
- Florida Department of Environmental Protection. (2025). *Statewide Land Use Land Cover*. Accessed May, 2026 at <https://geodata.dep.state.fl.us/datasets/FDEP::statewide-land-use-land-cover/about>.
- U.S. Geological Survey. *National Hydrography Dataset (NHD), Florida state extract*. Accessed May, 2026 at <https://geodata.dep.state.fl.us/datasets/69bb0b2544dd4551942e96800802441c_10/explore?location=28.037850%2C-83.910500%2C5>. 

## Terms of use

The MIT license in this repository covers the code only. Each dataset carries its provider's own terms of use and attribution requirements.
