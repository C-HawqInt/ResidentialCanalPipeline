# =============================================================================
# C-HAWQ | NCEAS Working Group | Residential canal spatial analysis
# =============================================================================
# Version 2 (2026-10-27). Reproduces every canal total for the manuscript from the
# FINAL layers (ResCanal_*_10212026_WMDs, 3,129 systems) and adds the distance-
# from-mouth and canal-form analyses. The original script (10/13 layers) is kept
# unchanged as chawq_residential_canal_analysis.R.
#
# Inputs (no zips needed; read straight from GIS Items):
#   Canal_Polygons/ResCanal_Polygons_10212026_WMDs.shp      (3,129 polygons)
#   Centerlines/ResCanal_Centerlines_10212026_WMDs.shp      (15,775 segments)
#   Distance_Polygons/DistanceStats_Final_by_CHAWQ_ID_20261026.csv
#     (per-polygon zonal statistics of in-water distance from the mouth)
#
# Analyses (numbers match the section headers below)
#   1. Polygon perimeter (shoreline), miles and km, and the shared-edge check
#   2. Total navigable waterway length from the centerlines
#   3. Shoreline and navigable length by Water Management District (WMDNAME)
#   4. Shoreline by region: Atlantic, Gulf, Florida Keys, Inland freshwater
#   5. County summaries: centerline length, shoreline, and canal area
#   6. Comparison with NOAA natural (tidal) shoreline
#   7. Canal size by Water Management District
#   8. Distance from the mouth by Water Management District        (new)
#   9. Size, shoreline and distance by canal form (TYP_FORM)        (new)
#
# HOW TO RUN (RStudio)
#   1. install.packages(c("sf", "dplyr", "tidyr", "readr", "stringr"))   # once
#      (optional: "writexl" for one Excel workbook, "lwgeom" for a geodesic check)
#   2. Check `ms_dir` below, then source("chawq_residential_canal_analysis_v2.R")
#   Results go to Statistical Analysis/canal_analysis_outputs_v2.
# =============================================================================
suppressPackageStartupMessages({
  library(sf)
  library(dplyr)
  library(tidyr)
  library(readr)
  library(stringr)
})

# ---- 0. Settings you may want to edit ---------------------------------------
# Manuscript folder (the E: drive letter can change; edit here if it does)
ms_dir   <- Sys.getenv("CHAWQ_MS_DIR", "E:/CHAWQ/ARW Manuscript")
data_dir <- file.path(ms_dir, "GIS Items")
shp_polygons    <- file.path(data_dir, "Canal_Polygons", "ResCanal_Polygons_10212026_WMDs.shp")
shp_centerlines <- file.path(data_dir, "Centerlines", "ResCanal_Centerlines_10212026_WMDs.shp")
csv_distance    <- file.path(data_dir, "Distance_Polygons", "DistanceStats_Final_by_CHAWQ_ID_20261026.csv")
out_dir <- file.path(ms_dir, "Statistical Analysis", "canal_analysis_outputs_v2")

# NOAA natural (tidal) shoreline, statute miles, as supplied by C-HAWQ
noaa_mi <- c(Florida = 8436, Atlantic = 3341, Gulf = 5095)

# Unit conversions
M_PER_MI    <- 1609.344
M2_PER_ACRE <- 4046.8564224

# Region rules (edit here, then re-run; nothing else changes).
# Rule order: Keys > Inland freshwater > Atlantic or Gulf by assessment group.
keys_group <- "Florida Keys"          # GROUP_NAME for the Keys (all Monroe County)

# Groups whose basins drain to Lake Okeechobee or the interior with no tidal reach
inland_groups <- c("Kissimmee River", "Lake Okeechobee", "Fisheating Creek",
                   "Ocklawaha", "Upper St. Johns")

# Counties with no Gulf, Atlantic, or Florida Bay shoreline
# (Clay and Putnam are left out on purpose: the tidal St. Johns River borders them)
no_coast_counties <- c("Alachua", "Bradford", "DeSoto", "Glades", "Hendry",
                       "Highlands", "Lake", "Marion", "Okeechobee", "Orange",
                       "Osceola", "Polk", "Seminole", "Sumter", "Union",
                       "Madison", "Gilchrist")

# Coast for every remaining group
atlantic_groups <- c("Indian River Lagoon", "Lake Worth Lagoon - Palm Beach Coast",
                     "Lower St. Johns", "Middle St. Johns",
                     "Southeast Coast - Biscayne Bay", "St. Lucie - Loxahatchee",
                     "Upper East Coast")
gulf_groups <- c("Apalachicola - Chipola", "Caloosahatchee", "Charlotte Harbor",
                 "Choctawhatchee - St. Andrew", "Everglades West Coast",
                 "Ochlockonee - St. Marks", "Pensacola", "Perdido",
                 "Sarasota Bay - Peace - Myakka", "Springs Coast", "Suwannee",
                 "Tampa Bay", "Tampa Bay Tributaries", "Withlacoochee")

# Receiving waters that are open to tides or salt water (used for the
# conservative NOAA comparison in section 6)
marine_receiving <- c("Bay/Estuary", "Coastal lagoon", "Gulf or Open Ocean")

# Optional spot check against a known place (set to NULL to skip)
spot_check_name <- "MARCO ISLAND"     # matches WATERBODY_ that starts with this text

dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
sf_use_s2(FALSE)   # all measurements use the projected (Albers, meters) data

# ---- Helpers ----------------------------------------------------------------
read_shp <- function(path) { stopifnot(file.exists(path)); st_read(path, quiet = TRUE) }

# Perimeter of every polygon ring, outer edge plus island (hole) edges, in meters
perim_m <- function(x) as.numeric(st_length(st_boundary(st_geometry(x))))

# Perimeter of the dissolved footprint: shared edges between touching polygons cancel
dissolved_perim_m <- function(x) {
  u <- st_union(st_make_valid(st_geometry(x)))
  sum(as.numeric(st_length(st_boundary(u))))
}

to_mi <- function(m) m / M_PER_MI
to_km <- function(m) m / 1000
save_csv <- function(df, name) write_csv(df, file.path(out_dir, name))
show <- function(df, title) {
  cat("\n", strrep("=", 78), "\n", title, "\n", strrep("=", 78), "\n", sep = "")
  print(as.data.frame(df), row.names = FALSE)
}
results <- list()   # collected for the optional Excel workbook

# ---- Read data and basic quality checks --------------------------------------
poly <- read_shp(shp_polygons)
cl   <- read_shp(shp_centerlines)
dist <- read_csv(csv_distance, show_col_types = FALSE) %>%
  select(CHAWQ_ID, DIST_MEAN, DIST_MED, DIST_MAX, DIST_COVR, DIST_TILES)

# Canal form labels: one polygon is spelled "Single Channel"; use one spelling
poly$TYP_FORM <- dplyr::recode(poly$TYP_FORM, "Single Channel" = "Single channel")

# Attach distance-from-mouth statistics (one row per CHAWQ_ID)
stopifnot(!anyDuplicated(poly$CHAWQ_ID), !anyDuplicated(dist$CHAWQ_ID))
poly <- poly %>% left_join(dist, by = "CHAWQ_ID")

stopifnot(identical(st_crs(poly), st_crs(cl)))
stopifnot(identical(st_crs(poly)$units_gdal, "metre"))
cat("CRS:", st_crs(poly)$input, "\n")
cat("Polygons:", nrow(poly), "| Centerlines:", nrow(cl), "\n")

poly$poly_row <- seq_len(nrow(poly))
cl$line_id    <- seq_len(nrow(cl))

qa <- tibble(
  check = c("Polygon geometries valid", "Polygon geometries empty",
            "Centerline geometries valid", "Centerline geometries empty",
            "Centerline segments shorter than 1 m (digitizing slivers)",
            "Polygons with no County / GROUP_NAME (unattributed)",
            "Polygons with no TYP_RECWAT (receiving water)",
            "Polygons with no TYP_FORM (canal form)",
            "Duplicate CHAWQ_IDs",
            "Polygons with no distance statistics",
            "Polygons with distance coverage below 95 percent"),
  value = c(sum(st_is_valid(poly)), sum(st_is_empty(poly)),
            sum(st_is_valid(cl)), sum(st_is_empty(cl)),
            sum(as.numeric(st_length(cl)) < 1),
            sum(is.na(poly$County)), sum(is.na(poly$TYP_RECWAT)),
            sum(is.na(poly$TYP_FORM)), sum(duplicated(poly$CHAWQ_ID)),
            sum(is.na(poly$DIST_MEAN)), sum(poly$DIST_COVR < 95, na.rm = TRUE))
)

# ---- 1. Confirm the finalized polygon perimeter -----------------------------
poly$peri_m_calc  <- perim_m(poly)
poly$peri_km_calc <- to_km(poly$peri_m_calc)
poly$peri_mi_calc <- to_mi(poly$peri_m_calc)
poly$area_m2_calc <- as.numeric(st_area(poly))

# Island (hole) edges are real shoreline, so they stay in the perimeter.
# Split the total into outer edge and island edge for the record.
ring_len <- function(r) sum(sqrt(rowSums(diff(r)^2)))
outer_m <- sum(vapply(st_geometry(poly), function(g) {
  if (inherits(g, "MULTIPOLYGON")) sum(vapply(g, function(p) ring_len(p[[1]]), numeric(1)))
  else ring_len(g[[1]])
}, numeric(1)))

perim_check <- tibble(
  measure = c("Stored peri_km (sum)", "Recalculated from geometry, km (sum)",
              "Stored peri_miles (sum)", "Recalculated from geometry, miles (sum)",
              "Largest per-polygon difference, km", "Largest per-polygon difference, miles",
              "Share of polygons where stored = recalculated (within 0.001 km)"),
  value = c(sum(poly$peri_km), sum(poly$peri_km_calc),
            sum(poly$peri_miles), sum(poly$peri_mi_calc),
            max(abs(poly$peri_km - poly$peri_km_calc)),
            max(abs(poly$peri_miles - poly$peri_mi_calc)),
            mean(abs(poly$peri_km - poly$peri_km_calc) < 0.001))
)

# shared edges: where split polygons touch, the sum of perimeters counts the cut twice
dissolved_m <- dissolved_perim_m(poly)
shared_edge <- tibble(
  measure = c("Sum of polygon perimeters, miles", "Dissolved footprint perimeter, miles",
              "Shared-edge difference, miles", "Shared-edge difference, percent",
              "Sum of polygon perimeters, km", "Dissolved footprint perimeter, km",
              "Shared-edge difference, km"),
  value = c(to_mi(sum(poly$peri_m_calc)), to_mi(dissolved_m),
            to_mi(sum(poly$peri_m_calc) - dissolved_m),
            100 * (sum(poly$peri_m_calc) - dissolved_m) / sum(poly$peri_m_calc),
            to_km(sum(poly$peri_m_calc)), to_km(dissolved_m),
            to_km(sum(poly$peri_m_calc) - dissolved_m))
)

# optional check against ellipsoidal (true-surface) length; needs the lwgeom package
geodesic_check <- NULL
if (requireNamespace("lwgeom", quietly = TRUE)) {
  g_m <- sum(as.numeric(lwgeom::st_geod_length(
    st_transform(st_boundary(st_geometry(poly)), 4269))))
  geodesic_check <- tibble(
    measure = c("Planar Albers perimeter, km", "Ellipsoidal perimeter, km", "Ratio"),
    value = c(to_km(sum(poly$peri_m_calc)), to_km(g_m), g_m / sum(poly$peri_m_calc)))
}

peri_final <- tibble(
  Peri_Miles = sum(poly$peri_mi_calc),
  Peri_Km    = sum(poly$peri_km_calc),
  Peri_Miles_dissolved = to_mi(dissolved_m),
  Peri_Km_dissolved    = to_km(dissolved_m),
  Outer_edge_km  = to_km(outer_m),
  Island_edge_km = to_km(sum(poly$peri_m_calc) - outer_m)
)

show(qa, "QA: geometry and attribute checks")
show(perim_check, "1a. Stored perimeter fields vs. geometry")
show(shared_edge, "1b. Shared-edge (split polygon) double count")
show(peri_final, "1c. FINAL polygon shoreline (Peri_Miles / Peri_Km)")
if (!is.null(geodesic_check)) show(geodesic_check, "1d. Planar vs. ellipsoidal length")
cat("\nNote: Shape_Area, Shape_Leng and Shape_Le_1 are stale ArcGIS fields and are not used.\n")

results$QA <- qa; results$`1_perimeter_check` <- perim_check
results$`1_shared_edge` <- shared_edge; results$`1_final_perimeter` <- peri_final

# ---- 2. Navigable waterway length from the centerlines ----------------------
cl$len_m_calc <- as.numeric(st_length(cl))
overlap_m <- sum(as.numeric(st_length(st_union(st_geometry(cl)))))

nav_total <- tibble(
  measure = c("Stored LEN_KM (sum)", "Recalculated from geometry, km",
              "Stored LEN_MI (sum)", "Recalculated from geometry, miles",
              "Largest per-segment difference, meters",
              "Length after dissolving overlaps, km (equals total if no double-drawn lines)"),
  value = c(sum(cl$LEN_KM), to_km(sum(cl$len_m_calc)),
            sum(cl$LEN_MI), to_mi(sum(cl$len_m_calc)),
            max(abs(cl$LEN_M - cl$len_m_calc)), to_km(overlap_m))
)
nav_final <- tibble(Navigable_Km = to_km(sum(cl$len_m_calc)),
                    Navigable_Miles = to_mi(sum(cl$len_m_calc)),
                    Segments = nrow(cl))
show(nav_total, "2a. Centerline length checks")
show(nav_final, "2b. FINAL navigable waterway length")
results$`2_nav_checks` <- nav_total; results$`2_nav_final` <- nav_final

# ---- Attribute clean-up shared by sections 3 to 7 ----------------------------
# Four polygons (7 miles of shoreline) carry no County/GROUP_NAME. They take the
# attributes of the nearest attributed polygon, and are flagged in `attr_source`.
poly$attr_source <- "polygon attributes"
missing_attr <- is.na(poly$County) | is.na(poly$GROUP_NAME)
if (any(missing_attr)) {
  donors <- poly[!missing_attr, ]
  nn <- st_nearest_feature(poly[missing_attr, ], donors)
  d  <- as.numeric(st_distance(poly[missing_attr, ], donors[nn, ], by_element = TRUE))
  poly$County[missing_attr]     <- donors$County[nn]
  poly$GROUP_NAME[missing_attr] <- donors$GROUP_NAME[nn]
  poly$attr_source[missing_attr] <- "nearest polygon"
  cat("\nFilled County/GROUP_NAME for", sum(missing_attr),
      "unattributed polygons from the nearest polygon; distances (m):",
      paste(round(d, 1), collapse = ", "), "\n")
}
poly$TYP_RECWAT <- ifelse(is.na(poly$TYP_RECWAT), "Unknown", poly$TYP_RECWAT)

# Region assignment (section 4 rules)
poly <- poly %>%
  mutate(
    Region = case_when(
      GROUP_NAME == keys_group ~ "Florida Keys",
      TYP_RECWAT == "Lake/Reservoir" | County %in% no_coast_counties |
        GROUP_NAME %in% inland_groups ~ "Inland freshwater",
      GROUP_NAME %in% atlantic_groups ~ "Atlantic",
      GROUP_NAME %in% gulf_groups ~ "Gulf",
      TRUE ~ "Unassigned"),
    marine_connected = TYP_RECWAT %in% marine_receiving
  )
stopifnot(!any(poly$Region == "Unassigned"))
# The Keys rule and Monroe County must describe the same polygons
stopifnot(all(poly$County[poly$Region == "Florida Keys"] == "Monroe"),
          all(poly$Region[poly$County == "Monroe"] == "Florida Keys"))

# Link each centerline to its source polygon. SRC_OID is the 0-based feature ID of
# the polygon shapefile the centerlines were built from, so row = SRC_OID + 1.
# The final centerline layer has no WMDNAME field; district comes from the polygon.
stopifnot("SRC_OID" %in% names(cl), max(cl$SRC_OID) < nrow(poly))
cl$poly_row <- cl$SRC_OID + 1L
mid <- st_cast(st_line_sample(st_geometry(cl), sample = 0.5), "POINT")
hits   <- st_intersects(mid, st_buffer(st_geometry(poly), 0.01))
in_src <- mapply(function(h, r) r %in% h, hits, cl$poly_row)
cat("Centerline midpoints inside their SRC_OID polygon:", sum(in_src), "of", nrow(cl), "\n")
cl <- cl %>%
  left_join(poly %>% st_drop_geometry() %>%
              select(poly_row, CHAWQ_ID, County, GROUP_NAME, Region, TYP_RECWAT, TYP_FORM,
                     marine_connected, WMDNAME),
            by = "poly_row")
stopifnot(!any(is.na(cl$WMDNAME)))

poly_df <- st_drop_geometry(poly)
cl_df   <- st_drop_geometry(cl)

# ---- 3. Totals by Water Management District ---------------------------------
wmd_shore <- poly_df %>%
  group_by(WMDNAME) %>%
  summarise(n_polygons = n(),
            shoreline_mi = sum(peri_mi_calc), shoreline_km = sum(peri_km_calc),
            .groups = "drop")
wmd_nav <- cl_df %>%
  group_by(WMDNAME) %>%
  summarise(n_centerline_segments = n(),
            navigable_mi = to_mi(sum(len_m_calc)), navigable_km = to_km(sum(len_m_calc)),
            .groups = "drop")
by_wmd <- full_join(wmd_shore, wmd_nav, by = "WMDNAME") %>%
  mutate(shoreline_per_navigable_mile = shoreline_mi / navigable_mi) %>%
  arrange(desc(shoreline_mi))
by_wmd <- bind_rows(by_wmd, by_wmd %>% summarise(
  WMDNAME = "ALL DISTRICTS",
  across(c(n_polygons, shoreline_mi, shoreline_km, n_centerline_segments,
           navigable_mi, navigable_km), sum),
  shoreline_per_navigable_mile = sum(shoreline_mi) / sum(navigable_mi)))
show(mutate(by_wmd, across(where(is.numeric), ~ round(.x, 2))),
     "3. Canal shoreline and navigable centerline by Water Management District")
results$`3_by_WMD` <- by_wmd

# ---- 4. Shoreline by region -------------------------------------------------
region_levels <- c("Atlantic", "Gulf", "Florida Keys", "Inland freshwater")
reg_shore <- poly_df %>%
  group_by(Region) %>%
  summarise(n_polygons = n(),
            shoreline_mi = sum(peri_mi_calc), shoreline_km = sum(peri_km_calc),
            area_acres = sum(area_m2_calc) / M2_PER_ACRE, area_km2 = sum(area_m2_calc) / 1e6,
            shoreline_marine_connected_mi = sum(peri_mi_calc[marine_connected]),
            .groups = "drop")
reg_nav <- cl_df %>%
  group_by(Region) %>%
  summarise(navigable_mi = to_mi(sum(len_m_calc)), navigable_km = to_km(sum(len_m_calc)),
            .groups = "drop")
by_region <- left_join(reg_shore, reg_nav, by = "Region") %>%
  mutate(Region = factor(Region, levels = region_levels)) %>%
  arrange(Region) %>% mutate(Region = as.character(Region))
by_region <- bind_rows(
  by_region,
  by_region %>% filter(Region != "Inland freshwater") %>%
    summarise(Region = "All coastal (Atlantic + Gulf + Keys)", across(where(is.numeric), sum)),
  by_region %>% summarise(Region = "All canals", across(where(is.numeric), sum)))
by_region <- by_region %>% mutate(pct_of_all_shoreline = 100 * shoreline_mi / sum(shoreline_mi[Region %in% region_levels]))
show(mutate(by_region, across(where(is.numeric), ~ round(.x, 2))),
     "4a. Canal shoreline by region")

region_x_receiving <- poly_df %>%
  group_by(Region, TYP_RECWAT) %>%
  summarise(n_polygons = n(), shoreline_mi = sum(peri_mi_calc), .groups = "drop") %>%
  arrange(factor(Region, levels = region_levels), desc(shoreline_mi))
region_x_group <- poly_df %>%
  group_by(Region, GROUP_NAME) %>%
  summarise(n_polygons = n(), shoreline_mi = sum(peri_mi_calc), .groups = "drop") %>%
  arrange(factor(Region, levels = region_levels), desc(shoreline_mi))
show(mutate(region_x_receiving, shoreline_mi = round(shoreline_mi, 1)),
     "4b. Region by receiving-water type (audit table)")
results$`4_by_region` <- by_region
results$`4_region_x_receiving` <- region_x_receiving
results$`4_region_x_group` <- region_x_group

# ---- 5. County summaries -----------------------------------------------------
cty_poly <- poly_df %>%
  group_by(County) %>%
  summarise(n_polygons = n(),
            shoreline_mi = sum(peri_mi_calc), shoreline_km = sum(peri_km_calc),
            area_acres = sum(area_m2_calc) / M2_PER_ACRE, area_km2 = sum(area_m2_calc) / 1e6,
            Region = Region[which.max(table(Region)[Region])],  # most common region
            .groups = "drop")
cty_nav <- cl_df %>%
  group_by(County) %>%
  summarise(n_centerline_segments = n(),
            navigable_mi = to_mi(sum(len_m_calc)), navigable_km = to_km(sum(len_m_calc)),
            .groups = "drop")
by_county <- full_join(cty_poly, cty_nav, by = "County") %>%
  select(County, Region, n_polygons, area_acres, area_km2, shoreline_mi, shoreline_km,
         n_centerline_segments, navigable_mi, navigable_km) %>%
  arrange(desc(shoreline_mi))
by_county <- bind_rows(by_county, by_county %>% summarise(
  County = "ALL COUNTIES", Region = "",
  across(where(is.numeric), sum)))
show(mutate(by_county, across(where(is.numeric), ~ round(.x, 2))),
     "5. County summaries (centerline, shoreline, area)")
results$`5_by_county` <- by_county

# ---- 6. Comparison with NOAA natural shoreline -------------------------------
shore_of <- function(region, marine_only = FALSE) {
  d <- poly_df %>% filter(Region %in% region)
  if (marine_only) d <- d %>% filter(marine_connected)
  sum(d$peri_mi_calc)
}
noaa_tbl <- function(marine_only, label) {
  atl <- shore_of("Atlantic", marine_only); gulf <- shore_of("Gulf", marine_only)
  keys <- shore_of("Florida Keys", marine_only)
  tibble(
    scenario = label,
    comparison = c("Atlantic coast (mainland canals only)",
                   "Gulf coast (mainland canals only)",
                   "Atlantic coast + all Keys canals (upper bound)",
                   "Gulf coast + all Keys canals (upper bound)",
                   "Florida statewide (Atlantic + Gulf + Keys)"),
    canal_shoreline_mi = c(atl, gulf, atl + keys, gulf + keys, atl + gulf + keys),
    noaa_natural_shoreline_mi = unname(noaa_mi[c("Atlantic", "Gulf", "Atlantic", "Gulf", "Florida")])
  ) %>% mutate(canal_as_pct_of_natural = 100 * canal_shoreline_mi / noaa_natural_shoreline_mi,
               canal_to_natural_ratio = canal_shoreline_mi / noaa_natural_shoreline_mi)
}
# Scenario A is the main comparison. Scenario B is a floor: it drops every polygon whose
# receiving water is a river, creek, or managed canal, which also removes tidal canal
# systems such as Cape Coral and Rotonda (Lee and Charlotte counties).
noaa_cmp <- bind_rows(
  noaa_tbl(FALSE, "A. All coastal canal shoreline (excludes inland freshwater)"),
  noaa_tbl(TRUE,  "B. Floor: bay, estuary, lagoon, or open-water receiving only"))
show(mutate(noaa_cmp, across(where(is.numeric), ~ round(.x, 2))),
     "6. Canal shoreline vs. NOAA natural shoreline (statute miles)")
cat("\nNOAA check: Atlantic + Gulf =", noaa_mi["Atlantic"] + noaa_mi["Gulf"],
    "vs. statewide figure supplied =", noaa_mi["Florida"], "\n")
results$`6_NOAA_comparison` <- noaa_cmp

# ---- 7. Canal size by Water Management District ------------------------------
scale_levels <- c("Small", "Moderate", "Large", "Very Large/Mega")

# Polygon level (note: ~13% of polygons are halves of split canal systems)
size_poly <- poly_df %>%
  mutate(area_acres_calc = area_m2_calc / M2_PER_ACRE) %>%
  group_by(WMDNAME) %>%
  summarise(n_polygons = n(),
            total_area_acres = sum(area_acres_calc),
            total_area_km2 = sum(area_m2_calc) / 1e6,
            mean_area_acres = mean(area_acres_calc),
            median_area_acres = median(area_acres_calc),
            p90_area_acres = quantile(area_acres_calc, 0.9),
            max_area_acres = max(area_acres_calc),
            median_shoreline_mi = median(peri_mi_calc),
            pct_area_in_very_large = 100 * sum(area_m2_calc[TYP_SCALE %in% "Very Large/Mega"]) /
              sum(area_m2_calc),
            .groups = "drop")

# Mean canal width = canal water area / navigable centerline length (a field-style
# average width; overstates width where basins are open)
width_wmd <- left_join(
  poly_df %>% group_by(WMDNAME) %>% summarise(area_m2 = sum(area_m2_calc), .groups = "drop"),
  cl_df %>% group_by(WMDNAME) %>% summarise(cl_m = sum(len_m_calc), .groups = "drop"),
  by = "WMDNAME") %>%
  mutate(mean_canal_width_m = area_m2 / cl_m) %>% select(WMDNAME, mean_canal_width_m)

# Existing size classes (TYP_SCALE: Small < 2.5 ac, Moderate 2.5-12.4, Large 12.4-62,
# Very Large/Mega > 62 ac) as counts and shares
scale_tbl <- poly_df %>%
  filter(!is.na(TYP_SCALE)) %>%
  count(WMDNAME, TYP_SCALE) %>%
  group_by(WMDNAME) %>% mutate(pct = 100 * n / sum(n)) %>% ungroup() %>%
  mutate(TYP_SCALE = factor(TYP_SCALE, levels = scale_levels)) %>%
  pivot_wider(id_cols = WMDNAME, names_from = TYP_SCALE, values_from = pct,
              names_prefix = "pct_", values_fill = 0)

# Canal-system level: SYSTEM_ID joins the pieces of one canal system
# Polygons with SYSTEM_ID = 0 are treated as their own system (key = -CHAWQ_ID)
poly_df$system_key <- ifelse(poly_df$SYSTEM_ID > 0, poly_df$SYSTEM_ID, -poly_df$CHAWQ_ID)
systems <- poly_df %>%
  group_by(system_key) %>%
  summarise(WMDNAME = WMDNAME[which.max(area_m2_calc)],     # district holding most of the system
            n_pieces = n(),
            area_acres = sum(area_m2_calc) / M2_PER_ACRE,
            shoreline_mi = sum(peri_mi_calc), .groups = "drop")
size_system <- systems %>%
  group_by(WMDNAME) %>%
  summarise(n_systems = n(),
            mean_system_area_acres = mean(area_acres),
            median_system_area_acres = median(area_acres),
            p90_system_area_acres = quantile(area_acres, 0.9),
            max_system_area_acres = max(area_acres),
            median_system_shoreline_mi = median(shoreline_mi),
            .groups = "drop")

size_wmd <- size_poly %>%
  left_join(size_system, by = "WMDNAME") %>%
  left_join(width_wmd, by = "WMDNAME") %>%
  left_join(scale_tbl, by = "WMDNAME") %>%
  left_join(by_wmd %>% select(WMDNAME, shoreline_mi, navigable_mi), by = "WMDNAME") %>%
  mutate(shoreline_mi_per_100_acres = 100 * shoreline_mi / (total_area_acres)) %>%
  arrange(desc(total_area_acres))
show(size_wmd %>% select(WMDNAME, n_polygons, total_area_acres, mean_area_acres,
                         median_area_acres, max_area_acres, n_systems,
                         median_system_area_acres, mean_canal_width_m,
                         pct_area_in_very_large) %>%
       mutate(across(where(is.numeric), ~ round(.x, 1))),
     "7a. Canal size by Water Management District")
show(size_wmd %>% select(WMDNAME, starts_with("pct_")) %>%
       mutate(across(where(is.numeric), ~ round(.x, 1))),
     "7b. Share of polygons in each existing size class (percent)")

# Do districts differ? Systems are the independent units; areas are heavily skewed,
# so use rank-based tests. Spatial clustering makes p-values optimistic.
kw <- kruskal.test(area_acres ~ factor(WMDNAME), data = systems)
pw <- pairwise.wilcox.test(systems$area_acres, systems$WMDNAME,
                           p.adjust.method = "holm", exact = FALSE)
pw_tbl <- as.data.frame(as.table(pw$p.value)) %>%
  filter(!is.na(Freq)) %>%
  rename(WMD_a = Var1, WMD_b = Var2, p_holm = Freq) %>%
  mutate(p_holm = signif(p_holm, 3))
size_test <- tibble(test = "Kruskal-Wallis on canal-system area by district",
                    statistic = unname(kw$statistic), df = unname(kw$parameter),
                    p_value = kw$p.value)
show(size_test, "7c. Do canal-system sizes differ among districts?")
show(pw_tbl, "7d. Pairwise Wilcoxon tests (Holm-adjusted p)")
results$`7_size_by_WMD` <- size_wmd
results$`7_size_tests` <- size_test
results$`7_pairwise` <- pw_tbl


# ---- 8. Distance from the mouth by Water Management District ----------------
# In-water distance from each system's mouth, from the per-polygon zonal
# statistics of the cost-distance rasters (DIST_MEAN, DIST_MAX, in meters).
# Distances are strongly right-skewed (a few Lake Okeechobee systems reach 30 km),
# so medians, IQRs and rank-based tests are used.
iqr_txt <- function(x) paste0(round(quantile(x, 0.25)), "-", round(quantile(x, 0.75)))
dist_summary <- function(d, ...) {
  d %>% group_by(...) %>%
    summarise(n_polygons = n(),
              median_of_mean_m = median(DIST_MEAN),
              IQR_of_mean_m = iqr_txt(DIST_MEAN),
              p90_of_max_m = unname(quantile(DIST_MAX, 0.9)),
              max_m = max(DIST_MAX),
              max_CHAWQ_ID = CHAWQ_ID[which.max(DIST_MAX)],
              area_weighted_mean_m = weighted.mean(DIST_MEAN, area_m2_calc),
              .groups = "drop")
}
dist_df <- poly_df %>% filter(!is.na(DIST_MEAN))
dist_wmd <- bind_rows(
  dist_summary(dist_df, WMDNAME),
  dist_summary(dist_df) %>% mutate(WMDNAME = "ALL DISTRICTS")) %>%
  relocate(WMDNAME)
show(mutate(dist_wmd, across(where(is.double), ~ round(.x, 1))),
     "8a. Distance from the mouth by Water Management District (meters)")

# System level for the tests: split pieces of one system are not independent.
# A system's mean distance is the area-weighted mean of its pieces; its maximum is
# the largest piece maximum.
dist_sys <- dist_df %>%
  group_by(system_key) %>%
  summarise(WMDNAME = WMDNAME[which.max(area_m2_calc)],
            TYP_FORM = TYP_FORM[which.max(area_m2_calc)],
            mean_dist_m = weighted.mean(DIST_MEAN, area_m2_calc),
            max_dist_m = max(DIST_MAX),
            area_km2 = sum(area_m2_calc) / 1e6, .groups = "drop")

# Effect size for Kruskal-Wallis: epsilon-squared = H / (n - 1)
kw_row <- function(formula, data, label) {
  k <- kruskal.test(formula, data = data)
  tibble(test = label, n = nrow(data), H = unname(k$statistic), df = unname(k$parameter),
         p_value = k$p.value, epsilon_sq = unname(k$statistic) / (nrow(data) - 1))
}
pairwise_tbl <- function(x, g) {
  pw <- pairwise.wilcox.test(x, g, p.adjust.method = "holm", exact = FALSE)
  as.data.frame(as.table(pw$p.value)) %>% filter(!is.na(Freq)) %>%
    rename(group_a = Var1, group_b = Var2, p_holm = Freq) %>%
    mutate(p_holm = signif(p_holm, 3))
}
dist_tests <- bind_rows(
  kw_row(mean_dist_m ~ factor(WMDNAME), dist_sys, "Mean distance by district (systems)"),
  kw_row(max_dist_m ~ factor(WMDNAME), dist_sys, "Maximum distance by district (systems)"))
dist_pw <- bind_rows(
  pairwise_tbl(dist_sys$mean_dist_m, dist_sys$WMDNAME) %>% mutate(measure = "mean distance"),
  pairwise_tbl(dist_sys$max_dist_m, dist_sys$WMDNAME) %>% mutate(measure = "maximum distance"))
show(mutate(dist_tests, across(where(is.double), ~ signif(.x, 3))),
     "8b. Do distances differ among districts? (Kruskal-Wallis, systems)")
show(dist_pw, "8c. Pairwise Wilcoxon tests by district (Holm-adjusted p)")
cat("Note: spatial clustering of canals within districts makes these p-values optimistic;",
    "report effect sizes alongside them.\n")
results$`8_distance_by_WMD` <- dist_wmd
results$`8_distance_tests` <- dist_tests
results$`8_distance_pairwise` <- dist_pw

# ---- 9. Size, shoreline and distance by canal form (TYP_FORM) ---------------
# Shoreline development: shoreline length per unit water area (km per km2). Higher
# values mean more waterfront per unit of water, as in narrow finger canals.
form_levels <- c("Single channel", "Finger", "Branching/Dendritic", "Open basin")
cl_by_poly <- cl_df %>% group_by(CHAWQ_ID) %>%
  summarise(centerline_km = sum(len_m_calc) / 1000, .groups = "drop")
form_df <- poly_df %>%
  filter(!is.na(TYP_FORM)) %>%
  left_join(cl_by_poly, by = "CHAWQ_ID") %>%
  mutate(TYP_FORM = factor(TYP_FORM, levels = form_levels),
         area_km2 = area_m2_calc / 1e6,
         shore_per_area = peri_km_calc / area_km2,
         centerline_km = coalesce(centerline_km, 0))
stopifnot(!any(is.na(form_df$TYP_FORM)))

by_form <- form_df %>%
  group_by(TYP_FORM) %>%
  summarise(n_polygons = n(),
            total_area_km2 = sum(area_km2),
            pct_of_state_area = 100 * sum(area_km2) / sum(form_df$area_km2),
            median_area_acres = median(area_m2_calc / M2_PER_ACRE),
            total_shoreline_km = sum(peri_km_calc),
            total_centerline_km = sum(centerline_km),
            median_shore_per_area_km_per_km2 = median(shore_per_area),
            median_of_mean_dist_m = median(DIST_MEAN, na.rm = TRUE),
            IQR_of_mean_dist_m = iqr_txt(na.omit(DIST_MEAN)),
            p90_of_max_dist_m = unname(quantile(DIST_MAX, 0.9, na.rm = TRUE)),
            .groups = "drop")
form_x_wmd <- form_df %>% count(WMDNAME, TYP_FORM) %>%
  group_by(WMDNAME) %>% mutate(pct = 100 * n / sum(n)) %>% ungroup() %>%
  pivot_wider(id_cols = WMDNAME, names_from = TYP_FORM, values_from = c(n, pct), values_fill = 0)
show(mutate(by_form, across(where(is.double), ~ round(.x, 1))),
     "9a. Size, shoreline and distance by canal form")
show(mutate(form_x_wmd, across(where(is.double), ~ round(.x, 1))),
     "9b. Canal forms within each district (count and percent of polygons)")

# Tests at the polygon level: form is recorded per polygon
form_tests <- bind_rows(
  kw_row(area_km2 ~ TYP_FORM, form_df, "Polygon area by form"),
  kw_row(shore_per_area ~ TYP_FORM, form_df, "Shoreline per area by form"),
  kw_row(DIST_MEAN ~ TYP_FORM, filter(form_df, !is.na(DIST_MEAN)), "Mean distance by form"),
  kw_row(DIST_MAX ~ TYP_FORM, filter(form_df, !is.na(DIST_MAX)), "Maximum distance by form"))
form_pw <- bind_rows(
  pairwise_tbl(form_df$DIST_MEAN, form_df$TYP_FORM) %>% mutate(measure = "mean distance"),
  pairwise_tbl(form_df$shore_per_area, form_df$TYP_FORM) %>% mutate(measure = "shoreline per area"))
show(mutate(form_tests, across(where(is.double), ~ signif(.x, 3))),
     "9c. Do canal forms differ? (Kruskal-Wallis, polygons)")
show(form_pw, "9d. Pairwise Wilcoxon tests by form (Holm-adjusted p)")
results$`9_by_form` <- by_form
results$`9_form_x_WMD` <- form_x_wmd
results$`9_form_tests` <- form_tests
results$`9_form_pairwise` <- form_pw

# ---- Optional spot check -----------------------------------------------------
if (!is.null(spot_check_name)) {
  idx <- str_starts(coalesce(poly$WATERBODY_, ""), spot_check_name)
  spot <- tibble(
    place = spot_check_name,
    polygons = sum(idx),
    shoreline_mi = sum(poly$peri_mi_calc[idx]),
    shoreline_km = sum(poly$peri_km_calc[idx]),
    navigable_mi = to_mi(sum(cl$len_m_calc[cl$poly_row %in% poly$poly_row[idx]])),
    navigable_km = to_km(sum(cl$len_m_calc[cl$poly_row %in% poly$poly_row[idx]])))
  show(mutate(spot, across(where(is.numeric), ~ round(.x, 2))),
       "Spot check by WATERBODY_ name")
  results$spot_check <- spot
}

# ---- Write outputs ---------------------------------------------------------
for (nm in names(results)) save_csv(results[[nm]], paste0(gsub("[^A-Za-z0-9_]", "_", nm), ".csv"))
if (requireNamespace("writexl", quietly = TRUE)) {
  writexl::write_xlsx(results, file.path(out_dir, "chawq_canal_analysis_results.xlsx"))
}
# keep the enriched layers for mapping or follow-up work
# (DATESTAMP is written as text because blank dates trip up the GeoPackage writer)
st_write(mutate(poly, DATESTAMP = as.character(DATESTAMP)),
         file.path(out_dir, "polygons_with_region.gpkg"), delete_dsn = TRUE, quiet = TRUE)
st_write(cl,
         file.path(out_dir, "centerlines_with_county_region.gpkg"), delete_dsn = TRUE, quiet = TRUE)
writeLines(capture.output(sessionInfo()), file.path(out_dir, "sessionInfo.txt"))
cat("\nDone. Results are in:", normalizePath(out_dir), "\n")
