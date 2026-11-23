---
doc_id: SOP-GEN-001-v2.0
doc_type: sop
title: Genotype quality control
sop_id: SOP-GEN-001
version: '2.0'
status: approved
effective_date: '2026-02-01'
label: FICTIONAL - generated for a demo
---
# Genotype quality control

## Purpose
Defines variant and sample QC before association analysis (fictional). Replaces v1.0.

## Variant filters
Remove variants with call rate below 0.95 or minor allele frequency below 0.01.

## Hardy-Weinberg
Remove variants with Hardy-Weinberg p below 1e-6.

## Inflation
The genomic inflation factor lambda must lie between 0.95 and 1.05 before results are reported.
