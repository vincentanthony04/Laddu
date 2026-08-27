# Project Laddu Terminal Actionable UI R2

## Product intent
The trading surface is a compact market-intelligence terminal. The first customer question is not “which worker is running?” but “which canonical trades are actionable now, and what are the exact risk boundaries?”

## First viewport
1. compact NIFTY / breadth / VIX / system tape;
2. compact LIVE / MARKET CLOSED / NOT ACTIONABLE truth state;
3. Actionable Now list with up to five canonical decisions visible at 1366x768;
4. Watch Next and Recent Outcomes immediately below.

## Actionable columns
`# | Stock / Mode | Setup | Evidence | LTP | Entry | Target | SL | R:R | Signal Age | Holding | Status`

Result / Outcome / After are deliberately not repeated on active/ready trades. They live in Recent Outcomes / Accuracy, where Result remains immutable and After is a separate learning observation.

## Decision truth
- `final_signals` is the sole Actionable Now source.
- No browser-side candidate promotion.
- No browser-generated trade geometry.
- No derived browser “bullish/bearish probability” labels.
- Custom internal chart remains disabled from decision authority.

## Visual authority
The repository skill `.github/skills/laddu-trading-terminal-ui/SKILL.md` and `.github/instructions/laddu-ui-mandatory.instructions.md` are mandatory for future UI changes.
