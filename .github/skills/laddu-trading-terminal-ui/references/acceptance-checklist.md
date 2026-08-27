# Laddu UI Acceptance Checklist

## 3-second test
A user can answer within ~3 seconds:
1. Is the system live/trustworthy?
2. Is the market broadly supportive or adverse?
3. Which stocks are actionable now?
4. What are Entry / Target / SL / R:R?
5. What is waiting next if nothing is actionable?

## Simplification audit
For every visible element ask:
- Does it help understand, decide, act, or verify?
- Is this information already shown elsewhere?
- Could a line/divider replace a card?
- Could a short status replace a sentence?
- Is this customer information or engineering telemetry?
- Does it push Actionable Now below the fold?
If an element fails these questions, remove or demote it.

## Visual quality
- One dominant subject per screen.
- Actionable table has strongest hierarchy.
- No large dead space.
- Row scan path is obvious left-to-right.
- Semantic colors are consistent.
- Numeric columns align cleanly.
- Light/dark both feel designed, not inverted.

## Truth-state tests
- LIVE + fresh
- MARKET CLOSED
- stale evidence
- partial data
- zero actionable
- 1 actionable
- 5 actionable
- active trade
- target hit
- SL hit
- after-state available/unavailable

## Regression
- No horizontal viewport overflow at supported desktop widths; table may have contained horizontal scrolling only when unavoidable.
- No clipped labels or values.
- No layout shift when live values update.
- Focus states visible.
- Screen reader labels for state chips and controls.
