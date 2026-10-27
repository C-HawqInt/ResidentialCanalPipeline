# =============================================================================
# C-HAWQ | NCEAS Working Group | Canal figures and summary table
# =============================================================================
# Version 2 (2026-10-27), for the FINAL layers (10212026, 3,129 systems).
# Companion to chawq_residential_canal_analysis_v2.R. Run that script first.
# This file works two ways:
#   - in the same R session (it reuses `poly_df` and `cl_df`), or
#   - on its own later (it reads the GeoPackages the main script saved).
#
# Needs: sf, dplyr, tidyr, readr, ggplot2, scales
#   install.packages(c("ggplot2", "scales"))   # the others come with the main script
#
# Outputs (folder Statistical Analysis/canal_analysis_outputs_v2/figures):
#   fig1_area_km2_by_WMD.png          fig2_centerline_km_by_WMD.png
#   fig3_shoreline_km_by_WMD.png      table4_WMD_summary.csv
#   fig5a_share_of_state_area.png     fig5b_system_size_by_WMD.png
#   county_summary_all_counties.csv   county_top5_table.csv
#   county_area_km2_top5.png          county_centerline_km_top5.png
#   county_shoreline_km_top5.png
#   fig6_distance_by_WMD.png          fig7_distance_by_form.png
#   fig8_shoreline_per_area_by_form.png
# A distance-to-mouth plot is banked at the bottom for when that field exists.
# =============================================================================

suppressPackageStartupMessages({
  library(sf); library(dplyr); library(tidyr); library(readr)
  library(ggplot2); library(scales)
})

ms_dir  <- Sys.getenv("CHAWQ_MS_DIR", "E:/CHAWQ/ARW Manuscript")
out_dir <- file.path(ms_dir, "Statistical Analysis", "canal_analysis_outputs_v2")
fig_dir <- file.path(out_dir, "figures")
dir.create(fig_dir, showWarnings = FALSE, recursive = TRUE)

# ---- Data: reuse the main script's tables, or reload them ---------------------
if (!exists("poly_df") || !exists("cl_df")) {
  poly_df <- st_drop_geometry(st_read(file.path(out_dir, "polygons_with_region.gpkg"), quiet = TRUE))
  cl_df   <- st_drop_geometry(st_read(file.path(out_dir, "centerlines_with_county_region.gpkg"), quiet = TRUE))
}

# ---- Look and feel (edit here to restyle every figure) ------------------------
bar_col   <- "#2a78d6"   # one hue for one measure; no rainbow needed
ink       <- "#0b0b0b"   # titles and values
ink_soft  <- "#52514e"   # axes, captions
grid_col  <- "#e6e5e1"   # recessive gridlines

theme_chawq <- function(base = 12) {
  theme_minimal(base_size = base) +
    theme(
      plot.title.position = "plot",
      plot.title    = element_text(face = "bold", colour = ink, size = base + 3),
      plot.subtitle = element_text(colour = ink_soft, margin = margin(b = 10)),
      plot.caption  = element_text(colour = ink_soft, size = base - 3, hjust = 0),
      axis.text     = element_text(colour = ink),
      axis.title    = element_text(colour = ink_soft),
      panel.grid.minor   = element_blank(),
      panel.grid.major.y = element_blank(),
      panel.grid.major.x = element_line(colour = grid_col, linewidth = 0.3),
      plot.margin = margin(12, 16, 10, 12)
    )
}

wmd_names <- c(SFWMD  = "South Florida",  SWFWMD = "Southwest Florida",
               SJRWMD = "St. Johns River", NWFWMD = "Northwest Florida",
               SRWMD  = "Suwannee River")
src_note <- "Source: C-HAWQ residential canal polygons and centerlines (final layers, 10/21/2026)."

# ---- Summary by WMD: area (km2), shoreline (km), navigable centerline (km) ----
area_shore <- poly_df %>%
  group_by(WMDNAME) %>%
  summarise(area_km2 = sum(area_m2_calc) / 1e6, shoreline_km = sum(peri_km_calc), .groups = "drop")
center <- cl_df %>%
  group_by(WMDNAME) %>%
  summarise(centerline_km = sum(len_m_calc) / 1000, .groups = "drop")

wmd_tbl <- left_join(area_shore, center, by = "WMDNAME") %>%
  arrange(desc(area_km2)) %>%
  mutate(District = unname(wmd_names[WMDNAME]),
         pct_of_state_area = 100 * area_km2 / sum(area_km2))
wmd_order <- wmd_tbl$WMDNAME            # one fixed order for every figure

# ---- 4. Table ----------------------------------------------------------------
table4 <- wmd_tbl %>%
  transmute(WMD = WMDNAME, District,
            `Canal area (km2)` = area_km2,
            `Shoreline (km)` = shoreline_km,
            `Navigable centerline (km)` = centerline_km) %>%
  bind_rows(summarise(., WMD = "Florida total", District = "",
                      across(where(is.numeric), sum))) %>%
  mutate(across(where(is.numeric), ~ round(.x, 1)))
print(as.data.frame(table4), row.names = FALSE)
write_csv(table4, file.path(fig_dir, "table4_WMD_summary.csv"))

# ---- Shared bar-plot builder (figures 1 to 3) ---------------------------------
# Horizontal bars in a fixed district order, value printed at each bar end.
bar_by_wmd <- function(df, value, title, axis_title, digits = 0, file) {
  d <- df %>% mutate(WMDNAME = factor(WMDNAME, levels = rev(wmd_order)))
  p <- ggplot(d, aes(x = .data[[value]], y = WMDNAME)) +
    geom_col(fill = bar_col, width = 0.6) +
    geom_text(aes(label = comma(.data[[value]], accuracy = 10^-digits)),
              hjust = -0.15, colour = ink, size = 4) +
    scale_x_continuous(labels = comma, expand = expansion(mult = c(0, 0.15))) +
    labs(title = title, x = axis_title, y = NULL, caption = src_note) +
    theme_chawq()
  ggsave(file.path(fig_dir, file), p, width = 7.5, height = 4, dpi = 300, bg = "white")
  p
}

# ---- 1. Canal area by WMD (km2) ------------------------------------------------
fig1 <- bar_by_wmd(wmd_tbl, "area_km2", "Canal water area by Water Management District",
                   expression("Canal area (km"^2*")"), digits = 1, file = "fig1_area_km2_by_WMD.png")

# ---- 2. Navigable centerline by WMD (km) ---------------------------------------
fig2 <- bar_by_wmd(wmd_tbl, "centerline_km", "Navigable canal length by Water Management District",
                   "Navigable centerline (km)", digits = 0, file = "fig2_centerline_km_by_WMD.png")

# ---- 3. Shoreline by WMD (km) --------------------------------------------------
fig3 <- bar_by_wmd(wmd_tbl, "shoreline_km", "Canal shoreline by Water Management District",
                   "Shoreline (km)", digits = 0, file = "fig3_shoreline_km_by_WMD.png")

# ---- 5a. Share of the statewide canal area held by each WMD -------------------
state_total <- sum(wmd_tbl$area_km2)
d5a <- wmd_tbl %>%
  mutate(WMDNAME = factor(WMDNAME, levels = rev(wmd_order)),
         label = paste0(percent(pct_of_state_area / 100, accuracy = 0.1),
                        "  (", comma(area_km2, accuracy = 0.1), " sq km)"))
fig5a <- ggplot(d5a, aes(x = pct_of_state_area, y = WMDNAME)) +
  geom_col(fill = bar_col, width = 0.6) +
  geom_text(aes(label = label), hjust = -0.08, colour = ink, size = 4) +
  scale_x_continuous(labels = label_number(suffix = "%"), limits = c(0, 100),
                     expand = expansion(mult = c(0, 0))) +
  labs(title = "Share of Florida's canal water area by district",
       subtitle = paste0("Statewide total: ", comma(state_total, accuracy = 0.1), " sq km"),
       x = "Share of statewide canal area", y = NULL, caption = src_note) +
  theme_chawq()
ggsave(file.path(fig_dir, "fig5a_share_of_state_area.png"), fig5a,
       width = 7.5, height = 4, dpi = 300, bg = "white")

# ---- 5b. How big are individual canal systems in each WMD? ---------------------
# One dot per canal system (SYSTEM_ID joins the pieces of one system). Log scale
# because sizes span four orders of magnitude. The bar marks the median.
# Polygons with SYSTEM_ID = 0 count as their own system (same rule as the main script)
poly_df$system_key <- ifelse(poly_df$SYSTEM_ID > 0, poly_df$SYSTEM_ID, -poly_df$CHAWQ_ID)
systems <- poly_df %>%
  group_by(system_key) %>%
  summarise(WMDNAME = WMDNAME[which.max(area_m2_calc)],
            area_acres = sum(area_m2_calc) / 4046.8564224,
            area_km2 = sum(area_m2_calc) / 1e6, .groups = "drop") %>%
  mutate(WMDNAME = factor(WMDNAME, levels = rev(wmd_order)))
med <- systems %>% group_by(WMDNAME) %>% summarise(med = median(area_km2), .groups = "drop")

set.seed(1)   # the jitter is cosmetic; the seed keeps the figure reproducible
fig5b <- ggplot(systems, aes(x = area_km2, y = WMDNAME)) +
  geom_jitter(height = 0.22, width = 0, colour = bar_col, alpha = 0.35, size = 1.3) +
  geom_errorbar(data = med, aes(x = med, xmin = med, xmax = med, y = WMDNAME),
                inherit.aes = FALSE, width = 0.55, colour = ink, linewidth = 0.9) +
  scale_x_log10(breaks = c(0.0001, 0.001, 0.01, 0.1, 1),
                labels = function(x) format(x, scientific = FALSE, drop0trailing = TRUE)) +
  labs(title = "Size of individual canal systems by district",
       subtitle = "Each dot is one canal system; black bar is the district median",
       x = expression("Canal system area (km"^2*", log scale)"), y = NULL, caption = src_note) +
  theme_chawq()
ggsave(file.path(fig_dir, "fig5b_system_size_by_WMD.png"), fig5b,
       width = 7.5, height = 4.5, dpi = 300, bg = "white")

cat("\nFigures and table saved to:", normalizePath(fig_dir), "\n")


# =============================================================================
# COUNTY SUMMARIES AND TOP-COUNTY FIGURES
# =============================================================================
# Polygons give the count, area, and shoreline for each county. Centerlines are
# assigned to the county of the polygon that contains their midpoint (done in the
# main script), so both layers describe the same counties.

top_n_counties <- 5              # how many counties get figures
county_rank_by <- "area_km2"     # rank by "area_km2", "shoreline_km", or "centerline_km"

# ---- County table: every county, polygon and centerline data together ---------
county_all <- poly_df %>%
  group_by(County) %>%
  summarise(Region = names(sort(table(Region), decreasing = TRUE))[1],  # most common region
            n_polygons = n(),
            area_km2 = sum(area_m2_calc) / 1e6,
            area_acres = sum(area_m2_calc) / 4046.8564224,
            shoreline_km = sum(peri_km_calc),
            shoreline_mi = sum(peri_mi_calc),
            .groups = "drop") %>%
  left_join(cl_df %>%
              group_by(County) %>%
              summarise(n_centerline_segments = n(),
                        centerline_km = sum(len_m_calc) / 1000,
                        centerline_mi = sum(len_m_calc) / 1609.344,
                        .groups = "drop"),
            by = "County") %>%
  mutate(across(c(n_centerline_segments, centerline_km, centerline_mi), ~ replace_na(.x, 0))) %>%
  arrange(desc(.data[[county_rank_by]]))

county_csv <- county_all %>%
  select(County, Region, n_polygons, area_km2, area_acres, shoreline_km, shoreline_mi,
         n_centerline_segments, centerline_km, centerline_mi) %>%
  bind_rows(summarise(., County = "ALL COUNTIES", Region = "",
                      across(where(is.numeric), sum))) %>%
  mutate(across(where(is.double), ~ round(.x, 3)))
write_csv(county_csv, file.path(fig_dir, "county_summary_all_counties.csv"))

top_counties <- county_all %>% slice_head(n = top_n_counties)
top_csv <- top_counties %>%
  transmute(County, Region, `Polygons` = n_polygons,
            `Canal area (km2)` = round(area_km2, 2),
            `Shoreline (km)` = round(shoreline_km, 1),
            `Navigable centerline (km)` = round(centerline_km, 1))
print(as.data.frame(top_csv), row.names = FALSE)
write_csv(top_csv, file.path(fig_dir, "county_top5_table.csv"))

# ---- Bar plots for the top counties (same look as the WMD figures) -------------
county_order <- top_counties$County      # one fixed order for all three figures
rank_label <- c(area_km2 = "canal area", shoreline_km = "canal shoreline",
                centerline_km = "navigable centerline length")[[county_rank_by]]

bar_by_county <- function(value, title, axis_title, digits, file) {
  d <- top_counties %>% mutate(County = factor(County, levels = rev(county_order)))
  p <- ggplot(d, aes(x = .data[[value]], y = County)) +
    geom_col(fill = bar_col, width = 0.6) +
    geom_text(aes(label = comma(.data[[value]], accuracy = 10^-digits)),
              hjust = -0.15, colour = ink, size = 4) +
    scale_x_continuous(labels = comma, expand = expansion(mult = c(0, 0.15))) +
    labs(title = title,
         subtitle = paste0("Top ", top_n_counties, " counties by ", rank_label),
         x = axis_title, y = NULL, caption = src_note) +
    theme_chawq()
  ggsave(file.path(fig_dir, file), p, width = 7.5, height = 4, dpi = 300, bg = "white")
  p
}

fig_cty_area <- bar_by_county("area_km2", "Canal water area by county",
                              expression("Canal area (km"^2*")"), 1, "county_area_km2_top5.png")
fig_cty_shore <- bar_by_county("shoreline_km", "Canal shoreline by county",
                               "Shoreline (km)", 0, "county_shoreline_km_top5.png")
fig_cty_center <- bar_by_county("centerline_km", "Navigable canal length by county",
                                "Navigable centerline (km)", 0, "county_centerline_km_top5.png")
cat("County files saved to:", normalizePath(fig_dir), "\n")


# =============================================================================
# DISTANCE FROM THE MOUTH AND CANAL FORM (new in version 2)
# =============================================================================
# Same design as fig5b: one dot per unit on a log axis, black bar at the median.
# Distances come from the per-polygon zonal statistics (DIST_MEAN, meters).

dot_median_plot <- function(d, value, group, title, subtitle, x_title, breaks, file,
                            height = 4.5) {
  med <- d %>% group_by(.data[[group]]) %>%
    summarise(med = median(.data[[value]]), n = n(), .groups = "drop")
  p <- ggplot(d, aes(x = .data[[value]], y = .data[[group]])) +
    geom_jitter(height = 0.22, width = 0, colour = bar_col, alpha = 0.3, size = 1.2) +
    geom_errorbar(data = med, aes(x = med, xmin = med, xmax = med, y = .data[[group]]),
                  inherit.aes = FALSE, width = 0.55, colour = ink, linewidth = 0.9) +
    scale_y_discrete(labels = setNames(
      paste0(med[[group]], "\nmedian ", comma(med$med, accuracy = 1), ", n = ", comma(med$n)),
      med[[group]])) +
    scale_x_log10(breaks = breaks, labels = comma) +
    labs(title = title, subtitle = subtitle, x = x_title, y = NULL, caption = src_note) +
    theme_chawq()
  ggsave(file.path(fig_dir, file), p, width = 7.5, height = height, dpi = 300, bg = "white")
  p
}

# ---- 6. Distance from the mouth by district (systems) -------------------------
# A system's mean distance is the area-weighted mean of its pieces.
dist_sys <- poly_df %>%
  filter(!is.na(DIST_MEAN)) %>%
  group_by(system_key) %>%
  summarise(WMDNAME = WMDNAME[which.max(area_m2_calc)],
            mean_dist_m = weighted.mean(DIST_MEAN, area_m2_calc), .groups = "drop") %>%
  mutate(WMDNAME = factor(WMDNAME, levels = rev(wmd_order)))
set.seed(1)
fig6 <- dot_median_plot(
  dist_sys, "mean_dist_m", "WMDNAME",
  title = "Typical in-water distance from the mouth is similar across districts",
  subtitle = "Mean distance within each canal system; black bar marks the district median",
  x_title = "Mean in-water distance from the mouth (m, log scale)",
  breaks = c(10, 30, 100, 300, 1000, 3000, 10000), file = "fig6_distance_by_WMD.png")

# ---- 7 and 8. Canal form ----------------------------------------------------------
form_levels <- c("Single channel", "Finger", "Branching/Dendritic", "Open basin")
form_df <- poly_df %>%
  mutate(TYP_FORM = dplyr::recode(TYP_FORM, "Single Channel" = "Single channel")) %>%
  filter(!is.na(TYP_FORM), !is.na(DIST_MEAN)) %>%
  mutate(TYP_FORM = factor(TYP_FORM, levels = rev(form_levels)),
         shore_per_area = peri_km_calc / (area_m2_calc / 1e6))
set.seed(1)
fig7 <- dot_median_plot(
  form_df, "DIST_MEAN", "TYP_FORM",
  title = "Finger and branching canals hold water farther from the mouth",
  subtitle = "Mean in-water distance within each polygon; black bar marks the median",
  x_title = "Mean in-water distance from the mouth (m, log scale)",
  breaks = c(10, 30, 100, 300, 1000, 3000, 10000), file = "fig7_distance_by_form.png")
set.seed(1)
fig8 <- dot_median_plot(
  form_df, "shore_per_area", "TYP_FORM",
  title = "Single-channel canals have the most shoreline per unit of water",
  subtitle = "Shoreline length per water area for each polygon; black bar marks the median",
  x_title = expression("Shoreline per water area (km per km"^2*", log scale)"),
  breaks = c(10, 30, 100, 300, 1000), file = "fig8_shoreline_per_area_by_form.png")
cat("Distance and canal-form figures saved to:", normalizePath(fig_dir), "\n")


# =============================================================================
# BANKED IDEA | Distance to mouth by district (needs data we do not have yet)
# =============================================================================
# Question: which district has the longer or more complex canal networks?
# Why distance to mouth: it measures how far water, boats, and pollutants must
# travel to reach open water, and it grows with network length and branching.
#
# What the data must provide: for each centerline segment, `dist_to_mouth_m`, the
# distance measured along the canal network to the nearest mouth (the point where
# a canal system opens to the receiving water). The polygons already carry
# `NumMouth` (how many mouths) but not where they are. Once mouth points exist,
# a network tool such as the sfnetworks package can compute the distance for every
# segment.
#
# Once cl_df has the column, run:  plot_dist_to_mouth(cl_df)   (optional: bw_km = 0.1)
# It shows, for each district, the length-weighted spread of distance to mouth
# (every kilometer of canal counts once, so long canals are not under-counted) and prints median, 90th percentile, and
# maximum distance in km.

weighted_q <- function(x, w, p) {          # length-weighted quantile
  o <- order(x); x <- x[o]; w <- w[o]
  x[which(cumsum(w) / sum(w) >= p)[1]]
}

plot_dist_to_mouth <- function(cl_df, dist_col = "dist_to_mouth_m", bw_km = 0.05) {   # bw_km smooths the curve; raise it for a smoother shape
  if (!dist_col %in% names(cl_df))
    stop("cl_df has no '", dist_col, "' column yet. This plot needs network distance ",
         "to the nearest canal mouth for each centerline segment.")
  d <- cl_df %>%
    transmute(WMDNAME = factor(WMDNAME, levels = wmd_order),
              dist_km = .data[[dist_col]] / 1000, len_m = len_m_calc) %>%
    group_by(WMDNAME) %>% mutate(w = len_m / sum(len_m)) %>% ungroup()  # weights sum to 1 per district
  stats <- d %>% group_by(WMDNAME) %>%
    summarise(median_km = weighted_q(dist_km, len_m, 0.5),
              p90_km    = weighted_q(dist_km, len_m, 0.9),
              max_km    = max(dist_km), .groups = "drop")
  print(as.data.frame(mutate(stats, across(where(is.numeric), ~ round(.x, 2)))), row.names = FALSE)
  p <- ggplot(d, aes(x = dist_km)) +
    geom_density(aes(weight = w), fill = bar_col, colour = NA, alpha = 0.7, bw = bw_km) +
    geom_vline(data = stats, aes(xintercept = median_km), colour = ink, linewidth = 0.7) +
    facet_grid(WMDNAME ~ ., switch = "y") +
    scale_y_continuous(breaks = NULL) +
    labs(title = "Distance to the nearest canal mouth by district",
         subtitle = "Share of canal length at each distance; black line marks the median",
         x = "Distance along canal to mouth (km)", y = NULL, caption = src_note) +
    theme_chawq() +
    theme(strip.placement = "outside", strip.text.y.left = element_text(angle = 0, hjust = 1, colour = ink),
          panel.grid.major.y = element_blank())
  ggsave(file.path(fig_dir, "fig6_distance_to_mouth_by_WMD.png"), p,
         width = 7.5, height = 4.5, dpi = 300, bg = "white")
  invisible(list(plot = p, stats = stats))
}

