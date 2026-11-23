#!/usr/bin/env python3
"""
engine/portfolio.py (Oddmaker v15.0 Clean Adaptive Architecture)
Layer 4: ポートフォリオ最適化＆資金配分層
  - 予算連動・厳密 0/1 ナップサック動的計画法 (select_knapsack_portfolio / _select_flexible_portfolio)
  - 100円刻みの厳密トリガミゼロ＆予算超過ゼロ・ガード (trim_picks_for_budget)
  - 2層構造トリガミゼロ本線傾斜配分 (allocate_bets)
"""
import math
from typing import Set, Any, Dict, List, Optional, Tuple

from engine.master_data import MIN_SYNTH_HARD_FLOOR

def calc_synth(odds_list: List[float]) -> float:
    valid = [o for o in odds_list if o > 0]
    if not valid:
        return 0.0
    inv = sum(1.0 / o for o in valid)
    return round(1.0 / inv, 2) if inv > 0 else 0.0


def trim_picks_for_budget(picked: List[Dict], total_budget: int, min_synth: float = 1.65) -> List[Dict]:
    """
    v15.0 実運用予算＆厳密トリガミゼロ・ガード:
      - budget // 100 を超える点数の絞り込みに加え、100円刻みでの
        「全買い目元本割れゼロ成立条件」 sum(ceil(budget / (100 * odds_i)) * 100) <= budget
        を満たすまで下位スコアの買い目を自動で整理する（300円/500円など低予算戦目のトリガミを完全撲滅）。
    """
    if not picked:
        return []
    budget = max(100, (int(total_budget) // 100) * 100)
    max_affordable = max(1, budget // 100)
    trimmed = list(picked[:max_affordable]) if len(picked) > max_affordable else list(picked)

    def _min_breakeven_sum(cands: List[Dict]) -> int:
        tot = 0
        for c in cands:
            o = max(1.01, float(c.get("odds") or 1.01))
            # 10円切り捨て払戻でも payout >= budget を満たす最小100円単位賭け金
            req = max(100, int(math.ceil((budget / o) / 100.0 - 1e-9)) * 100)
            while int((req * o) // 10) * 10 < budget:
                req += 100
            tot += req
        return tot

    while len(trimmed) > 1 and _min_breakeven_sum(trimmed) > budget:
        trimmed.pop()  # スコア降順の末尾（最下位評価）から削減

    if calc_synth([float(c["odds"]) for c in trimmed]) >= min_synth:
        return trimmed
    return trimmed if trimmed else list(picked[:1])


def allocate_bets(
    picked: List[Dict],
    total_budget: int,
    tilt_strength: float = 0.0,
    cover_ratio: float = 0.58,
    breakeven_margin: float = 1.00,
) -> List[Tuple[str, int]]:
    """
    v15.0 傾斜配分（本線厚め・トリガミ防止）＆ 均等配分 統合エンジン:
      - tilt_strength <= 0.0 の場合は従来の合成オッズ均等配分。
      - tilt_strength > 0.0 の場合は2層構造で傾斜配分を実行:
          ① ベース層 (floor_share): 全買い目で最低回収ライン（トリガミ防止: 最低1.05倍以上）を確保
          ② 傾斜層 (1 - floor_share): モデルスコア上位（特に1〜2番手の本線）へ重みを掛けて上乗せ
      - 100円単位丸め後の端数調整も、傾斜倍率 m_i で正規化した達成率 (amt*odds/m_i) 基準で最適配分。
    """
    valid = [c for c in picked if float(c.get("odds") or 0) > 0]
    inv_sum = sum(1.0 / float(c["odds"]) for c in valid)
    if inv_sum <= 0:
        return []
    n = len(valid)
    min_possible = n * 100
    target = max(min_possible, int(round(total_budget / 100.0) * 100))

    if n == 1:
        pair_str = valid[0].get("pair") or f"{valid[0]['w1']}-{valid[0]['w2']}"
        return [(pair_str, target)]

    if tilt_strength <= 0.0:
        items = []
        for c in valid:
            o = float(c["odds"])
            raw = target * ((1.0 / o) / inv_sum)
            amt = max(100, int(round(raw / 100.0) * 100))
            pair_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
            items.append([pair_str, amt, o, 1.0])
    else:
        scores = [float(c.get("_model_score") or 0.0) for c in valid]
        has_sc = all(s > 0 for s in scores)
        ms: List[float] = []
        for idx in range(n):
            if idx == 0:
                r_pos = 1.0
            elif idx == 1:
                r_pos = 0.55 if n >= 3 else -0.50
            else:
                r_pos = -0.50 - 0.50 * ((idx - 2) / float(max(1, n - 2)))
            rank_m = 1.0 + tilt_strength * r_pos
            if has_sc:
                mean_s = sum(scores) / n
                sc_ratio = max(0.5, min(1.8, scores[idx] / max(1e-9, mean_s)))
                sc_m = 1.0 + (tilt_strength * 0.40) * (sc_ratio - 1.0)
                m = 0.70 * rank_m + 0.30 * sc_m
            else:
                m = rank_m
            ms.append(max(0.50, m))

        synth = 1.0 / inv_sum
        min_no_gami_share = min(0.95, 1.12 / max(1.12, synth))
        floor_share = max(min_no_gami_share, min(0.95, float(cover_ratio)))
        surplus_w = [(ms[i] ** 2.2) / (float(valid[i]["odds"]) ** 0.75) for i in range(n)]
        sw_sum = sum(surplus_w) or 1.0

        items = []
        for i, c in enumerate(valid):
            o = float(c["odds"])
            base_amt = target * floor_share * ((1.0 / o) / inv_sum)
            extra_amt = target * (1.0 - floor_share) * (surplus_w[i] / sw_sum)
            safe_target = target * max(1.0, float(breakeven_margin))
            min_safe = max(100, int(math.ceil(safe_target / o / 100.0 - 1e-9) * 100)) if synth >= 1.20 else 100
            amt = max(min_safe, int(round((base_amt + extra_amt) / 100.0) * 100))
            pair_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
            items.append([pair_str, amt, o, ms[i], min_safe])

    cur_sum = sum(x[1] for x in items)
    while cur_sum > target:
        cands = [i for i, x in enumerate(items) if (x[1] - 100 >= (x[4] if len(x) > 4 else 100))]
        if not cands:
            cands = [i for i, x in enumerate(items) if x[1] >= 200]
        if not cands:
            break
        idx = max(cands, key=lambda i: (items[i][1] * items[i][2]) / items[i][3])
        items[idx][1] -= 100
        cur_sum -= 100
    while cur_sum < target:
        idx = min(range(len(items)), key=lambda i: (items[i][1] * items[i][2]) / items[i][3])
        items[idx][1] += 100
        cur_sum += 100

    return [(x[0], x[1]) for x in items]


# ==============================================================================
# 脚質判定（直近最大3走1コーナー通過順の中央値 nearest）＆ 動画5データ＋馬場強重み＋枠バイアス強化モデル

def _select_flexible_portfolio(
    scored_pool: List[Tuple[Dict, float]],
    target_synth: float,
    hard_floor: float = MIN_SYNTH_HARD_FLOOR,
    max_candidates: int = 18,
    mode_group: str = "混合",
    max_picks: int = 10,
    budget: Optional[int] = None,
    min_odds_floor: float = 0.0,
    exclude_top_pop: int = 0,
    max_odds_cap: Optional[float] = None,
    min_score_ratio: Optional[float] = None,
) -> List[Dict]:
    """
    v15.0 2次元離散予算・厳密 0/1 ナップサックDP（T10〜T20 固い人気除外＆中穴〜大穴シフト対応）:
      - T10〜T20 では min_odds_floor / exclude_top_pop により固い1番人気等の混入を構造的に遮断し、
        max_odds_cap・cand_win 拡大により中穴〜大穴ポートフォリオを構築。
      - 候補が0件になる少頭数等の例外時のみ段階的にフィルタを緩和して安全フォールバック。
    """
    if not scored_pool or max_picks <= 0:
        return []
    eff_target = max(hard_floor, float(target_synth))
    eff_min_o = max(eff_target, float(min_odds_floor or 0.0))

    cand_win = max(max_candidates, 18 if eff_target <= 2.8 else 24)
    max_o_cap = float(max_odds_cap) if (max_odds_cap is not None and max_odds_cap > 0) else max(35.0, eff_target * 16.0)
    min_sr = float(min_score_ratio) if (min_score_ratio is not None and min_score_ratio > 0) else (0.08 if eff_target <= 3.0 else 0.04)

    pool_slice = scored_pool[:cand_win]
    filtered_base = [
        (c, sc) for c, sc in pool_slice
        if eff_min_o <= float(c.get("odds") or 0.0) <= max_o_cap
        and (exclude_top_pop <= 0 or int(c.get("pop") or 99) > exclude_top_pop)
    ]
    if filtered_base:
        top_sc = max(sc for _, sc in filtered_base)
        cands = [(c, sc) for c, sc in filtered_base if sc >= top_sc * min_sr]
    else:
        cands = []

    if not cands and eff_min_o > eff_target:
        cands = [(c, sc) for c, sc in pool_slice if eff_min_o <= float(c.get("odds") or 0.0) <= max_o_cap]
    if not cands:
        cands = [(c, sc) for c, sc in pool_slice if float(c.get("odds") or 0.0) >= eff_target]
    if not cands:
        eff_target = hard_floor
        cands = [(c, sc) for c, sc in pool_slice if float(c.get("odds") or 0.0) >= hard_floor]
    if not cands:
        return []

    cap_int = int(math.floor(1000.0 / eff_target + 1e-9))
    best_lst: List[int] = []

    if budget is not None and 100 <= int(budget) <= 1000:
        bud_yen = (int(budget) // 100) * 100
        bud_units = max(1, bud_yen // 100)
        dp2: Dict[Tuple[int, int], Tuple[float, List[int]]] = {(0, 0): (0.0, [])}
        for idx_c, (c, sc) in enumerate(cands):
            o = float(c["odds"])
            w_inv = int(math.ceil(1000.0 / o - 1e-9))
            if w_inv > cap_int:
                continue
            req_u = max(1, int(math.ceil((bud_yen / o) / 100.0 - 1e-9)))
            while int((req_u * 100 * o) // 10) * 10 < bud_yen:
                req_u += 1
            if req_u > bud_units:
                continue
            new_dp2 = dict(dp2)
            for (bu, ci), (cv, lst) in dp2.items():
                if len(lst) >= max_picks:
                    continue
                nbu, nci = bu + req_u, ci + w_inv
                if nbu <= bud_units and nci <= cap_int:
                    nv = cv + sc
                    key = (nbu, nci)
                    prev = new_dp2.get(key)
                    if prev is None or nv > prev[0]:
                        new_dp2[key] = (nv, lst + [idx_c])
            dp2 = new_dp2
        best_lst = max(dp2.values(), key=lambda x: x[0])[1]

    if not best_lst:
        dp1: Dict[int, Tuple[float, List[int]]] = {0: (0.0, [])}
        for idx_c, (c, sc) in enumerate(cands):
            w_int = int(math.ceil(1000.0 / float(c["odds"]) - 1e-9))
            if w_int > cap_int:
                continue
            new_dp1 = dict(dp1)
            for cw, (cv, lst) in dp1.items():
                if len(lst) >= max_picks:
                    continue
                nw = cw + w_int
                if nw <= cap_int:
                    nv = cv + sc
                    prev = new_dp1.get(nw)
                    if prev is None or nv > prev[0]:
                        new_dp1[nw] = (nv, lst + [idx_c])
            dp1 = new_dp1
        best_lst = max(dp1.values(), key=lambda x: x[0])[1]

    best_p: List[Dict] = []
    for idx_c in best_lst:
        c, sc = cands[idx_c]
        c_copy = dict(c)
        c_copy["_model_score"] = sc
        best_p.append(c_copy)
    best_p.sort(key=lambda x: float(x.get("_model_score") or 0.0), reverse=True)

    while len(best_p) > 1 and calc_synth([float(c["odds"]) for c in best_p]) < hard_floor:
        low_i = min(range(len(best_p)), key=lambda idx: float(best_p[idx]["odds"]))
        best_p.pop(low_i)
    if best_p and calc_synth([float(c["odds"]) for c in best_p]) < hard_floor:
        return []
    return best_p


def _select_with_synth_floor(
    ranked_pool: List[Dict],
    target_n: int,
    min_synth: float,
    skip_cheapest_first: bool = False,
) -> List[Dict]:
    if not ranked_pool:
        return []
    work = list(ranked_pool)
    if skip_cheapest_first and len(work) > target_n:
        cheapest_idx = min(range(len(work)), key=lambda idx: work[idx]["odds"])
        work.pop(cheapest_idx)
    scored = [(c, 1.0 / max(1.0, float(c.get("odds") or 10.0))) for c in work]
    picked = _select_flexible_portfolio(scored, target_synth=max(min_synth, MIN_SYNTH_HARD_FLOOR))
    return picked[:max(1, target_n)] if picked else []


def _adjust_synth(pool: List[Dict], init_n: int, lo: float, hi: float) -> Tuple[List[Dict], float]:
    if not pool:
        return [], 0.0
    picked = list(pool[:init_n])
    s = calc_synth([c["odds"] for c in picked])
    idx = len(picked)
    while s > hi and idx < len(pool):
        cand = picked + [pool[idx]]
        cs = calc_synth([c["odds"] for c in cand])
        if cs < lo:
            break
        picked, s = cand, cs
        idx += 1
    while s < lo and len(picked) > 1:
        picked.pop()
        s = calc_synth([c["odds"] for c in picked])
    return picked, s


def _find_gap_idx(combos: List[Dict], ratio: float) -> int:
    for i in range(1, min(len(combos), 14)):
        prev_o, cur_o = combos[i - 1]["odds"], combos[i]["odds"]
        if prev_o > 0 and (cur_o / prev_o) >= ratio:
            return i
    return -1


def lookup_result_combo(combos: List[Dict], win_str: str) -> Tuple[Optional[int], Optional[float]]:
    if not win_str or "-" not in win_str or not combos:
        return None, None
    try:
        a_s, b_s = win_str.split("-")
        w1, w2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
    except ValueError:
        return None, None
    for c in combos:
        if c.get("w1") == w1 and c.get("w2") == w2:
            return int(c.get("pop") or 0) or None, float(c.get("odds") or 0.0) or None
    return None, None



select_knapsack_portfolio = _select_flexible_portfolio
