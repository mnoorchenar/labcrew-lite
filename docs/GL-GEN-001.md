---
doc_id: GL-GEN-001
doc_type: guideline
title: Linear regression for quantitative traits
version: '1.0'
status: approved
effective_date: '2025-01-01'
label: FICTIONAL - generated for a demo
---
# Linear regression for quantitative traits

## Model
Per-variant linear regression with covariates gives an effect size (beta) per allele and its standard error.

## Vectorisation
Residualise trait and genotypes on covariates once, then compute all variant statistics with matrix operations.
