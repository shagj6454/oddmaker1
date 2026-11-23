#!/usr/bin/env python3
"""
engine/bankroll.py (Oddmaker v15.1 Clean Adaptive Architecture)
Layer 5: 意思決定・見送り最小化・1.75倍モンテカルロ資金管理・シミュレーション層
  - 手動介入なし・直前オッズ変動なしの完全自律設計（本番と過去シミュレーションの100%整合を担保）
  - 1〜3戦目全参戦 ／ 4戦目以降のみ障害・ハンデ・低信頼帯(T15〜T20)除外 ／ クールダウン保護なし
  - LiveTrackTracker による同日・同場・同芝ダートのオンラインベイズ適応（未来リーク完全ゼロ）
"""
import inspect
import json
import math
import re
from datetime import datetime
from typing import Set, Any, Callable, Dict, List, Optional, Tuple

from engine.master_data import (
    CACHE_INDEX_JSON,
    CACHE_LIST_MD,
    DATA_DIR,
    DEEP_DEF_STAGE_IDX,
    DEEP_DEF_SYNTH_CAP,
    DEFENSIVE_ALLOWED_TIERS,
    DEFENSIVE_STAGE_IDX,
    DEFENSIVE_SYNTH_CAP,
    STRICT_DEF_ALLOWED_TIERS,
    STRICT_DEF_STAGE_IDX,
    STRICT_DEF_SYNTH_CAP,
    DIST_2000_LEG_DECAY,
    DRY_RUN_STATE_PATH,
    EXCLUDED_VENUES,
    HARVILLE_PAIR_BLEND,
    INITIAL_BANKROLL,
    JST,
    LEG_POWER_SCALE,
    LOW_TRUST_TIERS,
    MAX_CONCURRENT_LANES,
    MAX_RUN_HISTORY,
    MAX_STAGE_IDX,
    MAX_STEPBACK_STAGE_IDX,
    MODEL_VERSION,
    NAR_PROFILE,
    MIN_SYNTH_HARD_FLOOR,
    PROD_STATE_PATH,
    SKIP_TIERS,
    STAGES,
    TIER_SPECS,
    get_tier_hole_filter,
    TIER_SPEC_MAP,
    V14_TRACK_CHAR_STRENGTH,
    VALUE_MISPRICING_EXP,
    WITHDRAW_AMOUNT,
    WITHDRAW_THRESHOLD,
    ZORO_MARKET_MAX_ODDS,
    ZORO_PENALTY_DEFAULT,
    ZORO_PENALTY_MARKET,
    ZORO_PENALTY_STRONG,
)
from engine.environment import (
    LiveTrackTracker,
    RaceEnvironment,
    build_race_environment,
    calc_environment_waku_adjustment,
)
from engine.scoring import (
    calc_fav1_waku_prob,
    calc_harville_waku_pair_probs,
    calc_waku_leg_scores,
    get_field_size_odds_alpha,
    get_strong_zoro_wakus,
    rank_combos_by_leg_weight,
    sanitize_race_record,
    _score_combos_by_leg_weight,
)
from engine.portfolio import (
    allocate_bets,
    calc_synth,
    lookup_result_combo,
    trim_picks_for_budget,
    _adjust_synth,
    _find_gap_idx,
    _select_flexible_portfolio,
    _select_with_synth_floor,
)

def _make_pick(
    race: Dict,
    picked: List[Dict],
    min_synth: float = MIN_SYNTH_HARD_FLOOR,
    label: str = "",
    bet_label: str = "枠複",
    prob_info: Optional[Dict] = None,
) -> Dict:
    if not picked:
        return {"participate": False, "skip_reason": "対象なし"}
    synth = calc_synth([c["odds"] for c in picked])
    if synth < min_synth:
        return {"participate": False, "skip_reason": f"実合成{synth}倍<{min_synth}倍"}
    win_str = race.get("wf_pair") or ""
    res_pop, res_odds = lookup_result_combo(race.get("real_odds") or [], win_str)
    is_hit = False
    hit_combo = None
    if "-" in win_str:
        a_s, b_s = win_str.split("-")
        w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
        for c in picked:
            if c["w1"] == w1 and c["w2"] == w2:
                is_hit = True
                hit_combo = {
                    "pair": f"{w1}-{w2}",
                    "pop": int(c.get("pop") or 0),
                    "odds": float(c.get("odds") or 0.0),
                }
                break
    pairs = ",".join(f"{c['w1']}-{c['w2']}" for c in picked[:8])
    if len(picked) > 8:
        pairs += ",..."
    surf = race.get("surface") or ""
    cond = race.get("track_condition") or ""
    env_tag = f" {surf}{cond}" if (surf or cond) else ""
    out = {
        "participate": True,
        "bet_type": bet_label,
        "combos_summary": f"{label}{env_tag} [{pairs}]({len(picked)}点/合成{synth}倍)",
        "synth_odds": synth,
        "is_hit": is_hit,
        "hit_combo": hit_combo,
        "result_pair": win_str or "-",
        "result_pop": res_pop,
        "result_odds": res_odds,
        "surface_cond": f"{surf}{cond}",
        "picked": picked,
    }
    if prob_info:
        out["p_waku"] = prob_info["p_waku"]
        out["p_horse"] = prob_info["p_horse"]
        out["tier_id"] = prob_info["tier_id"]
        out["z_breakdown"] = prob_info["z_breakdown"]
    return out




def evaluate_v12_standard(
    race: Dict,
    bet_label: str = "枠連",
    stage_idx: Optional[int] = None,
    live_tracker: Optional[LiveTrackTracker] = None,
) -> Dict:
    """
    v15.1 Clean Adaptive Architecture 統一評価エンジン（完全自律・手動介入なし・直前オッズ変動不使用）:
      1) 当日環境（当日オンライン実測 > 静的事前プロファイル）を RaceEnvironment へ統合
      2) 見送り最小化ゲート:
         - 1〜3戦目: 除外競馬場・オッズ未取得以外は全レース参戦
         - 4戦目以降: 障害戦・ハンデ戦・低信頼帯(T15〜T20)のみ除外し、目標合成上限を 2.25倍へ引き締め
      3) 馬の状態変化（馬体重増減・斤量変化）× 6脚質 × 前走1-2角7/18割合 × コース適性 × 当日環境適応を統合
      4) Exact Harville(55%) × 枠連実オッズ(45%) ＋ バリュー歪み検知 ＋ 予算連動 0/1 ナップサックDPで最適組番を選択
    """
    clean_race = sanitize_race_record(race, recompute_waku_scores=False)
    venue = str(clean_race.get("venue", ""))
    rname = str(clean_race.get("race_name", "")) + " " + str(clean_race.get("race_class", ""))
    surface = str(clean_race.get("surface", "ダート"))
    track_cond = str(clean_race.get("track_condition", "良"))
    distance = int(clean_race.get("distance") or 0)

    if any(ex in venue for ex in EXCLUDED_VENUES):
        return {"participate": False, "skip_reason": "除外競馬場(見送り)"}

    combos = clean_race.get("real_odds") or []
    if len(combos) < 2:
        return {"participate": False, "skip_reason": "オッズ未取得(見送り)"}

    horses_info = clean_race.get("horses_info") or []
    env = build_race_environment(clean_race, horses_info, live_tracker=live_tracker)

    fav1_waku = int(clean_race.get("fav1_waku") or combos[0]["w1"])
    prob_info = calc_fav1_waku_prob(clean_race)
    tan_fav1 = float(clean_race.get("fav1_win_odds") or prob_info.get("fav1_win_odds") or 9.9)
    tid = prob_info["tier_id"]
    spec = TIER_SPEC_MAP[tid]
    _, _, _, mode_group, base_target_synth, axis_tilt, alpha, leg_pow, alloc_tilt = spec

    eff_stage = 0 if stage_idx is None else max(0, int(stage_idx))
    defensive_active = (eff_stage >= DEFENSIVE_STAGE_IDX)
    strict_def_active = (eff_stage >= STRICT_DEF_STAGE_IDX)
    if eff_stage >= DEEP_DEF_STAGE_IDX:
        t_synth = min(base_target_synth, DEEP_DEF_SYNTH_CAP)
    elif strict_def_active:
        t_synth = min(base_target_synth, STRICT_DEF_SYNTH_CAP)
    elif defensive_active:
        t_synth = min(base_target_synth, DEFENSIVE_SYNTH_CAP)
    else:
        t_synth = base_target_synth

    is_obstacle = ("障害" in rname) or ("障害" in surface)
    is_handicap = "ハンデ" in rname
    is_shinba = "新馬" in rname

    if tid in SKIP_TIERS:
        return {"participate": False, "skip_reason": f"常時見送りTier({tid})"}
    if eff_stage == 2 and tid in ("T16", "T17", "T18", "T19", "T20"):
        return {"participate": False, "skip_reason": f"3戦目・極荒れ帯除外({eff_stage+1}戦目/{tid})"}
    if eff_stage >= DEFENSIVE_STAGE_IDX:
        if is_obstacle:
            return {"participate": False, "skip_reason": f"4戦目以降・障害戦除外({eff_stage+1}戦目)"}
        if is_handicap:
            return {"participate": False, "skip_reason": f"4戦目以降・ハンデ戦除外({eff_stage+1}戦目/{tid})"}
        if is_shinba:
            return {"participate": False, "skip_reason": f"4戦目以降・新馬戦除外({eff_stage+1}戦目/{tid})"}
        if strict_def_active:
            if tid not in STRICT_DEF_ALLOWED_TIERS:
                return {"participate": False, "skip_reason": f"5戦目以降・T1-T6厳選除外({eff_stage+1}戦目/{tid})"}
        elif tid not in DEFENSIVE_ALLOWED_TIERS:
            return {"participate": False, "skip_reason": f"4戦目・T1-T9防衛除外({eff_stage+1}戦目/{tid})"}

    # Layer 3: 基礎枠脚質スコア × Layer 2: 当日環境＆馬状態適応マルチプライヤ
    base_waku_scores = calc_waku_leg_scores(
        horses_info, surface=surface, track_condition=track_cond, venue=venue, distance=distance, race_name=rname
    )
    env_mult = calc_environment_waku_adjustment(clean_race, horses_info, env, strength=V14_TRACK_CHAR_STRENGTH)
    waku_leg_scores = {
        k: round(max(0.74, min(1.38, float(base_waku_scores.get(k, 1.0)) * float(env_mult.get(k, 1.0)))), 3)
        for k in base_waku_scores
    }
    strong_zoro_wakus = get_strong_zoro_wakus(horses_info)

    hc = int(clean_race.get("horse_count") or len(horses_info) or 14)
    eff_alpha = alpha + (0.03 if hc <= 11 else (-0.03 if hc >= 17 else 0.0))
    eff_leg_pow = leg_pow * LEG_POWER_SCALE * (DIST_2000_LEG_DECAY if distance >= 2000 else 1.0)

    is_nar = (bet_label == "枠複")
    hole_flt = (
        {"min_odds_floor": 0.0, "exclude_top_pop": 0, "max_odds_cap": 40.0, "cand_win": 18, "min_score_ratio": 0.08, "ev_exp": VALUE_MISPRICING_EXP}
        if defensive_active
        else get_tier_hole_filter(tid)
    )
    eff_harv_blend = float(NAR_PROFILE["harville_blend"]) if is_nar else HARVILLE_PAIR_BLEND
    eff_val_exp = float(NAR_PROFILE["value_exp"]) if is_nar else float(hole_flt.get("ev_exp", VALUE_MISPRICING_EXP))

    harv_pair_probs = calc_harville_waku_pair_probs(horses_info)
    scored_pool = _score_combos_by_leg_weight(
        combos, waku_leg_scores, candidate_limit=36, odds_alpha=eff_alpha, leg_power=eff_leg_pow,
        zoro_penalty=ZORO_PENALTY_DEFAULT, strong_zoro_wakus=strong_zoro_wakus,
        strong_zoro_penalty=ZORO_PENALTY_STRONG, market_zoro_max_odds=ZORO_MARKET_MAX_ODDS, market_zoro_penalty=ZORO_PENALTY_MARKET, fav1_waku=fav1_waku, axis_tilt=axis_tilt,
        harville_pair_probs=harv_pair_probs, harville_blend=eff_harv_blend, value_exp=eff_val_exp,
    )

    stage_budget_ref = int(STAGES[min(eff_stage, MAX_STAGE_IDX)]["early_bet"])
    if is_nar:
        stage_budget_ref = max(200, int(round((stage_budget_ref * float(NAR_PROFILE["bet_scale"])) / 100.0) * 100))
    eff_max_picks = min(10, max(1, stage_budget_ref // 100))
    # v15.1: hole_flt は上で算出済み（4戦目以降は自動OFF）
    picked = _select_flexible_portfolio(
        scored_pool, target_synth=t_synth, hard_floor=MIN_SYNTH_HARD_FLOOR,
        max_candidates=int(hole_flt["cand_win"]),
        mode_group=mode_group, max_picks=eff_max_picks, budget=stage_budget_ref,
        min_odds_floor=float(hole_flt["min_odds_floor"]),
        exclude_top_pop=int(hole_flt["exclude_top_pop"]),
        max_odds_cap=float(hole_flt["max_odds_cap"]),
        min_score_ratio=float(hole_flt["min_score_ratio"]),
    )

    tags = []
    if strict_def_active:
        tags.append("🛡️厳選")
    elif defensive_active:
        tags.append("🛡️防衛")
    if env.live_n >= 2.5 and (abs(env.live_waku_tilt) > 0 or abs(env.live_speed_bias_sec) >= 0.7):
        tags.append("📡適応")
    tag_str = "".join(tags)
    lbl = f"{tid}{tag_str}({fav1_waku}枠:{prob_info['p_waku']}%/単{tan_fav1})"
    res = _make_pick(clean_race, picked, min_synth=MIN_SYNTH_HARD_FLOOR, label=lbl, bet_label=bet_label, prob_info=prob_info)
    if res["participate"]:
        res["mode"] = lbl
        res["bucket"] = f"{tid}({mode_group})"
        res["tier_id"] = tid
        res["alloc_tilt"] = alloc_tilt
        res["defensive_active"] = defensive_active
        res["env_explain"] = " / ".join(env.explain)
        res["live_observed_n"] = env.live_n
    return res


def build_100_strategies(bet_label: str = "枠複") -> List[Tuple[str, Callable[[Dict], Dict]]]:
    """中央・地方共通の100パターン総当たりロジック（v15.1新ロジック＋脚質重視パターン搭載）"""
    strats: List[Tuple[str, Callable[[Dict], Dict]]] = []

    def wf(r: Dict) -> Optional[List[Dict]]:
        if "障害" in str(r.get("race_name", "")):
            return None
        if r.get("odds_kind") not in (None, "wakufuku", "wakuren"):
            return None
        c = r.get("real_odds") or []
        return c if len(c) >= 6 else None

    def by_pops(pops: List[int], no_zoro=False, min_s=1.40, f1_min=0.0, f1_max=999.0, r_lo=1, r_hi=12):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c or not (r_lo <= int(r["race_num"]) <= r_hi):
                return {"participate": False, "skip_reason": "対象外"}
            f1 = float(r.get("fav1_win_odds") or r.get("win_odds") or 0.0)
            if f1_min > 0 and not (f1_min <= f1 <= f1_max):
                return {"participate": False, "skip_reason": "単勝条件外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            return _make_pick(r, [x for x in src if x["pop"] in pops], min_synth=min_s, bet_label=bet_label)
        return fn

    def zoro_fn(mode: str, param1: int, param2: int = 0):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            zoros = [x for x in c if x["w1"] == x["w2"] and x["pop"] <= param1]
            if not zoros:
                return {"participate": False, "skip_reason": "ゾロ目なし"}
            if mode == "only":
                return _make_pick(r, zoros, min_synth=1.50, label="ゾロ目のみ", bet_label=bet_label)
            others = [x for x in c if x["w1"] != x["w2"] and x["pop"] >= param2][:3]
            return _make_pick(r, zoros[:2] + others, min_synth=1.60, label="ゾロ目+", bet_label=bet_label)
        return fn

    def gap_fn(ratio: float, after: bool, n_pick: int):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            gi = _find_gap_idx(c, ratio)
            if gi < 1:
                return {"participate": False, "skip_reason": "断層なし"}
            picked = c[gi : gi + n_pick] if after else c[max(0, gi - n_pick) : gi]
            return _make_pick(r, picked, min_synth=1.50, label=f"断層{'後' if after else '前'}", bet_label=bet_label)
        return fn

    def synth_fn(start_pop: int, init_n: int, lo: float, hi: float, no_zoro=False):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            src = [x for x in c if x["w1"] != x["w2"]] if no_zoro else c
            picked, _ = _adjust_synth([x for x in src if x["pop"] >= start_pop], init_n, lo, hi)
            return _make_pick(r, picked, min_synth=lo, label=f"合成{lo}-{hi}", bet_label=bet_label)
        return fn

    def leg_weighted_fn(min_s: float, fade_fav1: bool):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            fw = int(r.get("fav1_waku") or 0)
            pool = [x for x in c if (x["w1"] != fw and x["w2"] != fw)] if (fade_fav1 and fw > 0) else c
            wls = r.get("waku_leg_scores") or calc_waku_leg_scores(
                r.get("horses_info") or [], str(r.get("surface") or "ダート"),
                str(r.get("track_condition") or "良"), str(r.get("venue") or ""),
            )
            ranked = rank_combos_by_leg_weight(pool, wls, candidate_limit=12, strong_zoro_wakus=get_strong_zoro_wakus(r.get("horses_info") or []))
            picked = _select_with_synth_floor(ranked, 5, min_s)
            return _make_pick(r, picked, min_synth=min_s, label="脚質重み", bet_label=bet_label)
        return fn

    def custom_fn(mode: str, p1: float, p2: float):
        def fn(r: Dict) -> Dict:
            c = wf(r)
            if not c:
                return {"participate": False, "skip_reason": "対象外"}
            if mode == "odds_range":
                picked = [x for x in c if p1 <= x["odds"] <= p2][:6]
            elif mode == "inner":
                picked = [x for x in c if x["w1"] <= 4 and x["w2"] <= 4][:int(p1)]
            elif mode == "outer":
                picked = [x for x in c if x["w1"] >= 5 and x["w2"] >= 5][:int(p1)]
            elif mode == "waku_in":
                picked = [x for x in c if x["w1"] == int(p1) or x["w2"] == int(p1)][:int(p2)]
            else:
                picked = []
            return _make_pick(r, picked, min_synth=1.50, label=mode, bet_label=bet_label)
        return fn

    # v15.1 コア戦略群を先頭に登録
    strats.append(("v15.1_全データ+逃げ先行馬場重み", lambda r: evaluate_v12_standard(r, bet_label)))
    strats.append(("脚質重み_全レース5点(合成2.4+)", leg_weighted_fn(2.40, False)))
    strats.append(("脚質重み_1人気枠外し5点(合成2.5+)", leg_weighted_fn(2.50, True)))

    for lo, hi, nz in [
        (1, 3, False), (1, 4, False), (1, 5, False), (2, 4, False), (2, 5, False), (2, 6, False),
        (2, 6, True), (3, 6, False), (3, 7, False), (3, 8, False), (3, 8, True), (4, 8, False),
        (4, 9, False), (5, 10, False), (6, 12, False), (7, 14, False),
    ]:
        strats.append((f"連続_{lo}-{hi}人気{'(ゾロ除)' if nz else ''}", by_pops(list(range(lo, hi + 1)), no_zoro=nz)))

    for name, pops, nz in [
        ("変則_2,4,5,6,7,8(ゾロ含)", [2, 4, 5, 6, 7, 8], False), ("変則_2,4,5,6,7,8(ゾロ除)", [2, 4, 5, 6, 7, 8], True),
        ("変則_奇数1,3,5,7,9", [1, 3, 5, 7, 9], False), ("変則_偶数2,4,6,8,10", [2, 4, 6, 8, 10], False),
        ("変則_奇数3,5,7,9,11", [3, 5, 7, 9, 11], False), ("変則_偶数4,6,8,10,12", [4, 6, 8, 10, 12], False),
        ("変則_1,4,5,6,7", [1, 4, 5, 6, 7], False), ("変則_1,3,6,8,10", [1, 3, 6, 8, 10], False),
        ("変則_2,3,7,8,9", [2, 3, 7, 8, 9], False), ("変則_2,5,7,9,11", [2, 5, 7, 9, 11], False),
        ("変則_3,4,5,6,12", [3, 4, 5, 6, 12], False), ("変則_1,2,8,9,10", [1, 2, 8, 9, 10], False),
        ("変則_1,5,8,11,14", [1, 5, 8, 11, 14], False), ("変則_3,6,9,12,15", [3, 6, 9, 12, 15], False),
    ]:
        strats.append((name, by_pops(pops, no_zoro=nz)))

    for name, m, p1, p2 in [
        ("ゾロ目のみ(1-6人気)", "only", 6, 0), ("ゾロ目のみ(1-8人気)", "only", 8, 0),
        ("ゾロ目のみ(1-10人気)", "only", 10, 0), ("ゾロ目のみ(1-15人気)", "only", 15, 0),
        ("ゾロ目のみ(全人気)", "only", 36, 0), ("ゾロ目+上位1-3", "plus", 10, 1),
        ("ゾロ目+2-4人気", "plus", 10, 2), ("ゾロ目+3-5人気", "plus", 10, 3),
        ("ゾロ目+5-7人気", "plus", 12, 5), ("ゾロ目+7-9人気", "plus", 15, 7),
    ]:
        strats.append((name, zoro_fn(m, p1, p2)))

    for ratio, after, npick in [
        (1.3, False, 2), (1.3, False, 3), (1.3, True, 3), (1.3, True, 4), (1.4, False, 3), (1.4, True, 3),
        (1.4, True, 4), (1.5, False, 3), (1.5, True, 3), (1.5, True, 5), (1.6, False, 3), (1.6, True, 4),
        (1.8, True, 4), (2.0, True, 5),
    ]:
        strats.append((f"断層{ratio}倍_{'直後' if after else '直前'}{npick}点", gap_fn(ratio, after, npick)))

    for sp, init_n, lo, hi, nz in [
        (1, 3, 1.5, 2.0, False), (2, 4, 1.8, 2.5, False), (2, 4, 1.8, 2.5, True), (2, 4, 2.0, 3.0, False),
        (3, 4, 2.0, 3.0, False), (3, 4, 2.5, 4.0, False), (3, 4, 3.0, 5.0, False), (4, 5, 2.5, 4.0, False),
        (4, 5, 3.0, 5.0, False), (4, 5, 4.0, 6.0, False), (5, 5, 3.5, 5.5, False), (5, 5, 4.0, 7.0, False),
        (6, 6, 5.0, 8.0, False), (7, 6, 6.0, 10.0, False),
    ]:
        strats.append((f"合成{lo}-{hi}({sp}人気~{'ゾロ除' if nz else ''})", synth_fn(sp, init_n, lo, hi, nz)))

    for name, pops, f1_lo, f1_hi in [
        ("本命戦(単~1.8)_1-3人気", [1, 2, 3], 1.0, 1.8), ("本命戦(単~1.8)_2-5人気", [2, 3, 4, 5], 1.0, 1.8),
        ("本命戦(単~1.8)_4-8人気", [4, 5, 6, 7, 8], 1.0, 1.8), ("標準戦(単1.8-2.5)_2-6人気", [2, 3, 4, 5, 6], 1.8, 2.5),
        ("標準戦(単1.8-2.5)_4-9人気", [4, 5, 6, 7, 8, 9], 1.8, 2.5), ("混戦(単2.5+)_1-4人気", [1, 2, 3, 4], 2.5, 99.0),
        ("混戦(単2.5+)_2-6人気", [2, 3, 4, 5, 6], 2.5, 99.0), ("混戦(単2.5+)_4-9人気", [4, 5, 6, 7, 8, 9], 2.5, 99.0),
        ("混戦(単2.5+)_6-12人気", [6, 7, 8, 9, 10, 11, 12], 2.5, 99.0), ("大混戦(単3.5+)_2-6人気", [2, 3, 4, 5, 6], 3.5, 99.0),
        ("大混戦(単3.5+)_5-10人気", [5, 6, 7, 8, 9, 10], 3.5, 99.0), ("大混戦(単3.5+)_7-14人気", [7, 8, 9, 10, 11, 12, 13, 14], 3.5, 99.0),
    ]:
        strats.append((name, by_pops(pops, f1_min=f1_lo, f1_max=f1_hi)))

    for name, pops, r1, r2 in [
        ("前半1-6R_2-6人気", [2, 3, 4, 5, 6], 1, 6), ("前半1-6R_4-9人気", [4, 5, 6, 7, 8, 9], 1, 6),
        ("前半1-6R_6-12人気", [6, 7, 8, 9, 10, 11, 12], 1, 6), ("中盤5-8R_2-6人気", [2, 3, 4, 5, 6], 5, 8),
        ("中盤5-8R_4-9人気", [4, 5, 6, 7, 8, 9], 5, 8), ("後半9-12R_2-6人気", [2, 3, 4, 5, 6], 9, 12),
        ("後半9-12R_4-10人気", [4, 5, 6, 7, 8, 9, 10], 9, 12), ("後半9-12R_6-12人気", [6, 7, 8, 9, 10, 11, 12], 9, 12),
        ("メイン10-12R_3-8人気", [3, 4, 5, 6, 7, 8], 10, 12), ("メイン10-12R_5-12人気", [5, 6, 7, 8, 9, 10, 11, 12], 10, 12),
    ]:
        strats.append((name, by_pops(pops, r_lo=r1, r_hi=r2)))

    for name, m, p1, p2 in [
        ("オッズ帯_5-12倍", "odds_range", 5.0, 12.0), ("オッズ帯_8-20倍", "odds_range", 8.0, 20.0),
        ("オッズ帯_10-30倍", "odds_range", 10.0, 30.0), ("オッズ帯_15-40倍", "odds_range", 15.0, 40.0),
        ("オッズ帯_20-60倍", "odds_range", 20.0, 60.0), ("内枠1-4枠のみ上位4点", "inner", 4, 0),
        ("外枠5-8枠のみ上位4点", "outer", 4, 0),
    ]:
        strats.append((name, custom_fn(m, p1, p2)))

    return strats[:100]



def resolve_next_stage_on_hit(
    stage_idx: int,
    bet: int,
    payout: int,
    set_bet: int,
    set_payout: int,
    synth_odds: float = 0.0,
) -> Tuple[int, int, int, str]:
    """
    v15.1 1.75倍モンテカルロ残債連動ステップバック遷移:
      - セット累計払戻 >= セット累計投資（全回収完了） -> 1戦目へ完全リセット ("HIT")
      - 2戦目以降で払戻がセット累計にわずかに届かなかった場合（未回収的中）:
          残負債 rem = set_bet - set_payout を算出し、STAGES の累計額が rem をカバーする
          最小戦目へ自動ステップバック（必ず現在戦目より浅い戦目へ戻る）し、
          残負債を引き継いで次回的中での完済を狙う ("HIT_STEP_BACK")。
    """
    if set_payout >= set_bet or stage_idx <= 0:
        return 0, 0, 0, "HIT"

    rem = max(0, set_bet - set_payout)
    new_idx = max(0, stage_idx - 2)
    for k in range(stage_idx):
        cum_k = int(STAGES[k].get("cum") or sum(STAGES[j]["early_bet"] for j in range(k + 1)))
        if cum_k >= rem:
            new_idx = k
            break
    # v15.1 安全上限キャップ: 深場的中後も最大7戦目(MAX_STEPBACK_STAGE_IDX=6)まで戻して再事故を防止
    new_idx = min(new_idx, max(0, stage_idx - 1), MAX_STEPBACK_STAGE_IDX)
    cum_cap = int(STAGES[new_idx].get("cum") or sum(STAGES[j]["early_bet"] for j in range(new_idx + 1)))
    return new_idx, min(rem, cum_cap), 0, "HIT_STEP_BACK"




def run_progression_sim(
    races: List[Dict],
    eval_fn: Callable[[Dict], Dict],
    log_skips: bool = False,
    use_live_tracker: bool = True,
) -> Dict:
    """
    1.75倍モンテカルロ13戦追い上げシミュレーション（v15.1 完全自律オンライン環境適応）:
      - 同一開催日・競馬場・芝/ダートの終了済みレース結果を LiveTrackTracker で1Rずつオンライン学習（未来リークゼロ）。
    """
    tracker = LiveTrackTracker() if use_live_tracker else None

    balance = INITIAL_BANKROLL
    withdrawn_total = 0
    withdraw_count = 0
    stage_idx = 0
    set_bet = 0
    set_payout = 0
    total_bet = 0
    total_payout = 0
    hits = 0
    misses = 0
    skips = 0
    stop_losses = 0
    step_backs = 0
    cur_lose = 0
    max_lose = 0
    skip_reasons: Dict[str, int] = {}
    prog_stats: Dict[str, Dict] = {}
    race_logs: List[Dict] = []

    try:
        sig = inspect.signature(eval_fn)
        n_params = len(sig.parameters)
    except Exception:
        n_params = 1

    _wrapped_bet_label: Optional[str] = None
    if eval_fn is not evaluate_v12_standard and n_params == 1:
        code_obj = getattr(eval_fn, "__code__", None)
        if code_obj and "evaluate_v12_standard" in (code_obj.co_names or ()):
            for c_val in (code_obj.co_consts or ()):
                if isinstance(c_val, str) and c_val in ("枠連", "枠複"):
                    _wrapped_bet_label = c_val
                    break
            if _wrapped_bet_label is None:
                _wrapped_bet_label = "枠連"

    for race in races:
        clean_r = sanitize_race_record(race, recompute_waku_scores=False)
        if eval_fn is evaluate_v12_standard:
            ev = evaluate_v12_standard(clean_r, "枠連", stage_idx=stage_idx, live_tracker=tracker)
        elif _wrapped_bet_label is not None:
            ev = evaluate_v12_standard(clean_r, _wrapped_bet_label, stage_idx=stage_idx, live_tracker=tracker)
        elif n_params >= 3:
            ev = eval_fn(clean_r, stage_idx, tracker)
        elif n_params >= 2:
            ev = eval_fn(clean_r, stage_idx)
        else:
            ev = eval_fn(clean_r)

        ts = clean_r.get("timestamp", "")
        rtitle = f"{clean_r.get('venue', '')} {clean_r.get('race_num', '')}R"
        surf_cond = f"{clean_r.get('surface', '')}{clean_r.get('distance', '')}m/{clean_r.get('track_condition', '')}"

        if not ev.get("participate"):
            skips += 1
            rsn = ev.get("skip_reason") or "見送り"
            rsn_key = rsn.split("(")[0]
            skip_reasons[rsn_key] = skip_reasons.get(rsn_key, 0) + 1
            if log_skips:
                race_logs.append({
                    "timestamp": ts, "race_title": rtitle, "bet_type": ev.get("bet_type", "枠連"),
                    "stage_name": STAGES[stage_idx]["name"], "mode_name": rsn,
                    "surface_cond": surf_cond, "combos_summary": "-", "synth_odds": 0.0,
                    "p_waku": 0.0, "p_horse": 0.0, "tier_id": "",
                    "bought_tickets": [], "hit_ticket": None, "status": "SKIP",
                    "total_bet": 0, "payout": 0, "result_pair": clean_r.get("wf_pair") or "-",
                    "result_pop": None, "result_odds": None, "balance": balance + withdrawn_total,
                    "env_explain": "",
                })
            if tracker is not None:
                tracker.observe(clean_r)
            continue

        st_cfg = STAGES[stage_idx]
        raw_budget = int(st_cfg["early_bet"])
        picked = trim_picks_for_budget(ev["picked"], raw_budget, min_synth=MIN_SYNTH_HARD_FLOOR)
        if not picked:
            skips += 1
            skip_reasons["予算絞込不能"] = skip_reasons.get("予算絞込不能", 0) + 1
            if tracker is not None:
                tracker.observe(clean_r)
            continue

        eff_cover = min(0.92, 0.62 + 0.04 * max(0, stage_idx - 2))
        alloc_tilt = float(ev.get("alloc_tilt", 0.35))
        alloc = allocate_bets(picked, raw_budget, tilt_strength=alloc_tilt, cover_ratio=eff_cover)
        bet = sum(a for _, a in alloc)
        synth_val = calc_synth([float(c["odds"]) for c in picked])

        win_str = str(clean_r.get("wf_pair") or "")
        win_pop, win_odds = lookup_result_combo(clean_r.get("real_odds") or [], win_str)
        alloc_map = dict(alloc)
        payout = 0
        hit_ticket = None
        bought_tickets = []
        for c in picked:
            p_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
            amt = int(alloc_map.get(p_str, 0))
            is_h = (p_str == win_str and amt > 0)
            if is_h:
                payout = int((amt * float(c["odds"])) // 10) * 10
                hit_ticket = {"pair": p_str, "pop": c.get("pop"), "odds": c["odds"], "amount": amt, "is_hit": True}
            bought_tickets.append({"pair": p_str, "pop": c.get("pop"), "odds": c["odds"], "amount": amt, "is_hit": is_h})

        flat_alloc = dict(allocate_bets(picked, 1000, tilt_strength=alloc_tilt, cover_ratio=0.62))
        flat_bet = sum(flat_alloc.values())
        flat_pay = 0
        if hit_ticket is not None:
            f_amt = int(flat_alloc.get(hit_ticket["pair"], 0))
            flat_pay = int((f_amt * float(hit_ticket["odds"])) // 10) * 10

        bkt = ev.get("bucket") or "OTHER"
        ps = prog_stats.setdefault(bkt, {"races": 0, "hits": 0, "bet": 0, "payout": 0, "flat_bet": 0, "flat_payout": 0})
        ps["races"] += 1
        ps["bet"] += bet
        ps["payout"] += payout
        ps["flat_bet"] += flat_bet
        ps["flat_payout"] += flat_pay

        total_bet += bet
        total_payout += payout
        set_bet += bet
        set_payout += payout
        balance += (payout - bet)

        stage_label = st_cfg["name"]
        if payout > 0:
            hits += 1
            ps["hits"] += 1
            cur_lose = 0
            new_idx, new_sb, new_sp, status = resolve_next_stage_on_hit(
                stage_idx, bet, payout, set_bet, set_payout, synth_val
            )
            if status == "HIT_STEP_BACK":
                step_backs += 1
            stage_idx, set_bet, set_payout = new_idx, new_sb, new_sp
        else:
            misses += 1
            cur_lose += 1
            if cur_lose > max_lose:
                max_lose = cur_lose
            if stage_idx >= MAX_STAGE_IDX:
                status = "STOP_LOSS"
                stop_losses += 1
                stage_idx = 0
                set_bet = 0
                set_payout = 0
                cur_lose = 0
            else:
                status = "MISS"
                stage_idx += 1

        if balance >= WITHDRAW_THRESHOLD:
            balance -= WITHDRAW_AMOUNT
            withdrawn_total += WITHDRAW_AMOUNT
            withdraw_count += 1

        combos_sum = ", ".join(f"{t['pair']}({t['odds']})" for t in bought_tickets[:4]) + ("..." if len(bought_tickets) > 4 else "")
        race_logs.append({
            "timestamp": ts,
            "race_title": rtitle,
            "bet_type": ev.get("bet_type", "枠連"),
            "stage_name": stage_label,
            "mode_name": ev.get("mode") or ev.get("label") or ev.get("bucket") or "",
            "surface_cond": surf_cond,
            "combos_summary": combos_sum,
            "synth_odds": synth_val,
            "p_waku": ev.get("p_waku", 0.0),
            "p_horse": ev.get("p_horse", 0.0),
            "tier_id": ev.get("tier_id", ""),
            "bought_tickets": bought_tickets,
            "hit_ticket": hit_ticket,
            "status": status,
            "total_bet": bet,
            "payout": payout,
            "result_pair": win_str or "-",
            "result_pop": win_pop,
            "result_odds": win_odds,
            "balance": balance + withdrawn_total,
            "env_explain": ev.get("env_explain", ""),
        })

        if tracker is not None:
            tracker.observe(clean_r)

    n_played = hits + misses
    hit_rate = round(hits / n_played * 100.0, 1) if n_played > 0 else 0.0
    rec_rate = round(total_payout / total_bet * 100.0, 1) if total_bet > 0 else 0.0
    net_profit = total_payout - total_bet
    return {
        "balance": balance,
        "withdrawn_total": withdrawn_total,
        "withdraw_count": withdraw_count,
        "races": n_played,
        "hits": hits,
        "misses": misses,
        "skips": skips,
        "step_backs": step_backs,
        "hit_rate": hit_rate,
        "prog_stats": dict(sorted(prog_stats.items(), key=lambda kv: (0, int(re.match(r"T(\d+)", kv[0]).group(1))) if re.match(r"T(\d+)", kv[0]) else (1, kv[0]))),
        "race_logs": race_logs,
        "daily_stats": {
            "initial_bankroll": INITIAL_BANKROLL,
            "final_balance": balance + withdrawn_total,
            "withdrawn_total": withdrawn_total,
            "played_count": n_played,
            "hit_count": hits,
            "miss_count": misses,
            "skip_count": skips,
            "total_bet": total_bet,
            "total_payout": total_payout,
            "net_profit": net_profit,
            "recovery_rate": rec_rate,
            "max_losing_streak": max_lose,
            "stop_loss_count": stop_losses,
            "skip_breakdown": skip_reasons,
        },
    }


def update_cache_index() -> List[Dict]:
    entries: List[Dict] = []
    for p in sorted(DATA_DIR.glob("raw_cache_*.json"), reverse=True):
        m_nar = re.match(r"raw_cache_(\d{4})_(all|\d{2})_(.+)\.json$", p.name)
        m_jra = re.match(r"raw_cache_jra_(\d{8})\.json$", p.name)
        m_jra_m = re.match(r"raw_cache_jra_(\d{4})_(\d{2})\.json$", p.name)
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
            count = len(data)
            days_count = len({r.get("date_key", "") for r in data if r.get("date_key")}) or 1
        except Exception:
            count, days_count = 0, 0
        if m_nar:
            yr, mo, vn = m_nar.group(1), m_nar.group(2), m_nar.group(3)
            label = f"地方 {yr}年 年間" if mo == "all" else f"地方 {yr}年{int(mo)}月"
            entries.append({"year": int(yr), "month": mo, "venue": vn, "label": label, "races": count, "days": days_count, "file": p.name, "cached": True})
        elif m_jra_m:
            yr, mo = m_jra_m.group(1), m_jra_m.group(2)
            entries.append({"year": int(yr), "month": mo, "venue": "JRA", "label": f"JRA {yr}年{int(mo)}月(土日)", "races": count, "days": days_count, "file": p.name, "cached": True})
        elif m_jra:
            ymd = m_jra.group(1)
            entries.append({"year": int(ymd[:4]), "month": ymd[4:6], "venue": "JRA", "label": f"JRA {ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}", "races": count, "days": 1, "file": p.name, "cached": True})

    json.dump({"updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), "cached_items": entries},
              open(CACHE_INDEX_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    lines = ["# 🟢 v15.1 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象 | 区分 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
    for e in entries:
        lines.append(f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R |")
    open(CACHE_LIST_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return entries


def get_model_config() -> Dict[str, Any]:
    """Python側とindex.htmlのSingle Source of Truth用モデル設定ペイロード"""
    return {
        "version": MODEL_VERSION,
        "initial_bankroll": INITIAL_BANKROLL,
        "withdraw_threshold": WITHDRAW_THRESHOLD,
        "withdraw_amount": WITHDRAW_AMOUNT,
        "defensive_stage": DEFENSIVE_STAGE_IDX + 1,
        "defensive_synth_cap": DEFENSIVE_SYNTH_CAP,
        "strict_def_stage": STRICT_DEF_STAGE_IDX + 1,
        "strict_def_allowed_tiers": list(STRICT_DEF_ALLOWED_TIERS),
        "strict_def_synth_cap": STRICT_DEF_SYNTH_CAP,
        "deep_def_stage": DEEP_DEF_STAGE_IDX + 1,
        "deep_def_synth_cap": DEEP_DEF_SYNTH_CAP,
        "max_stepback_stage": MAX_STEPBACK_STAGE_IDX + 1,
        "max_concurrent_lanes": MAX_CONCURRENT_LANES,
        "zoro_market_max_odds": ZORO_MARKET_MAX_ODDS,
        "zoro_penalty_market": ZORO_PENALTY_MARKET,
        "low_trust_tiers": list(LOW_TRUST_TIERS),
        "stages": [
            {
                "stage": s["stage"],
                "name": s["name"],
                "bet": s["early_bet"],
                "cum": s.get("cum", 0),
                "req_synth": round(s.get("cum", s["early_bet"]) / float(max(1, s["early_bet"])), 2),
            }
            for s in STAGES
        ],
        "tiers": [
            {
                "id": sp[0],
                "min_prob": int(round(sp[1] * 100)),
                "name": sp[2],
                "group": sp[3],
                "target_synth": sp[4],
                "axis_tilt": sp[5],
                "alloc_tilt": sp[8],
                "low_trust": sp[0] in LOW_TRUST_TIERS,
            }
            for sp in TIER_SPECS
        ],
    }


def save_dry_run_dashboard(
    run_id: str, year: int, month: str, week_label: str, venue: str,
    mode_label: str, data_source: str, total_races: int, sim_res: Dict,
    override_prog_stats: Optional[Dict] = None,
) -> None:
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    cached_items = update_cache_index()
    entry = {
        "run_id": run_id, "executed_at": now_str, "year": year, "month": month,
        "week_label": week_label, "venue": venue, "mode_label": mode_label,
        "data_source": data_source, "total_races_fetched": total_races,
        "balance": sim_res.get("balance", sim_res["daily_stats"]["final_balance"]),
        "withdrawn_total": sim_res.get("withdrawn_total", sim_res["daily_stats"].get("withdrawn_total", 0)),
        "withdraw_count": sim_res.get("withdraw_count", 0),
        "daily_stats": sim_res["daily_stats"],
        "prog_stats": override_prog_stats if override_prog_stats is not None else sim_res["prog_stats"],
        "race_logs": sim_res["race_logs"],
    }
    prev_hist = []
    if DRY_RUN_STATE_PATH.exists():
        try:
            old = json.load(open(DRY_RUN_STATE_PATH, "r", encoding="utf-8"))
            prev_hist = old.get("run_history", []) if isinstance(old.get("run_history"), list) else []
        except Exception:
            pass
    new_hist = [h for h in prev_hist if h.get("run_id") != run_id]
    new_hist.insert(0, entry)
    out = {
        **entry, "updated_at": now_str, "bankrupt_count": 0, "set_loss": 0, "stage_idx": 0,
        "active_ticket": None, "model_config": get_model_config(),
        "cached_items": cached_items, "run_history": new_hist[:MAX_RUN_HISTORY],
    }
    json.dump(out, open(DRY_RUN_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))


def _ensure_prod_lanes(state: Dict) -> Dict:
    """本番状態の動的マルチレーンプール（MAX_CONCURRENT_LANES=2）を初期化・同期"""
    if "lanes" not in state or not isinstance(state.get("lanes"), list) or not state["lanes"]:
        state["lanes"] = [{
            "id": 0,
            "stage_idx": int(state.get("stage_idx") or 0),
            "set_bet": int(state.get("set_bet") or 0),
            "set_payout": int(state.get("set_payout") or 0),
            "cur_lose": int(state.get("cur_lose") or 0),
            "pending_race_id": None,
        }]
    state.setdefault("balance", INITIAL_BANKROLL)
    state.setdefault("withdrawn_total", 0)
    state.setdefault("daily_stats", {"total_bet": 0, "total_payout": 0})
    state.setdefault("race_logs", [])
    deepest = max(state["lanes"], key=lambda x: int(x.get("stage_idx") or 0))
    state["stage_idx"] = int(deepest.get("stage_idx") or 0)
    state["set_bet"] = int(deepest.get("set_bet") or 0)
    state["set_payout"] = int(deepest.get("set_payout") or 0)
    state["set_loss"] = max(0, state["set_bet"] - state["set_payout"])
    return state


def load_prod_state() -> Dict:
    if PROD_STATE_PATH.exists():
        try:
            d = json.loads(PROD_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return _ensure_prod_lanes(d)
        except Exception:
            pass
    return _ensure_prod_lanes({
        "version": MODEL_VERSION,
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "set_bet": 0,
        "set_payout": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    })


def save_prod_state(state: Dict) -> None:
    _ensure_prod_lanes(state)
    if isinstance(state.get("race_logs"), list) and len(state["race_logs"]) > 500:
        state["race_logs"] = state["race_logs"][:500]
    state["version"] = MODEL_VERSION
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(PROD_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
