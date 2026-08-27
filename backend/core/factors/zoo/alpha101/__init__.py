"""Alpha101 zoo — Kakushadze (2015) "101 Formulaic Alphas", arXiv:1601.00991.

Pure price/volume factors, ported from the formulas' published
definitions (clean-room reimplementation using our own core/factors
operators, not copied source code). 19 of the original 101 are
sector-neutral and require sector tags we don't have a source for on
our NSE universe yet -- those are explicitly deferred (not ported),
per the build contract. Every factor here must still clear the
purity/lookahead/IC gates before being trusted live; presence in this
directory means "importable", not "validated".
"""
