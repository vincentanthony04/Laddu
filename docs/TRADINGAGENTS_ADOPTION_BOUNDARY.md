# Project Laddu — TradingAgents Adoption Boundary

## Purpose

TauricResearch/TradingAgents is used as an architectural reference only. R28 does not add the TradingAgents package, LangGraph, a new LLM provider dependency, broker execution, or a parallel decision authority.

## Concepts Laddu may reuse

- checkpoint/resume for long research workflows, bound to exact workflow identity;
- typed/structured research outputs instead of free-text contracts;
- deterministic instrument/company identity before any AI reasoning;
- verified market-data snapshots grounding price/indicator statements;
- explicit analyst/research/risk separation;
- durable decision/reflection logs for research learning;
- provider capability/configuration registries rather than scattered provider conditionals.

## Laddu authorities that remain unchanged

- NSE/BSE cash equities only for production desks; broker authority remains NONE;
- canonical PIT data, PostgreSQL governance, Parquet/DuckDB research, QuestDB time-series and deterministic evidence remain authoritative;
- scanner admission, entry geometry, risk, WFA, settlement, cost/P&L and final Model Paper state remain Laddu-owned deterministic/governed services;
- no Yahoo Finance, live-news snapshot, external agent opinion, or LLM output can overwrite canonical Laddu evidence;
- any future agent layer must be research/critic/explanation-only until separately proven, with production influence zero by default.

## R28 decision

R28 is installer/data-plane closure only. TradingAgents concepts are retained as a post-install acceptance simplification path; no runtime dependency is added while the Windows production install gate is still open.
