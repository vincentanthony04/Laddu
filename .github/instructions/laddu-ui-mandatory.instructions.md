---
applyTo: "frontend/**"
---
# Mandatory Project Laddu UI engineering policy

Every frontend change MUST follow `.github/skills/laddu-trading-terminal-ui/SKILL.md` and its acceptance checklist.

## Non-negotiable gates
- Actionable Now is the dominant customer surface; no marketing hero or KPI-card wall above it.
- At 1366x768, market/trust state plus at least five actionable rows (when five exist) must be visible without page scrolling.
- The UI consumes canonical backend authorities. It must not derive Entry, Target, SL, trade status, Result, After, or production admission.
- Missing/stale/partial evidence is displayed as unavailable/not actionable; never synthesize a number/state.
- Result is immutable. After/follow-through is separate.
- Custom internal chart is not part of the decision path; use the external broker chart action unless an explicitly validated chart integration is introduced.
- Semantic colors are functional: green positive/actionable, red risk/failure, amber waiting, cyan/blue live/context, grey unavailable.
- Engineering workers/proof remain in Diagnostics and may not dominate customer trading pages.
- Whole stock rows/stock symbols must be keyboard accessible and open canonical Stock Intelligence.

## Mandatory review before completion
1. Run `validation/verify_terminal_actionable_ui_r2.py` (or its successor).
2. Run customer vertical regression.
3. Run JavaScript syntax validation.
4. Review rendered screenshots at 1366x768, 1600x900, 1920x1080 in dark and light themes.
5. Review actionable, zero-actionable, stale/not-actionable, market-closed and long-text states.
6. Perform the Simplification Audit in the skill checklist.
7. If `web-design-reviewer` and `webapp-testing` skills are available in Copilot, use both before marking UI work complete.

A visually functional page is not accepted if the information hierarchy is wrong.
