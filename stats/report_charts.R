# LabCrew Lite - report figures (Manhattan + QQ) as PNG, for the HTML and PDF report.
# All data are SIMULATED / FICTIONAL. Same look as labcrew's ui/report_export.R (base R, no extra packages).
# Usage: Rscript stats/report_charts.R in.json out_dir
#   in.json: {"plot": {position, neglog10p, qq_expected, qq_observed, variant_ids}, "bonferroni_threshold", "lambda_gc"}
# Values are drawn as given (computed by stats/gwas.R); nothing is recomputed here.
suppressPackageStartupMessages(library(jsonlite))
args <- commandArgs(trailingOnly = TRUE)
inp <- fromJSON(args[1], simplifyVector = TRUE); out <- args[2]
dir.create(out, showWarnings = FALSE, recursive = TRUE)

RX <- list(ink = "#111827", ink2 = "#4b5563", grid = "#e5e7eb", blue = "#2a78d6", orange = "#eb6834")
chart_par <- function() par(family = "sans", mar = c(4.2, 4.2, 1.6, 1), mgp = c(2.4, .7, 0), tcl = -.25, las = 1,
                            col.axis = RX$ink2, col.lab = RX$ink2, cex.axis = .8, cex.lab = .85, fg = RX$ink2, bty = "n")
grid_lines <- function() { grid(col = RX$grid, lty = 1); box(col = RX$grid) }
draw <- function(file, f) {
  type <- if (isTRUE(capabilities("cairo"))) "cairo" else getOption("bitmapType")
  png(file, width = 6.6, height = 3.3, units = "in", res = 200, type = type)
  on.exit(dev.off()); chart_par(); f()
}

p <- inp$plot
draw(file.path(out, "manhattan.png"), function() {
  x <- p$position; y <- p$neglog10p; id <- p$variant_ids
  thr <- -log10(inp$bonferroni_threshold); hit <- y > thr
  plot(x, y, type = "n", xlab = "Variant position (simulated panel)", ylab = expression(-log[10](p)), ylim = c(0, max(c(y, thr)) * 1.12))
  grid_lines(); points(x[!hit], y[!hit], pch = 16, cex = .45, col = adjustcolor(RX$blue, .55))
  abline(h = thr, lty = 2, col = RX$ink2, lwd = 1.2)
  points(x[hit], y[hit], pch = 21, bg = RX$orange, col = "white", cex = 1.4, lwd = 1.2)
  if (any(hit)) text(x[hit], y[hit], id[hit], pos = 3, cex = .7, col = RX$ink)
  text(min(x), thr, sprintf("Bonferroni p = %s", inp$bonferroni_threshold), adj = c(0, -.4), cex = .7, col = RX$ink2)
})
draw(file.path(out, "qq.png"), function() {
  e <- p$qq_expected; o <- p$qq_observed; mx <- max(c(e, o))
  plot(e, o, type = "n", xlab = expression(Expected~-log[10](p)), ylab = expression(Observed~-log[10](p)), xlim = c(0, mx), ylim = c(0, mx))
  grid_lines(); abline(0, 1, lty = 2, col = RX$ink2, lwd = 1.2)
  points(e, o, pch = 16, cex = .5, col = adjustcolor(RX$blue, .7))
  legend("topleft", legend = sprintf("lambda = %s", inp$lambda_gc), bty = "n", cex = .85)
})
