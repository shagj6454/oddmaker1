#!/usr/bin/env python3
"""
core_v12.py — Oddmaker v15.5 Clean Adaptive Architecture Facade
旧3,573行のモノリスを責務別の5層パッケージ (engine/) へ全面再設計・分離した互換ファサード。
手動介入・直前オッズ変動は完全排除し、本番と過去シミュレーションの整合性を担保。
"""
from engine.master_data import *  # noqa: F401, F403
from engine.environment import *  # noqa: F401, F403
from engine.scoring import *      # noqa: F401, F403
from engine.portfolio import *    # noqa: F401, F403
from engine.bankroll import *     # noqa: F401, F403
from engine.scoring import (
    _calc_harville_waku_and_horse_prob,
    _extract_fav2_and_same_waku_odds,
    _extract_rival_wakus,
    _interp_base_place2_prob,
    _logit,
    _lookup_keibalab_course,
    _score_combos_by_leg_weight,
    _sigmoid,
)
from engine.portfolio import (
    _adjust_synth,
    _find_gap_idx,
    _select_flexible_portfolio,
    _select_with_synth_floor,
)
from engine.bankroll import _make_pick


def calc_v1317_track_character_mult(race, strength=V14_TRACK_CHAR_STRENGTH):
    horses = race.get("horses_info") or []
    env = build_race_environment(race, horses)
    return calc_environment_waku_adjustment(race, horses, env, strength=strength)
