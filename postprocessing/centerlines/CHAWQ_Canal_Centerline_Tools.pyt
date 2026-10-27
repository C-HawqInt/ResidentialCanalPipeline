# -*- coding: utf-8 -*-
"""
C-HAWQ Canal Centerline Tools  (ArcGIS Pro Python toolbox)

Tools
  1. Polygons to Centerlines      - canal polygons -> centerline polylines
  2. Summarize Centerline Length  - total / grouped centerline length in km and miles
  3. Zonal Stats from County Rasters - mean / median / max / area of a distance raster
                                      per canal polygon, reading tiles from county folders

How the centerline is built
  Each polygon part (outer ring plus any island holes) has its boundary densified,
  and a Voronoi diagram of those boundary points is computed. The Voronoi edges that
  fall inside the polygon trace the medial axis (the line of points equidistant from
  both banks), which is the canal centerline. Short side branches that form at
  corners and canal ends are then pruned, the line is simplified, and the dead ends
  are optionally extended to the canal terminus.

Requirements
  ArcGIS Pro 2.9+ (arcpy, numpy, scipy and matplotlib all ship with Pro's default
  Python environment). Input polygons must be in a projected coordinate system
  (for example NAD 1983 (2011) UTM Zone 17N or a Florida State Plane zone).
"""

import math
import os

import numpy as np
import arcpy


# --------------------------------------------------------------------------
# Core geometry (pure numpy / scipy; no arcpy objects, so it can be unit-tested)
# --------------------------------------------------------------------------

def _signed_area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)


def _orient(ring, ccw):
    """Return ring wound counter-clockwise (ccw=True) or clockwise (ccw=False)."""
    if (_signed_area(ring) > 0) != ccw:
        return ring[::-1].copy()
    return ring


def _open_ring(ring):
    ring = np.asarray(ring, dtype=float)
    if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    return ring


def _densify_ring(ring, spacing):
    """Insert points along an open ring so no gap exceeds `spacing`."""
    nxt = np.roll(ring, -1, axis=0)
    seg = nxt - ring
    seglen = np.hypot(seg[:, 0], seg[:, 1])
    good = seglen > 1e-9
    ring, nxt, seg, seglen = ring[good], nxt[good], seg[good], seglen[good]
    k = np.maximum(1, np.ceil(seglen / spacing)).astype(int)
    idx = np.repeat(np.arange(len(ring)), k)
    start = np.cumsum(k) - k
    offs = np.arange(k.sum()) - np.repeat(start, k)
    t = offs / k[idx]
    return ring[idx] + seg[idx] * t[:, None]


def _make_inside_fn(rings):
    """Point-in-polygon test: inside the outer ring (first) and outside every hole (rest)."""
    from matplotlib.path import Path
    paths = [Path(np.vstack([_open_ring(r), _open_ring(r)[:1]])) for r in rings]

    def inside(pts):
        res = paths[0].contains_points(pts)
        for hole in paths[1:]:
            res &= ~hole.contains_points(pts)
        return res
    return inside


def _rdp(pts, tol):
    """Douglas-Peucker simplification of an Nx2 polyline (endpoints always kept)."""
    n = len(pts)
    if n < 3 or tol <= 0:
        return pts
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        p, q = pts[a], pts[b]
        d = q - p
        L = math.hypot(d[0], d[1])
        seg = pts[a + 1:b]
        if L < 1e-12:
            dist = np.hypot(seg[:, 0] - p[0], seg[:, 1] - p[1])
        else:
            dist = np.abs(d[0] * (seg[:, 1] - p[1]) - d[1] * (seg[:, 0] - p[0])) / L
        i = int(np.argmax(dist))
        if dist[i] > tol:
            m = a + 1 + i
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return pts[keep]


def _build_graph(vor_vertices, edges):
    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _edge_len(coords, a, b):
    d = coords[a] - coords[b]
    return math.hypot(d[0], d[1])


def _prune_spurs(adj, coords, radius, spur_factor, min_spur):
    """
    Remove short branches that end at a leaf and join a junction.
    A branch is a spur if its length < max(min_spur, spur_factor * radius at the junction).
    Candidates are judged against the same snapshot of the graph so that the two
    corner branches at a canal end are removed together.
    """
    while True:
        leaves = [n for n, nb in adj.items() if len(nb) == 1]
        candidates = []  # (nodes_to_remove, junction, length)
        for leaf in leaves:
            path = [leaf]
            prev, cur, length = None, leaf, 0.0
            junction = None
            while True:
                nbrs = [m for m in adj[cur] if m != prev]
                if not nbrs:
                    break
                nxt = nbrs[0]
                length += _edge_len(coords, cur, nxt)
                prev, cur = cur, nxt
                if len(adj[cur]) != 2:
                    if len(adj[cur]) >= 3:
                        junction = cur
                    break
                path.append(cur)
            if junction is None:
                continue
            if length < max(min_spur, spur_factor * radius[junction]):
                candidates.append((path, junction, length))
        if not candidates:
            return adj
        by_junction = {}
        for c in candidates:
            by_junction.setdefault(c[1], []).append(c)
        remove = []
        for j, cs in by_junction.items():
            if len(cs) >= len(adj[j]):          # every branch is short: keep the longest
                cs = sorted(cs, key=lambda c: c[2])[:-1]
            remove.extend(cs)
        if not remove:
            return adj
        for path, j, _ in remove:
            for n in path:
                for m in list(adj.get(n, ())):
                    adj[m].discard(n)
                adj.pop(n, None)
        for n in [n for n, nb in adj.items() if not nb]:
            adj.pop(n)


def _trace_chains(adj):
    """Split the graph into chains that run between junctions / endpoints (or closed loops)."""
    seen = set()
    chains = []

    def ekey(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start, nb):
        chain = [start]
        prev, cur = start, nb
        seen.add(ekey(start, nb))
        while True:
            chain.append(cur)
            if cur == start or len(adj[cur]) != 2:
                break
            nxt = [m for m in adj[cur] if m != prev][0]
            if ekey(cur, nxt) in seen:
                break
            seen.add(ekey(cur, nxt))
            prev, cur = cur, nxt
        return chain

    for s in [n for n, nb in adj.items() if len(nb) != 2]:
        for nb in adj[s]:
            if ekey(s, nb) not in seen:
                chains.append(walk(s, nb))
    for s in list(adj):
        for nb in adj[s]:
            if ekey(s, nb) not in seen:
                chains.append(walk(s, nb))
    return chains


def centerlines_for_part(rings, spacing, spur_factor=1.5, min_spur=0.0,
                         simplify_tol=0.0, extend_ends=True):
    """
    rings        : list of Nx2 arrays in the same units as `spacing`; rings[0] is the outer
                   ring, any others are island holes.
    spacing      : boundary densification spacing.
    spur_factor  : prune a leaf branch shorter than this multiple of the local half-width.
    min_spur     : prune a leaf branch shorter than this absolute length.
    simplify_tol : Douglas-Peucker tolerance (0 = off).
    extend_ends  : extend dead ends by their half-width so lines reach the canal terminus.
    Returns a list of Mx2 arrays (one per centerline segment), in the input coordinates.
    """
    from scipy.spatial import Voronoi, cKDTree

    rings = [_open_ring(r) for r in rings]
    origin = rings[0].min(axis=0)
    rings = [r - origin for r in rings]

    dens, rid, pos = [], [], []
    for i, ring in enumerate(rings):
        d = _densify_ring(_orient(ring, ccw=(i == 0)), spacing)
        dens.append(d)
        rid.append(np.full(len(d), i))
        pos.append(np.arange(len(d)))
    pts = np.vstack(dens)
    rid = np.concatenate(rid)
    pos = np.concatenate(pos)
    sizes = np.array([len(d) for d in dens])
    if len(pts) < 4:
        return []

    # tiny deterministic jitter avoids degenerate (co-circular) Voronoi vertices
    rng = np.random.RandomState(12345)
    jpts = pts + (rng.rand(*pts.shape) - 0.5) * spacing * 1e-3

    vor = Voronoi(jpts)
    rv = np.asarray(vor.ridge_vertices)
    rp = vor.ridge_points
    finite = (rv[:, 0] >= 0) & (rv[:, 1] >= 0)

    # drop ridges between neighbouring boundary points: they are perpendicular hairs
    same = rid[rp[:, 0]] == rid[rp[:, 1]]
    gap = np.abs(pos[rp[:, 0]] - pos[rp[:, 1]])
    gap = np.minimum(gap, sizes[rid[rp[:, 0]]] - gap)
    keep = finite & ~(same & (gap <= 1))

    inside = _make_inside_fn(rings)
    v_in = inside(vor.vertices)
    rv_k = rv[keep]
    ok = v_in[rv_k[:, 0]] & v_in[rv_k[:, 1]]
    rv_k = rv_k[ok]
    mids = 0.5 * (vor.vertices[rv_k[:, 0]] + vor.vertices[rv_k[:, 1]])
    rv_k = rv_k[inside(mids)]
    if len(rv_k) == 0:
        return []

    used = np.unique(rv_k)
    radius = np.zeros(len(vor.vertices))
    radius[used] = cKDTree(pts).query(vor.vertices[used])[0]
    coords = vor.vertices

    adj = _build_graph(coords, [(int(a), int(b)) for a, b in rv_k])
    adj = _prune_spurs(adj, coords, radius, spur_factor, min_spur)
    chains = _trace_chains(adj)

    out = []
    for chain in chains:
        line = coords[chain].copy()
        if extend_ends and len(adj) and chain[0] != chain[-1]:
            for end in (0, -1):
                node = chain[end]
                if len(adj[node]) != 1:
                    continue
                r = radius[node]
                # reference point at least ~2 spacings back along the line
                seq = line if end == -1 else line[::-1]
                tip = seq[-1]
                back = seq[0]
                for p in seq[::-1][1:]:
                    back = p
                    if math.hypot(*(tip - p)) >= 2 * spacing:
                        break
                d = tip - back
                dl = math.hypot(d[0], d[1])
                if dl < 1e-9 or r <= 0:
                    continue
                d = d / dl
                for frac in (1.0, 0.75, 0.5, 0.25):
                    cand = tip + d * r * frac
                    if inside(cand[None, :])[0]:
                        line = np.vstack([line, cand]) if end == -1 else np.vstack([cand, line])
                        break
        line = _rdp(line, simplify_tol)
        if len(line) >= 2:
            out.append(line + origin)
    return out


# --------------------------------------------------------------------------
# arcpy helpers
# --------------------------------------------------------------------------

def _part_to_rings(part):
    """arcpy Array (points, None between rings) -> list of Nx2 arrays."""
    rings, cur = [], []
    for p in part:
        if p is None:
            if len(cur) >= 3:
                rings.append(np.array(cur))
            cur = []
        else:
            cur.append((p.X, p.Y))
    if len(cur) >= 3:
        rings.append(np.array(cur))
    return rings


def _tool_params(*specs):
    params = []
    for (label, name, dtype, ptype, direction) in specs:
        params.append(arcpy.Parameter(displayName=label, name=name, datatype=dtype,
                                      parameterType=ptype, direction=direction))
    return params


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------

class Toolbox(object):
    def __init__(self):
        self.label = "C-HAWQ Canal Centerline Tools"
        self.alias = "chawq_centerline"
        self.tools = [PolygonsToCenterlines, SummarizeCenterlineLength,
                      ZonalStatsFromCountyRasters]


class PolygonsToCenterlines(object):
    def __init__(self):
        self.label = "Polygons to Centerlines"
        self.description = ("Creates a centerline for each canal polygon (the line equidistant "
                            "from both banks). Handles branching canals and islands.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = _tool_params(
            ("Input canal polygons", "in_features", "GPFeatureLayer", "Required", "Input"),
            ("Output centerlines", "out_features", "DEFeatureClass", "Required", "Output"),
            ("Boundary point spacing (meters)", "spacing_m", "GPDouble", "Required", "Input"),
            ("Spur pruning factor (x local half-width)", "spur_factor", "GPDouble", "Required", "Input"),
            ("Minimum spur length to keep (meters)", "min_spur_m", "GPDouble", "Required", "Input"),
            ("Simplify tolerance (meters)", "simplify_m", "GPDouble", "Required", "Input"),
            ("Extend dead ends to canal terminus", "extend_ends", "GPBoolean", "Optional", "Input"),
        )
        p[0].filter.list = ["Polygon"]
        p[2].value = 3.0
        p[3].value = 1.5
        p[4].value = 5.0
        p[5].value = 0.5
        p[6].value = True
        return p

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        p = parameters[0]
        if p.value and not p.hasBeenValidated:
            try:
                sr = arcpy.Describe(p.valueAsText).spatialReference
                if sr.type != "Projected":
                    p.setErrorMessage("Input must be in a projected coordinate system "
                                      "(for example UTM 17N or Florida State Plane). "
                                      "Run Project first.")
            except Exception:
                pass
        for i in (2, 3):
            if parameters[i].value is not None and parameters[i].value <= 0:
                parameters[i].setErrorMessage("Value must be greater than zero.")
        for i in (4, 5):
            if parameters[i].value is not None and parameters[i].value < 0:
                parameters[i].setErrorMessage("Value cannot be negative.")

    def execute(self, parameters, messages):
        in_fc = parameters[0].valueAsText
        out_fc = parameters[1].valueAsText
        spacing_m = float(parameters[2].value)
        spur_factor = float(parameters[3].value)
        min_spur_m = float(parameters[4].value)
        simplify_m = float(parameters[5].value)
        extend = bool(parameters[6].value)

        desc = arcpy.Describe(in_fc)
        sr = desc.spatialReference
        if sr.type != "Projected":
            raise arcpy.ExecuteError("Input must be in a projected coordinate system.")
        mpu = sr.metersPerUnit
        spacing = spacing_m / mpu
        min_spur = min_spur_m / mpu
        simplify = simplify_m / mpu

        out_dir, out_name = os.path.split(out_fc)
        arcpy.management.CreateFeatureclass(out_dir, out_name, "POLYLINE", spatial_reference=sr)
        arcpy.management.AddField(out_fc, "SRC_OID", "LONG")
        arcpy.management.AddField(out_fc, "LEN_M", "DOUBLE")
        arcpy.management.AddField(out_fc, "LEN_KM", "DOUBLE")
        arcpy.management.AddField(out_fc, "LEN_MI", "DOUBLE")

        total = int(arcpy.management.GetCount(in_fc)[0])
        arcpy.SetProgressor("step", "Building centerlines...", 0, max(total, 1), 1)

        n_poly = n_lines = n_fail = 0
        total_m = 0.0
        with arcpy.da.SearchCursor(in_fc, ["OID@", "SHAPE@"]) as sc, \
                arcpy.da.InsertCursor(out_fc, ["SHAPE@", "SRC_OID", "LEN_M", "LEN_KM", "LEN_MI"]) as ic:
            for oid, geom in sc:
                n_poly += 1
                arcpy.SetProgressorPosition()
                if geom is None:
                    continue
                for i in range(geom.partCount):
                    rings = _part_to_rings(geom.getPart(i))
                    if not rings:
                        continue
                    try:
                        lines = centerlines_for_part(rings, spacing, spur_factor, min_spur,
                                                     simplify, extend)
                    except Exception as e:  # keep going; report at the end
                        n_fail += 1
                        arcpy.AddWarning("OID {} part {}: {}".format(oid, i, e))
                        continue
                    for ln in lines:
                        arr = arcpy.Array([arcpy.Point(float(x), float(y)) for x, y in ln])
                        pl = arcpy.Polyline(arr, sr)
                        length_m = pl.length * mpu
                        total_m += length_m
                        ic.insertRow((pl, oid, length_m, length_m / 1000.0, length_m / 1609.344))
                        n_lines += 1
                if n_poly % 500 == 0:
                    arcpy.AddMessage("  {} of {} polygons processed".format(n_poly, total))

        arcpy.ResetProgressor()
        arcpy.AddMessage("Polygons processed: {}".format(n_poly))
        arcpy.AddMessage("Centerline segments written: {}".format(n_lines))
        arcpy.AddMessage("Total centerline length: {:,.2f} km ({:,.2f} miles)".format(
            total_m / 1000.0, total_m / 1609.344))
        if n_fail:
            arcpy.AddWarning("{} polygon part(s) failed; see warnings above.".format(n_fail))


class SummarizeCenterlineLength(object):
    def __init__(self):
        self.label = "Summarize Centerline Length"
        self.description = ("Totals centerline length in kilometers and miles, optionally "
                            "grouped by a field (for example city, county or canal system).")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = _tool_params(
            ("Input centerlines", "in_lines", "GPFeatureLayer", "Required", "Input"),
            ("Group by field", "group_field", "Field", "Optional", "Input"),
            ("Output summary table", "out_table", "DETable", "Required", "Output"),
        )
        p[0].filter.list = ["Polyline"]
        p[1].parameterDependencies = [p[0].name]
        return p

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        in_fc = parameters[0].valueAsText
        group = parameters[1].valueAsText
        out_tbl = parameters[2].valueAsText

        sr = arcpy.Describe(in_fc).spatialReference
        if sr.type != "Projected":
            raise arcpy.ExecuteError("Input must be in a projected coordinate system.")
        mpu = sr.metersPerUnit

        fields = ["SHAPE@LENGTH"] + ([group] if group else [])
        sums, counts = {}, {}
        with arcpy.da.SearchCursor(in_fc, fields) as sc:
            for row in sc:
                key = row[1] if group else "ALL"
                sums[key] = sums.get(key, 0.0) + row[0] * mpu
                counts[key] = counts.get(key, 0) + 1

        out_dir, out_name = os.path.split(out_tbl)
        arcpy.management.CreateTable(out_dir, out_name)
        arcpy.management.AddField(out_tbl, "GROUP_VAL", "TEXT", field_length=255)
        arcpy.management.AddField(out_tbl, "SEGMENTS", "LONG")
        arcpy.management.AddField(out_tbl, "LEN_KM", "DOUBLE")
        arcpy.management.AddField(out_tbl, "LEN_MI", "DOUBLE")
        with arcpy.da.InsertCursor(out_tbl, ["GROUP_VAL", "SEGMENTS", "LEN_KM", "LEN_MI"]) as ic:
            for key in sorted(sums, key=lambda k: str(k)):
                m = sums[key]
                ic.insertRow((str(key), counts[key], m / 1000.0, m / 1609.344))

        grand = sum(sums.values())
        arcpy.AddMessage("Total centerline length: {:,.2f} km ({:,.2f} miles) in {} segments".format(
            grand / 1000.0, grand / 1609.344, sum(counts.values())))


# --------------------------------------------------------------------------
# Zonal statistics from tiled county rasters
# --------------------------------------------------------------------------

RASTER_EXTS = (".tif", ".tiff", ".img", ".jp2", ".asc", ".crf")


def window_cell_values(inside, arr, x_left, y_top, cw, ch, tile_box=None):
    """
    Values of raster cells whose centers fall inside a polygon.
    arr            : 2-D array (NaN = NoData); row 0 is the top row.
    x_left, y_top  : coordinates of the window's upper-left corner.
    cw, ch         : cell width / height.
    inside         : function(Nx2 points) -> bool array (polygon test).
    tile_box       : (xmin, ymin, xmax, ymax) of the tile; centers are kept only on the
                     half-open interval [min, max) so abutting tiles never double count.
    Returns a 1-D float array of the cell values.
    """
    nrows, ncols = arr.shape
    xs = x_left + (np.arange(ncols) + 0.5) * cw
    ys = y_top - (np.arange(nrows) + 0.5) * ch
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    keep = np.isfinite(arr.ravel())
    if tile_box is not None:
        xmin, ymin, xmax, ymax = tile_box
        keep &= (pts[:, 0] >= xmin) & (pts[:, 0] < xmax) & (pts[:, 1] > ymin) & (pts[:, 1] <= ymax)
    if not keep.any():
        return np.empty(0)
    idx = np.where(keep)[0]
    ok = inside(pts[idx])
    return arr.ravel()[idx[ok]]


def summarize_values(values, cell_area):
    """Mean / median / max and covered area for a 1-D array of cell values."""
    if len(values) == 0:
        return None
    return {"mean": float(np.mean(values)), "median": float(np.median(values)),
            "max": float(np.max(values)), "n": int(len(values)),
            "area": float(len(values) * cell_area)}


def _window_indices(rx0, ry1, cw, ch, width, height, xmin, ymin, xmax, ymax):
    """Column / row index range of a raster that covers a bounding box (clipped to raster)."""
    c0 = max(0, int(math.floor((xmin - rx0) / cw)))
    c1 = min(width, int(math.ceil((xmax - rx0) / cw)))
    r0 = max(0, int(math.floor((ry1 - ymax) / ch)))
    r1 = min(height, int(math.ceil((ry1 - ymin) / ch)))
    return c0, c1, r0, r1


class _TileIndex(object):
    """Catalog of every raster under a root folder, tagged with its county folder."""

    def __init__(self):
        self.items = []

    def scan(self, root, name_contains=None, log=None):
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(RASTER_EXTS):
                    continue
                if name_contains and name_contains.lower() not in fn.lower():
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    r = arcpy.Raster(path)
                    ext = r.extent
                    rel = os.path.relpath(dirpath, root)
                    county = rel.split(os.sep)[0] if rel != "." else ""
                    self.items.append({
                        "path": path, "county": county.lower(),
                        "box": (ext.XMin, ext.YMin, ext.XMax, ext.YMax),
                        "sr": ext.spatialReference, "cw": r.meanCellWidth,
                        "ch": r.meanCellHeight, "width": r.width, "height": r.height,
                        "band_count": r.bandCount})
                except Exception as e:
                    if log:
                        log("Skipped {}: {}".format(path, e))
        return len(self.items)


class ZonalStatsFromCountyRasters(object):
    def __init__(self):
        self.label = "Zonal Stats from County Rasters"
        self.description = ("Reads distance-raster tiles stored in county folders and writes "
                            "mean, median, maximum and area for each canal polygon.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        p = _tool_params(
            ("Input canal polygons", "in_features", "GPFeatureLayer", "Required", "Input"),
            ("Folder of county raster folders", "root", "DEFolder", "Required", "Input"),
            ("Output polygons", "out_features", "DEFeatureClass", "Required", "Output"),
            ("Field prefix (5 characters or fewer)", "prefix", "GPString", "Required", "Input"),
            ("Polygon county field (optional)", "county_field", "Field", "Optional", "Input"),
            ("Only use rasters whose name contains (optional)", "name_filter", "GPString", "Optional", "Input"),
        )
        p[0].filter.list = ["Polygon"]
        p[3].value = "DIST_"
        p[4].parameterDependencies = [p[0].name]
        return p

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        pre = parameters[3].valueAsText
        if pre and len(pre) > 5:
            parameters[3].setErrorMessage("Use 5 characters or fewer so field names fit "
                                          "the 10-character shapefile limit.")

    @staticmethod
    def _norm(s):
        return "".join(ch for ch in str(s).lower() if ch.isalnum())

    def execute(self, parameters, messages):
        in_fc = parameters[0].valueAsText
        root = parameters[1].valueAsText
        out_fc = parameters[2].valueAsText
        prefix = parameters[3].valueAsText
        county_field = parameters[4].valueAsText
        name_filter = parameters[5].valueAsText

        sr = arcpy.Describe(in_fc).spatialReference
        if sr.type != "Projected":
            raise arcpy.ExecuteError("Input polygons must be in a projected coordinate system.")
        mpu = sr.metersPerUnit

        arcpy.AddMessage("Scanning rasters under {} ...".format(root))
        index = _TileIndex()
        n = index.scan(root, name_filter, log=arcpy.AddWarning)
        if n == 0:
            raise arcpy.ExecuteError("No rasters found. Check the folder and the name filter.")
        counties = sorted(set(t["county"] for t in index.items))
        arcpy.AddMessage("Found {} raster tiles in {} folder(s).".format(n, len(counties)))
        norm_counties = {self._norm(c): c for c in counties}

        arcpy.management.CopyFeatures(in_fc, out_fc)
        f_mean, f_med, f_max, f_cov = (prefix + s for s in ("MEAN", "MED", "MAX", "COVR"))
        existing = [f.name.upper() for f in arcpy.ListFields(out_fc)]
        for name in (f_mean, f_med, f_max, f_cov):
            if name.upper() in existing:
                raise arcpy.ExecuteError("Field {} already exists. Use a different prefix.".format(name))
            arcpy.management.AddField(out_fc, name, "DOUBLE")
        if "AREA_M2" not in existing:
            arcpy.management.AddField(out_fc, "AREA_M2", "DOUBLE")
        arcpy.management.AddField(out_fc, prefix + "TILES", "SHORT")

        fields = ["OID@", "SHAPE@", f_mean, f_med, f_max, f_cov, "AREA_M2", prefix + "TILES"]
        if county_field:
            fields.append(county_field)

        total = int(arcpy.management.GetCount(out_fc)[0])
        arcpy.SetProgressor("step", "Computing zonal statistics...", 0, max(total, 1), 1)
        n_done = n_none = n_nofolder = 0

        with arcpy.da.UpdateCursor(out_fc, fields) as uc:
            for row in uc:
                arcpy.SetProgressorPosition()
                geom = row[1]
                area_m2 = geom.area * mpu * mpu if geom is not None else None
                row[6] = area_m2
                row[7] = 0
                tiles = index.items
                if county_field:
                    key = self._norm(row[8]) if row[8] is not None else ""
                    if key in norm_counties:
                        tiles = [t for t in index.items if t["county"] == norm_counties[key]]
                    else:
                        n_nofolder += 1   # no matching folder: fall back to all tiles by location
                values, cell_area_sum, used = [], 0.0, 0
                proj_geoms = {}
                for t in tiles:
                    tsr = t["sr"]
                    if tsr.factoryCode == sr.factoryCode and tsr.factoryCode:
                        g = geom
                    else:
                        if tsr.name not in proj_geoms:
                            proj_geoms[tsr.name] = geom.projectAs(tsr)
                        g = proj_geoms[tsr.name]
                    e = g.extent
                    bx = t["box"]
                    if e.XMax < bx[0] or e.XMin > bx[2] or e.YMax < bx[1] or e.YMin > bx[3]:
                        continue
                    rx0, ry1 = bx[0], bx[3]
                    c0, c1, r0, r1 = _window_indices(rx0, ry1, t["cw"], t["ch"], t["width"],
                                                     t["height"], e.XMin, e.YMin, e.XMax, e.YMax)
                    if c1 <= c0 or r1 <= r0:
                        continue
                    ll = arcpy.Point(rx0 + c0 * t["cw"], ry1 - r1 * t["ch"])
                    arr = arcpy.RasterToNumPyArray(t["path"], ll, c1 - c0, r1 - r0,
                                                   nodata_to_value=np.nan)
                    if arr.ndim == 3:
                        arr = arr[0]
                    arr = arr.astype(float)
                    inside_fns = [_make_inside_fn(r) for r in
                                  (_part_to_rings(g.getPart(i)) for i in range(g.partCount)) if r]
                    vals = np.empty(0)
                    for inside in inside_fns:
                        v = window_cell_values(inside, arr, rx0 + c0 * t["cw"], ry1 - r0 * t["ch"],
                                               t["cw"], t["ch"], tile_box=bx)
                        vals = np.concatenate([vals, v])
                    if len(vals):
                        values.append(vals)
                        # covered area in square meters
                        cell_area_sum += len(vals) * t["cw"] * t["ch"] * (tsr.metersPerUnit or 1.0) ** 2
                        used += 1
                if values:
                    allv = np.concatenate(values)
                    row[2], row[3], row[4] = float(np.mean(allv)), float(np.median(allv)), float(np.max(allv))
                    row[5] = (min(100.0, 100.0 * cell_area_sum / area_m2) if area_m2 else None)
                    row[7] = used
                    n_done += 1
                else:
                    n_none += 1
                uc.updateRow(row)

        arcpy.ResetProgressor()
        arcpy.AddMessage("Polygons with statistics: {}".format(n_done))
        if n_none:
            arcpy.AddWarning("{} polygon(s) had no raster cells (no overlapping tile or all NoData).".format(n_none))
        if n_nofolder:
            arcpy.AddWarning("{} polygon(s) had a county value with no matching folder; "
                             "they were matched by location instead.".format(n_nofolder))
        arcpy.AddMessage("Statistics are in the raster's own units. {}COVR is the percent of the "
                         "polygon covered by valid raster cells.".format(prefix))
