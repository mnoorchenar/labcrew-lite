# Purpose: the GWAS statistics engine, in R. Python's CrewAI tools call it; it never sees the LLM.
#
# Usage:  Rscript stats/gwas.R <input.json> <output.json>
# Input:  paths to the genotype matrix (raw float64, column-major, NaN = missing call) and a CSV of
#         age, sex, trait; plus the QC thresholds and prior from rules.yaml (Python reads the YAML).
# Output: QC counts, per-variant regression, lambda, Bonferroni hits, PIP ranking, plot data.
#
# Each choice in two sentences:
#   * Regression: base R lm(trait ~ genotype + age + sex) per variant, the standard, validated linear model.
#     Missing calls are mean-imputed per variant (simple; a stated limitation).
#   * Lambda = median(chi-square) / qchisq(0.5, 1): inflation of the whole test distribution.
#   * Bayes factors: coloc::finemap.abf (published Wakefield ABF, prior effect SD = 0.15 x SD(trait)).
#     coloc's own posterior assumes one causal variant per region, so we combine its log ABF with the stated
#     prior per variant instead: PIP = plogis(lABF + qlogis(prior)), assuming independent variants.
suppressPackageStartupMessages({ library(jsonlite); library(coloc) })

args <- commandArgs(trailingOnly = TRUE)
inp <- fromJSON(args[1], simplifyVector = TRUE)

# ---------------------------------------------------------------- data
G <- matrix(readBin(inp$genotypes_bin, "double", n = inp$n * inp$m), nrow = inp$n, ncol = inp$m)
G[is.nan(G)] <- NA
cov <- read.csv(inp$covariates_csv)
ids <- inp$variant_ids

# ---------------------------------------------------------------- variant QC
called <- !is.na(G)
n_called <- colSums(called)
n1 <- colSums(G == 1, na.rm = TRUE); n2 <- colSums(G == 2, na.rm = TRUE); n0 <- n_called - n1 - n2
p <- (n1 + 2 * n2) / pmax(2 * n_called, 1)
qc <- list(maf = pmin(p, 1 - p), call_rate = n_called / inp$n)
expct <- rbind((1 - p)^2, 2 * p * (1 - p), p^2) * rep(n_called, each = 3)
chi2 <- colSums(ifelse(expct > 0, (rbind(n0, n1, n2) - expct)^2 / expct, 0))
qc$hwe_p <- ifelse(qc$maf > 0, pchisq(chi2, df = 1, lower.tail = FALSE), NA)   # monomorphic: untestable

keep <- rep(TRUE, inp$m)
failed_by <- list()
for (i in seq_len(nrow(inp$qc_rules))) {           # thresholds come from rules.yaml; NA never passes
  r <- inp$qc_rules[i, ]
  v <- qc[[r$metric]]
  ok <- !is.na(v) & (is.na(r$min) | v >= r$min) & (is.na(r$max) | v <= r$max)
  failed_by[[r$id]] <- sum(!ok)
  keep <- keep & ok
}

# ---------------------------------------------------------------- association: lm() per variant
tested <- which(keep)
fit <- t(vapply(tested, function(j) {
  g <- G[, j]
  g[is.na(g)] <- mean(g, na.rm = TRUE)
  summary(lm(cov$trait ~ g + cov$age + cov$sex))$coefficients["g", ]
}, numeric(4)))                                       # columns: Estimate, Std. Error, t value, Pr(>|t|)
beta <- fit[, 1]; se <- fit[, 2]; tval <- fit[, 3]; pval <- fit[, 4]
m_t <- length(tested)
threshold <- inp$family_wise_alpha / m_t
lambda <- median(tval^2) / qchisq(0.5, df = 1)

# ---------------------------------------------------------------- Bayes factors (coloc) and PIP
# coloc fixes the prior effect SD at 0.15 x SD(trait). Refuse a rules file asking for anything else,
# instead of silently ignoring the versioned setting.
if (!isTRUE(all.equal(inp$prior_effect_sd_in_trait_sd, 0.15)))
  stop("rules.yaml prior_effect_sd_in_trait_sd must be 0.15: coloc fixes the prior SD at 0.15 x SD(trait)")
abf <- finemap.abf(list(beta = beta, varbeta = se^2, type = "quant", sdY = sd(cov$trait),
                        snp = ids[tested]), p1 = 1e-4)  # p1 only affects coloc's SNP.PP, which we do not use
labf <- abf$lABF.[match(ids[tested], abf$snp)]
pip <- plogis(labf + qlogis(inp$prior_prob_associated))

# ---------------------------------------------------------------- output
row <- function(k) list(variant_id = ids[tested[k]], beta = signif(beta[k], 4), se = signif(se[k], 4),
                        p = signif(pval[k], 3), pip = signif(pip[k], 3), bonferroni_hit = pval[k] < threshold)
hit_k <- which(pval < threshold)
hit_k <- hit_k[order(pval[hit_k])]
m_all <- inp$m
qc_frac <- signif(1 - m_t / m_all, 4)
lam <- round(lambda, 4)
out <- list(
  engine = list(R = paste(R.version$major, R.version$minor, sep = "."), coloc = as.character(packageVersion("coloc"))),
  n_samples = inp$n, n_variants = m_all, n_tested = m_t, qc_failed_by = failed_by, qc_fail_fraction = qc_frac,
  bonferroni_threshold = signif(threshold, 4), lambda_gc = lam, n_hits = length(hit_k),
  hits = lapply(hit_k, row), ranked = lapply(head(order(-pip), 20), row),   # head(): fewer than 20 tested must not index NA
  plot = list(position = as.integer(sub("var", "", ids[tested])), neglog10p = round(-log10(pval), 3),
              qq_expected = round(-log10((seq_len(m_t) - 0.5) / m_t), 3),
              qq_observed = round(sort(-log10(pval), decreasing = TRUE), 3), variant_ids = ids[tested]),
  metrics = list(qc_fail_fraction = qc_frac, lambda_gc = lam, n_hits = length(hit_k)))
writeLines(toJSON(out, auto_unbox = TRUE, digits = NA), args[2])
