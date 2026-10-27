# Data sources

No data are stored in this repository. Every input is public, and each source should be downloaded or queried from its provider and credited with the provider's own citation. The pipeline queries the two REST services below directly; the other layers are downloaded once and passed to the tools as local feature classes.

| Dataset | Provider | Used by | How to get it | Version used in the manuscript |
| --- | --- | --- | --- | --- |
| NHDPlus High Resolution flowlines (FCodes 33600, 33601, 33603) | U.S. Geological Survey | Tools 1, 3, 4, 5, 6 | Queried automatically from the USGS National Map REST service (`NHD_SERVICE` in `toolbox/data_sources.py`) | National Release 1; accessed May 2026 |
| National Hydrography Dataset (NHD) area features, `NHDArea` | U.S. Geological Survey | Tools 6 and 7 | USGS Florida state shapefile (`NHD_H_Florida_State_Shape`), obtained through FDEP; also available from the USGS National Map Downloader | Florida state shapefile, published 2024-01-16; accessed May 2026 |
| Land Use and Cover (FLUCCS Level 3) by Water Management District, 2019–2023 (codes 5120 and 8160) | Compiled by the University of Florida GeoPlan Center (Florida Geographic Data Library, FGDL) from the five water management districts and FDEP | Tool 10 | FGDL download (`lu_l3_state_jul25`) | Ed. July 2025, published 2025-07-30; district imagery years NWFWMD 2022, SJRWMD 2022, SFWMD 2021–2023, SRWMD 2023, SWFWMD 2023; accessed May 2026 |
| Florida statewide parcels (cadastral, DOR land-use codes) | Florida Department of Revenue, Property Tax Oversight Program | Tools 2–10 | Queried automatically from the Florida Geographic Information Office REST service (`PARCEL_REST_URL`), or downloaded and passed as a local layer | 2025 tax roll; accessed May 2026 |
| Water Management District boundaries | South Florida Water Management District, via FDEP | Reporting (after the pipeline) | FDEP Map Direct open data | Accessed 2026-10-14 |
| World Imagery basemap | Esri and contributors | Manual review and digitizing (not used by the code) | ArcGIS Pro basemap | Accessed 2026-05-04 to 2026-09-15 |

## Links

- NHDPlus HR REST service: <https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer>
- NHDPlus HR data release: <https://doi.org/10.5066/P9WFOBQI>
- USGS National Map Downloader (NHD state extracts): <https://apps.nationalmap.gov/downloader/>
- Florida Geographic Data Library (FLUCCS Level 3 statewide land use, `lu_l3_state_jul25`): <https://www.fgdl.org/>
- FDEP Statewide Land Use Land Cover (same district data, hosted by FDEP): <https://geodata.dep.state.fl.us/datasets/FDEP::statewide-land-use-land-cover/about>
- USGS National Hydrography Dataset: <https://www.usgs.gov/national-hydrography/national-hydrography-dataset>
- Florida statewide parcels (FGIO search): <https://geodata.floridagio.gov/search?q=parcels>
- Water Management District boundaries: <https://mapdirect-fdep.opendata.arcgis.com/datasets/sfwmd::water-management-district-boundaries/about>

## Citations

Cite each dataset you use. The entries below follow APA 7; check each provider's page for its preferred citation before publishing.

- Florida Department of Revenue, Property Tax Oversight Program. (2026). *Florida statewide parcels* [Data set]. Florida Geographic Information Office. Retrieved May 2026, from <https://geodata.floridagio.gov/search?q=parcels>
- U.S. Geological Survey. (2022). *NHDPlus High Resolution (NHDPlus HR) National Release 1* [Data set]. <https://doi.org/10.5066/P9WFOBQI>
- University of Florida GeoPlan Center. (2025). *Land use and cover (FLUCCS Level 3) by water management district in Florida 2019–2023* (Ed. July 2025) [Data set]. Florida Geographic Data Library. Retrieved May 2026, from <https://www.fgdl.org/>
- U.S. Geological Survey. (2024). *USGS National Hydrography Dataset Best Resolution (NHD) – Florida (published 20240116) shapefile* [Data set]. Retrieved May 2026, from <https://www.usgs.gov/national-hydrography/national-hydrography-dataset>

## Terms of use

The MIT license in this repository covers the code only. Each dataset carries its provider's own terms of use and attribution requirements.
