# PL23 Scanner Truth Restoration

PL23 restores the operator-facing scanner truth contract without changing scanner execution or trading mathematics.

## Proven regression anchor
- R30 was explicitly frozen with scanner runtime orchestration as a non-regression area.
- Historical installed evidence proves Intraday 3364/3364 and Delivery 4137/4137 full-universe completion.
- PL22's deterministic Delivery coverage validator still completes 4137/4137 monotonically in 17 cycles; the scanner engine is therefore not reverted.

## PL23 correction
- Whole-universe quick cards bind to `intraday_coverage` / `delivery_coverage`.
- Intraday live analysis and Delivery deep analysis are labelled separately.
- A new sweep may show current progress, but retained `last_completed_sweep_count` / `last_completed_at` remain visible and cannot be mistaken for regression.
- Research/ML/WFA failures remain separate from scanner health.

## Frozen boundaries
PL22 scanner engine, PL22 evidence transport, Entry/Target/SL/R:R, costs, risk/admission, Model Paper lifecycle and broker authority are unchanged. Broker authority remains `NONE`.
