# Project Laddu R27 — End-to-End Review and Closure Notes

State: **NOT ACCEPTED / NOT RELEASE** until installed evidence passes. Broker authority remains **NONE**.

## Review conclusion

R26 proved that most major runtime surfaces exist, but installed evidence still exposed three classes of product debt: (1) evidence truth was not consistently projected into Decision Proof, causing healthy materialized evidence to appear permanently DEFERRED; (2) lifecycle/operator state semantics could overstate STUCK/FAILED when one bounded research stage or a declared retry wait was the actual condition; and (3) visual density had been achieved partly through sub-10px typography and weak semantic colour, reducing readability and making good/bad states visually ambiguous.

R27 does not add another trading model and does not weaken the existing mathematical, PIT, risk, WFA, Model Paper or broker boundaries.

## Functional corrections

- Decision Proof now recognizes the canonical `setup_family` field.
- Independent research participation/liquidity evidence is projected even before a canonical trade decision exists.
- High/Adequate liquidity states normalize to PASS; Low/Weak remain evidence waits, not fabricated PASS.
- Market/sector context may be projected from materialized research context instead of requiring a duplicate decision-row field.
- Official NSE states containing a genuine `*_READY` authority state normalize to PASS; partial/pending states remain waits.
- No admitted setup no longer poisons downstream S/R, cost-adjusted R:R, canonical admission and final-action cells. Those become neutral NOT_APPLICABLE until relevant.
- Hard blockers are separated from pending requirements. The UI shows `Hard blocker` only for an actual fail; otherwise it shows `Next requirement`.
- Decision Proof adds deterministic evidence-quality and authority-tier metadata. This is evidence completeness/authority, **not a prediction-confidence percentage**.
- One WFA desk execution exception is isolated as `EXECUTION_ERROR`; lifecycle still performs read-model refresh and final reconciliation so the operator receives complete truth instead of an aborted 4/7 snapshot.
- Supervised workers explicitly waiting on retry/yield conditions normalize to EXPECTED_IDLE instead of false STUCK.

## Semantic visual system

One meaning across both themes:

- Emerald/green: healthy, running, ready, complete, qualified, selected, positive/actionable.
- Amber: legitimate evidence wait, partial, warming, recovering, pending.
- Red: actual failed, rejected, invalid, blocked, stale, stuck.
- Blue/cyan: live context, selected navigation, informational/market context.
- Neutral grey: unavailable, not required, not applicable, closed/idle where no action is required.

Authority intensity increases from evidence-building → evidence-ready → final-selected. `FINAL_SELECTED` receives the strongest emerald treatment.

## Readability correction

Historical ultra-compact CSS still contained 8–10px table/metadata values. R27 overrides those with an explicit floor:

- Body 15px.
- Primary tables 13px; headings ~11.5px with strong weight.
- Quote values 14px; quote labels 10.5px.
- Main headings 30px, section headings 17px.
- Model Paper, Research, Accuracy and Progress operational details raised to readable 11.5–15px bands.

Density is now created by compact spacing and information hierarchy rather than tiny type.

## Progress & Proof cleanup

Active/problem work is ordered before normal healthy work. EXPECTED_IDLE/market-closed jobs are collapsed into a single healthy group by default, greatly reducing page height while preserving individual Copy controls and exact evidence access.

## Deliberately not introduced

- No TradingAgents runtime dependency.
- No LLM production decision authority.
- No change to deterministic mathematics, S/R, VWAP, EMA, Supertrend, RSI, MACD, Camarilla, delivery/volume, pattern, breakout/retest or risk logic.
- No weakening of PIT completeness, leakage, purge/embargo, fresh executable quote, Model Paper, WFA or forward-maturity gates.
- No broker execution authority.

## Acceptance still required

Source/static validation is not installed acceptance. R27 must still be installed on the target and prove the off-market lifecycle/read models. Market hours remain required only for the final live quote/freshness/intraday acceptance slice.
