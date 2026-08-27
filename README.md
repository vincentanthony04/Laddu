# Project Laddu (v131.1.6)

AI intelligence trading system for NSE/BSE Indian equity markets. Broker authority is `NONE` — Model Paper only, no live order execution.

---

## Install

1. Extract the full package with Windows "Extract All" (do not run from inside a zip preview).
2. From the extracted folder, run:
   ```
   INSTALL_UPDATE.cmd
   ```
   Approve the Administrator elevation prompt when asked.
3. The installer will:
   - validate package integrity and prerequisites
   - bootstrap missing Windows prerequisites where possible
   - validate the pinned Python environment
   - initialize/migrate PostgreSQL and QuestDB schema
   - preserve any existing `data`, `secure`, and `logs` state from a prior install
4. Default install root: `C:\ProgramData\ProjectLaddu`
   Installer/diagnostic evidence: `C:\Temp\ProjectLaddu`
5. On success, the app opens at `http://127.0.0.1:8086/`.

If a prerequisite needs a reboot, BIOS virtualization change, or policy approval, the installer stops before touching the target install. Re-run `INSTALL_UPDATE.cmd` after resolving it.

**Other entry points:** `START.cmd` / `STOP.cmd` / `RESTART.cmd` / `STATUS.cmd` / `uninstall.ps1`.

---

## Set the Upstox token

Live market data requires an Upstox access token. It is never stored in plaintext or committed to this repo.

```
.\settoken.ps1
```

This prompts for the token, encrypts it with Windows DPAPI (`LocalMachine` scope), writes it to `secure\upstox_token.dpapi` under the install root, and restarts the service. To read/write it programmatically, see `backend/security/token_helper.ps1`.

---

## Pending: Research / Alpha / ML / Walk-Forward

Multi-year historical data is present, but it is **not fully reaching** the research → alpha → ML → WFA → learning pipeline. This is the current top-priority engineering gap.

**Known state (from `docs/PL25`–`PL28`, `docs/R45`–`R46`):**
- The persisted research training panel (`research_delivery_training_panel`) has previously failed to be recognized as ready even when materialized, due to stale manifest fields (fixed in PL25 for that specific case).
- `factor_registry` has been found empty even when local NSE rank-IC/decay was actually computed — the trainer wasn't persisting it (fixed for that instance in PL26).
- Supervised training targets have hit `infinity`/non-finite values from zero-denominator historical calculations, breaking scikit-learn fitting (fixed for that instance in PL28).
- These were each narrow, one-off closures — not a systemic fix. The underlying question of **why full historical depth doesn't consistently reach training/WFA** is still open.

**What's not yet done (still outstanding):**
1. A full **Research Pipeline Diagnostic** that quantitatively reports, end to end:
   - historical rows/dates/symbols available vs. entering research vs. entering feature generation vs. entering alpha evaluation vs. entering training
   - WFA folds attempted/completed/rejected
   - OOF predictions, models trained vs. persisted
   - learning cycles attempted vs. completed vs. evidence persisted
2. Identifying the **first point** in that chain where historical depth is actually lost (not just the latest symptom).
3. Fixing that root blocker with a regression test that fails before and passes after.
4. Only then progressing to: alpha historical execution → WFA fold progression → ML training/persistence → learning-loop consumption.

**Rules while working on this (do not violate):**
- No weakening of WFA/validation thresholds to force a pass.
- No fabricated/backfilled alpha, training, or WFA results.
- No look-ahead or survivorship bias introduced to "unblock" data.
- A visible/failing diagnostic is preferred over a false green result.
- Trading mathematics, risk controls, and broker authority (`NONE`) stay frozen — this is a data/pipeline investigation, not a rewrite.

Recommended sequence for anyone picking this up:
```
PR 1 — Repository / contributor setup          (this repo, done)
PR 2 — Research pipeline diagnostic            (NOT DONE — start here)
PR 3 — Fix historical-data projection
PR 4 — Fix Alpha historical execution
PR 5 — Fix WFA progression
PR 6 — Fix ML training/persistence
PR 7 — Fix learning-loop consumption
PR 8 — Performance/checkpoint optimization
```

See `docs/ARCHITECTURE.md` and `docs/LEVEL5_PRODUCT_CONTRACT.md` for the full system contract.

---

## Notes

- This repo excludes local databases, logs, secrets, and DPAPI-encrypted tokens (`.gitignore`).
- Model Paper only — no live broker execution, no production broker credentials in this repo.
