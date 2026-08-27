"""Composition facade for Project Laddu GET routes.

Route implementations are grouped by system, stock-intelligence, research and
help domains.  This module owns no endpoint business logic.
"""
from routes_get_system import *
from routes_get_performance import *
from routes_get_system import _index_evidence_scores, _market_depth_contract
from routes_get_stock import *
from routes_get_research import *
from routes_get_help import *
from routes_get_registry import ROUTES, match_prefix
