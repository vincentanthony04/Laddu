from __future__ import annotations

MODEL_TOURNAMENT_CONTRACT = {
    "version": "laddu-model-tournament-2.0.0",
    "production_authority": "deterministic_risk_and_post_cost_ev_gate",
    "intraday": {
        "ranker": ["lightgbm_lambdarank", "catboost_ranker"],
        "probability": ["lightgbm_classifier", "catboost_classifier", "elastic_net_logistic"],
        "distribution": ["quantile_lightgbm", "catboost_multi_quantile"],
        "time_to_event": ["discrete_time_hazard", "survival_gbdt"],
        "regime": ["hmm_mixture", "calibrated_regime_classifier"],
        "temporal_challengers": ["patchtst", "temporal_fusion_transformer"],
        "setup_families": [
            "ORB_CONTINUATION", "VWAP_TREND_PULLBACK", "RANGE_REVERSION",
            "GAP_CONTINUATION", "GAP_FAILURE", "BREAKOUT_RETEST", "SECTOR_MOMENTUM",
        ],
    },
    "delivery": {
        "ranker": ["lightgbm_lambdarank", "catboost_ranker"],
        "probability": ["lightgbm_classifier", "catboost_classifier", "elastic_net_logistic"],
        "distribution": ["quantile_lightgbm", "catboost_multi_quantile"],
        "time_to_event": ["discrete_time_hazard", "survival_gbdt"],
        "regime": ["hmm_mixture", "calibrated_regime_classifier"],
        "temporal_challengers": ["patchtst", "temporal_fusion_transformer", "chronos", "timesfm", "moirai"],
        "tabular_challengers": ["tabpfn"],
        "relationship_challengers": ["sector_graph_neural_network"],
        "horizons": ["5_TRADING_DAY", "10_TRADING_DAY", "20_TRADING_DAY", "60_TRADING_DAY"],
    },
    "required_outputs": [
        "cross_sectional_rank", "rank_percentile", "target_before_stop_probability",
        "net_return_q05", "net_return_q50", "net_return_q95", "mae_distribution",
        "mfe_distribution", "time_to_target_distribution", "time_to_stop_distribution",
        "calibrated_probability", "conformal_uncertainty_interval",
    ],
    "promotion_requires": [
        "point_in_time_population", "survivorship_control", "corporate_action_control",
        "purged_walk_forward", "embargo", "frozen_forward_predictions", "post_cost_outcomes",
        "regime_stratification", "liquidity_stratification", "market_cap_stratification",
        "calibration", "multiple_testing_control", "feature_ablation", "seed_stability",
        "capacity_and_turnover", "forward_paper", "rollback_assignment",
    ],
    "research_only": ["reinforcement_learning", "llm_price_authority"],
}
