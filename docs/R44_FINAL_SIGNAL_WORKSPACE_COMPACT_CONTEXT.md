# R44 Final Signal Workspace + Compact Context

R44 is a frontend-only child of the exact R43 archive. It changes the Workspace information hierarchy without changing scanner, decision, risk, database, PIT/WFA, ML, installer, chart, or broker authority.

## Customer contract

- Workspace primary table is **Final Signals**, not research candidates.
- Final Signals requires a Final/Open/Promoted/Actionable lifecycle state and complete positive Entry, Target and SL geometry. Research-only, Prepared/Watch, pending-live-confirmation, and terminal rows do not enter the active Final Signals table.
- Default view is Top 5 by canonical evidence/rank score; Top 10 is optional. Open positions are never hidden merely because they fall outside the view limit.
- All / Intraday / Delivery is a first-class Workspace filter. Mode is a column; stock rows have no second-line descriptive text.
- Final rows display Stock, Mode, Score, LTP, ₹ change, % change, Entry, Target, SL, Age/Timeline, Hit, Next, Status and Net P&L.
- R44 displays the agreed Intraday timing contract (`Entry ≤14:30`, `Flat ≤15:00`) as lifecycle context only. R43 backend bytes are frozen; R44 does not claim to change session-policy enforcement.
- Market/index/sector support is collapsed to one non-scrolling decision rail with market state, SENSEX, NIFTY, VIX, BANK, IT, AUTO, breadth and a visual BULLISH/MIXED/BEARISH context inference.
- Intraday and Delivery scanner health are collapsed into one supporting line beneath Final Signals.

## Research truth retained

R44 does not alter WFA, ML training, alpha validation or production influence. Those remain independently governed and must not be represented as production-ready merely because the Workspace is cleaner.
