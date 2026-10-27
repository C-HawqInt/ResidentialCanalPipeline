"""
data_sources.py
---------------
Single source of truth for REST endpoints, layer IDs, and lookup tables used
by the canal-footprint pipeline. If a service URL changes, update it here and
the rest of the pipeline picks it up automatically.

Tested against ArcGIS Pro 3.x (Python 3.11, arcpy >= 3.0). The `requests`
package is bundled with arcpy so no extra installs are required.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlencode

import requests


# ---------------------------------------------------------------------------
# NHD / NHDPlus HR  (USGS — The National Map)
# ---------------------------------------------------------------------------
# These endpoints are stable and supported by the USGS. Verify on first run by
# opening NHD_SERVICE in a browser; the layer list is published at the bottom.
NHD_SERVICE: str = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"
)
NHD_FLOWLINE_LAYER: int = 3      # NHDFlowline (lines)
NHD_WATERBODY_LAYER: int = 9     # NHDWaterbody (polygons)

# FCode 33600 = Canal/Ditch (covers most cases). 33601 = Aqueduct,
# 33603 = Stormwater. We pull all three; trim later if 33601/33603 are noisy.
CANAL_FCODES: tuple[int, ...] = (33600, 33601, 33603)


# ---------------------------------------------------------------------------
# Florida statewide parcels
# ---------------------------------------------------------------------------
# The toolbox supports two parcel sources:
#   * "rest"  - live REST query (slower, always current)
#   * "local" - point at a downloaded FGDL statewide parcel feature class
#
# REST option: FGIO hosts the FDOR statewide cadastral feature service.
# Service ItemId: efa909d6b1c841d298b0a649e7f71cf2 (verified May 2026).
# Native SR is EPSG:3086 (FL Albers), 10.8M parcels, max 2000 records/page.
PARCEL_REST_URL: str = (
    "https://services9.arcgis.com/Gh9awoU677aKree0/ArcGIS/rest/services/"
    "Florida_Statewide_Cadastral/FeatureServer/0"
)

# DOR Use Code field name on the parcel layer. The FDOR Cadastral layer uses
# DOR_UC (string, length 4). Some county-specific layers use PA_UC or DORUC.
DOR_FIELD: str = "DOR_UC"

# County identifier field. The FDOR Cadastral layer uses CO_NO (Double type)
# populated with the FIPS county code — NOT the DOR DR-493 code.
COUNTY_FIELD: str = "CO_NO"
COUNTY_FIELD_IS_NUMERIC: bool = True  # affects SQL quoting in pipeline_core

# Florida DOR Use Codes considered "residential" for our purposes.
# Reference: FDOR DR-493 (Property Use Codes), 000-009 = residential
# (000 vacant residential, 001 single family, 002 mobile home, 003 multi-family
# of 10+ units, 004 condominium, 005 cooperative, 006 retirement home,
# 007 miscellaneous residential, 008 multi-family of fewer than 10 units,
# 009 residential common elements). Vacant residential (000) is included.
# DOR_UC is stored as text. The statewide layer used for the manuscript stores
# three-digit codes ("000", "001"); four-digit zero-padded forms ("0000") are
# also accepted so the query still works if a release pads the field.
RESIDENTIAL_DOR_CODES: list[str] = (
    [f"{n:03d}" for n in range(0, 10)]
    + [f"{n:04d}" for n in range(0, 10)]
)

# SQL fragment that selects the residential codes above. Use this in WHERE
# clauses for the parcel REST query.
RESIDENTIAL_DOR_WHERE: str = (
    f"({DOR_FIELD} IN ("
    + ", ".join(f"'{c}'" for c in RESIDENTIAL_DOR_CODES)
    + "))"
)

# Subset of fields to request from the parcel service. Requesting all ~120
# fields + geometry per page caused 504 Gateway Timeouts. These four are the
# minimum we need: county, DOR class, parcel ID, plus OBJECTID (always
# included). Geometry is returned regardless when returnGeometry=true.
PARCEL_OUT_FIELDS: list[str] = ["OBJECTID", "CO_NO", "DOR_UC", "PARCEL_ID"]



# ---------------------------------------------------------------------------
# Florida counties — name to FIPS county code (integer)
# ---------------------------------------------------------------------------
# The FDOR Cadastral CO_NO field is a numeric (Double) FIPS county code with
# no leading zeros. Examples: Collier=21, Lee=71, Miami-Dade=86.
FLORIDA_COUNTIES: dict[str, int] = {
    "Alachua": 1,        "Baker": 3,          "Bay": 5,
    "Bradford": 7,       "Brevard": 9,        "Broward": 11,
    "Calhoun": 13,       "Charlotte": 15,     "Citrus": 17,
    "Clay": 19,          "Collier": 21,       "Columbia": 23,
    "DeSoto": 27,        "Dixie": 29,         "Duval": 31,
    "Escambia": 33,      "Flagler": 35,       "Franklin": 37,
    "Gadsden": 39,       "Gilchrist": 41,     "Glades": 43,
    "Gulf": 45,          "Hamilton": 47,      "Hardee": 49,
    "Hendry": 51,        "Hernando": 53,      "Highlands": 55,
    "Hillsborough": 57,  "Holmes": 59,        "Indian River": 61,
    "Jackson": 63,       "Jefferson": 65,     "Lafayette": 67,
    "Lake": 69,          "Lee": 71,           "Leon": 73,
    "Levy": 75,          "Liberty": 77,       "Madison": 79,
    "Manatee": 81,       "Marion": 83,        "Martin": 85,
    "Miami-Dade": 86,    "Monroe": 87,        "Nassau": 89,
    "Okaloosa": 91,      "Okeechobee": 93,    "Orange": 95,
    "Osceola": 97,       "Palm Beach": 99,    "Pasco": 101,
    "Pinellas": 103,     "Polk": 105,         "Putnam": 107,
    "St. Johns": 109,    "St. Lucie": 111,    "Santa Rosa": 113,
    "Sarasota": 115,     "Seminole": 117,     "Sumter": 119,
    "Suwannee": 121,     "Taylor": 123,       "Union": 125,
    "Volusia": 127,      "Wakulla": 129,      "Walton": 131,
    "Washington": 133,
}

# Counties with significant residential canal networks. Useful for pilot runs
# before scaling statewide. Not exhaustive; add as needed.
PILOT_COUNTIES: list[str] = [
    "Lee",          # Cape Coral
    "Collier",      # Marco Island, Naples
    "Charlotte",    # Punta Gorda Isles
    "Broward",      # Fort Lauderdale
    "Miami-Dade",
    "Monroe",       # Florida Keys
    "Pinellas",     # Tarpon Springs, St. Pete
    "Sarasota",
    "Brevard",      # Indian River Lagoon area
    "Hillsborough", # Apollo Beach, Tampa
]


# ---------------------------------------------------------------------------
# Spatial reference
# ---------------------------------------------------------------------------
# Working SR: NAD83(2011) Florida GDL Albers (EPSG:3086). Equal-area, accurate
# for statewide work, and the de facto standard for FGDL data. Use this for
# any area calculations.
WORKING_SR_EPSG: int = 3086

# REST queries return WGS84 (4326) by default. We project to WORKING_SR_EPSG
# after writing to the gdb.
REST_OUT_SR: int = 4326


# ---------------------------------------------------------------------------
# REST query helpers
# ---------------------------------------------------------------------------
class RestQueryError(RuntimeError):
    """Raised when a REST query fails or returns malformed JSON."""


def query_paginated(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    geometry: dict | None = None,
    out_sr: int = REST_OUT_SR,
    page_size: int = 1000,
    max_retries: int = 6,
    backoff_s: float = 5.0,
    order_by: str = "OBJECTID",
) -> Iterator[dict]:
    """
    Yield Esri JSON feature dicts from a paginated REST `query` endpoint.

    Yields one feature dict at a time. Caller decides how to assemble them
    (write to JSON file, build FeatureSet, etc.). Pagination uses
    resultOffset / resultRecordCount; works against any service that supports
    pagination (most modern ArcGIS REST services do).

    Parameters
    ----------
    layer_url : str
        Full URL to the layer (e.g. .../MapServer/3 or .../FeatureServer/0).
    where : str
        SQL where clause. Default "1=1" pulls everything.
    out_fields : str
        Comma-separated field names, or "*" for all.
    geometry : dict or None
        Esri JSON envelope or polygon for spatial filter. None = no filter.
    out_sr : int
        Output spatial reference WKID.
    page_size : int
        Features per request. Most services cap at 1000-2000.
    max_retries : int
        Retries per page on transient errors.
    backoff_s : float
        Seconds between retries (linear backoff).
    """
    offset = 0
    while True:
        params: dict = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": out_sr,
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            # orderByFields makes pagination deterministic. Without it,
            # ArcGIS REST services often return HTTP 400 at large offsets.
            "orderByFields": order_by,
        }
        if geometry is not None:
            params["geometry"] = json.dumps(geometry)
            params["geometryType"] = "esriGeometryEnvelope"
            params["spatialRel"] = "esriSpatialRelIntersects"
            params["inSR"] = out_sr

        url = f"{layer_url}/query"
        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                # POST avoids URL-length limits when the where clause is long.
                # ArcGIS REST query supports both GET and POST identically.
                resp = requests.post(url, data=params, timeout=120)
                resp.raise_for_status()
                payload = resp.json()
                if "error" in payload:
                    raise RestQueryError(
                        f"REST error from {url}: {payload['error']}"
                    )
                features = payload.get("features", [])
                break
            except (requests.RequestException, ValueError, RestQueryError) as e:
                last_err = e
                if attempt == max_retries - 1:
                    raise RestQueryError(
                        f"Query failed after {max_retries} attempts: {e}\n"
                        f"URL: {url}\nParams: {urlencode(params)[:300]}"
                    ) from e
                time.sleep(backoff_s * (2 ** attempt))

        if not features:
            return
        for feat in features:
            yield feat

        if len(features) < page_size:
            return
        offset += page_size


def features_to_json_file(
    features: Iterable[dict],
    spatial_reference: int,
    geometry_type: str,
    fields: list[dict],
    out_path: str | Path,
) -> Path:
    """
    Write an iterable of Esri JSON features to a feature-set JSON file that
    `arcpy.conversion.JSONToFeatures` can consume.

    Parameters
    ----------
    features : iterable of dict
        Esri JSON features as returned by `query_paginated`.
    spatial_reference : int
        WKID for the output `spatialReference` block.
    geometry_type : str
        One of "esriGeometryPoint", "esriGeometryPolyline",
        "esriGeometryPolygon".
    fields : list of dict
        Esri-JSON field definitions (name, type, alias, etc.). Pull these
        from the layer's metadata (`<url>?f=json`) so types match.
    out_path : str or Path
        Where to write the JSON. Returns the resolved Path.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    feature_set = {
        "displayFieldName": "",
        "geometryType": geometry_type,
        "spatialReference": {"wkid": spatial_reference},
        "fields": fields,
        "features": list(features),
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(feature_set, fh)
    return out_path


def get_layer_metadata(layer_url: str) -> dict:
    """Fetch the layer's `?f=json` metadata block. Useful for pulling field
    definitions before exporting to a feature set."""
    resp = requests.get(layer_url, params={"f": "json"}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def query_by_ids(
    layer_url: str,
    where: str = "1=1",
    out_fields: str = "*",
    geometry: dict | None = None,
    out_sr: int = REST_OUT_SR,
    batch_size: int = 200,
    max_retries: int = 6,
    backoff_s: float = 5.0,
) -> Iterator[dict]:
    """
    Fetch features in independent batches by OBJECTID.

    Use this when offset-based pagination fails (large result sets cause
    server-side cursor instability or timeouts). Two-phase:

      1. Ask the service once for all matching OBJECTIDs (returnIdsOnly).
         This is a single small response regardless of result-set size.
      2. Pull features in chunks of `batch_size` by explicit ID list. Each
         chunk is stateless on the server side, so a failure in batch N
         doesn't cascade into batch N+1.

    Yields Esri-JSON feature dicts identical to query_paginated.
    """
    query_url = f"{layer_url}/query"

    # Phase 1: collect all matching OBJECTIDs.
    id_params: dict = {
        "where": where,
        "returnIdsOnly": "true",
        "f": "json",
    }
    if geometry is not None:
        id_params["geometry"] = json.dumps(geometry)
        id_params["geometryType"] = "esriGeometryEnvelope"
        id_params["spatialRel"] = "esriSpatialRelIntersects"
        id_params["inSR"] = out_sr

    payload = _post_with_retries(query_url, id_params, max_retries, backoff_s)
    ids = payload.get("objectIds") or []
    if not ids:
        return
    ids = sorted(int(x) for x in ids)

    # Phase 2: fetch features in independent batches.
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        feat_params: dict = {
            "objectIds": ",".join(str(x) for x in chunk),
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": out_sr,
            "f": "json",
        }
        payload = _post_with_retries(
            query_url, feat_params, max_retries, backoff_s
        )
        for feat in payload.get("features", []):
            yield feat


def _post_with_retries(
    url: str,
    params: dict,
    max_retries: int = 6,
    backoff_s: float = 5.0,
) -> dict:
    """POST with exponential-backoff retries. Returns parsed JSON or raises."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, data=params, timeout=120)
            resp.raise_for_status()
            payload = resp.json()
            if "error" in payload:
                raise RestQueryError(
                    f"REST error from {url}: {payload['error']}"
                )
            return payload
        except (requests.RequestException, ValueError, RestQueryError) as e:
            if attempt == max_retries - 1:
                raise RestQueryError(
                    f"Query failed after {max_retries} attempts: {e}"
                ) from e
            time.sleep(backoff_s * (2 ** attempt))
    # Unreachable, but keeps type-checkers happy.
    raise RestQueryError("retry loop exited without success")
