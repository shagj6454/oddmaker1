#!/usr/bin/env python3
"""
engine/scoring.py (Oddmaker v14.10 Clean Adaptive Architecture)
Layer 3: データクレンジング・確率・適応型スコアリング層
"""
import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from engine.master_data import (
    EXCLUDED_VENUES,
    ENV_LOGIT_CLAMP,
    ENV_LOGIT_SCALE,
    FRONT7_CONSISTENCY_BONUS,
    FRONT7_RATIO_TH,
    FRONT_BIAS_VENUES,
    HARVILLE_PAIR_BLEND,
    JRA_AXIS_OK_VENUES,
    JRA_COURSE_WAKU_BIAS,
    JRA_G1_NAMES,
    JRA_G2_NAMES,
    JRA_G3_NAMES,
    JRA_UPSET_VENUES,
    KEIBALAB_COURSE_WAKU_BEST5,
    LONG_STRAIGHT_VENUES,
    MULTI_CONTENDER_BONUS,
    MULTI_CONTENDER_MAX_ODDS,
    NAR_AXIS_OK_VENUES,
    NAR_UPSET_VENUES,
    OVERALL_POP_WIN_ROI,
    STRONG_ZORO_MAX_ODDS,
    STRONG_ZORO_MAX_POP,
    TIER_SPECS,
    UPSET_CLASS_KEYWORDS,
    VALUE_MISPRICING_EXP,
    VENUE_POP_WIN_ROI,
    WAKUREN_MKT_BLEND,
    ZORO_MARKET_MAX_ODDS,
    ZORO_PENALTY_DEFAULT,
    ZORO_PENALTY_MARKET,
    ZORO_PENALTY_STRONG,
    _BASE_ODDS_CURVE,
    _BIAS_CURVES,
    _KL_RANK_BONUS,
)
from engine.environment import (
    calc_front7_pos_ratio,
    get_leg_weight,
)

def normalize_text(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_hhmm_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def calc_waku_from_umaban(umaban: int, total_horses: int) -> int:
    """JRA/NAR共通ルール：出走頭数と馬番から枠番(1〜8)を100%一意に算出"""
    if umaban <= 0 or total_horses <= 0:
        return 0
    if total_horses <= 8:
        return min(umaban, 8)
    counts = [1] * 8
    rem = total_horses - 8
    for w in range(7, -1, -1):
        if rem > 0:
            counts[w] += 1
            rem -= 1
    for w in range(7, -1, -1):
        if rem > 0:
            counts[w] += 1
            rem -= 1
    acc = 0
    for idx, cnt in enumerate(counts):
        acc += cnt
        if umaban <= acc:
            return idx + 1
    return 8



def calc_median_nearest(values: List[int]) -> Optional[int]:
    """複数走の通過順から代表値を算出（2走時は四捨五入平均、3走以上は中央値）"""
    valid = sorted(int(v) for v in (values or []) if v and int(v) > 0)
    if not valid:
        return None
    if len(valid) == 2:
        return int(round((valid[0] + valid[1]) / 2.0))
    return valid[len(valid) // 2]


def classify_leg_type(c1_pos: Optional[int], prev_field_size: Optional[int] = 14) -> str:
    """
    1コーナー通過順と出走頭数から脚質を4分類:
      - 逃げ: 1番手（または14頭以上なら2番手以内かつ上位15%）
      - 先行: 上位35%以内（または4番手以内かつ上位55%以内）
      - 差し: 上位70%以内
      - 追込: それより後ろ
    """
    if not c1_pos or c1_pos <= 0:
        return "不明"
    n = max(int(prev_field_size or 14), int(c1_pos), 6)
    ratio = float(c1_pos) / float(n)
    if c1_pos == 1 or (c1_pos == 2 and ratio <= 0.15):
        return "逃げ"
    if ratio <= 0.35 or (c1_pos <= 4 and ratio <= 0.55):
        return "先行"
    if ratio <= 0.70:
        return "差し"
    return "追込"


def clean_race_class_str(race_class: str, race_name: str = "") -> str:
    """
    v13.17: 平場・未勝利・条件戦・障害戦に誤付与された末尾 G1/G2/G3 を除去し、
    JRA実重賞のみ正確なグレードを保持する。
    """
    rc = (race_class or "").strip()
    rn = (race_name or "").strip()
    if not rc:
        return rc
    is_non_open = any(k in rc for k in ("新馬", "未勝利", "1勝", "１勝", "2勝", "２勝", "3勝", "３勝", "障害"))
    cleaned = re.sub(r"\s+G[123]\s*$", "", rc)
    if is_non_open:
        return cleaned
    if any(k in rn for k in JRA_G1_NAMES):
        return f"{cleaned} G1"
    if any(k in rn for k in JRA_G2_NAMES):
        return f"{cleaned} G2"
    if any(k in rn for k in JRA_G3_NAMES):
        return f"{cleaned} G3"
    if ("オープン" in cleaned or "OP" in cleaned) and re.search(r"\s+G[123]\s*$", rc):
        return cleaned
    return rc


def sanitize_horses_info(horses_info: Optional[List[Dict]], keep_scratched: bool = False) -> List[Dict]:
    """
    v13.17 防御的サニタイザ:
      - 馬番の重複レコード（旧スクレイパーで末尾に混入した馬番1〜3等）を先頭優先で除去
      - 取消・除外馬 (win_odds < 1.0) を除外（全頭オッズ未設定時のみユニーク一覧を保持）
      - 初出走馬のダミー prev_field_size=14 を None へ正規化、2走/3走の脚質を統一再判定
    """
    seen: Set[int] = set()
    unique_horses: List[Dict] = []
    for h in horses_info or []:
        u = int(h.get("umaban") or 0)
        if u <= 0 or u in seen:
            continue
        seen.add(u)
        nh = dict(h)
        c1_list = [int(x) for x in (nh.get("prev_c1_list") or []) if x and int(x) > 0]
        if nh.get("prev_c1") is None and not c1_list:
            nh["prev_field_size"] = None
            nh["leg_type"] = "不明"
        else:
            if c1_list:
                nh["prev_c1"] = calc_median_nearest(c1_list)
            c2_list = [int(x) for x in (nh.get("prev_c2_list") or []) if x and int(x) > 0]
            if c2_list:
                nh["prev_c2"] = calc_median_nearest(c2_list)
            c4_list = [int(x) for x in (nh.get("prev_c4_list") or []) if x and int(x) > 0]
            if c4_list:
                nh["prev_c4"] = calc_median_nearest(c4_list)
            nh["leg_type"] = classify_leg_type(nh.get("prev_c1"), nh.get("prev_field_size"))
        unique_horses.append(nh)

    if keep_scratched or not unique_horses:
        return unique_horses

    has_any_odds = any(float(h.get("win_odds") or 0.0) >= 1.0 for h in unique_horses)
    if not has_any_odds:
        return unique_horses
    return [h for h in unique_horses if float(h.get("win_odds") or 0.0) >= 1.0]


def sanitize_race_record(race: Dict, recompute_waku_scores: bool = False) -> Dict:
    """
    v13.17 レースレコード正規化（旧汚染キャッシュを自動修復して安全に評価可能にする）:
      - horses_info の重複馬番＆取消馬を分離除去し、実出走頭数 horse_count を修正
      - fav1_umaban / fav1_waku / fav1_win_odds / fav2_win_odds / fav1_leg_type を再解決
      - race_class 末尾の誤付与 G1/G2 を除去
      - has_scratch を実取消馬有無で厳密再判定
    """
    out = dict(race)
    raw_hi = race.get("horses_info") or []
    all_unique = sanitize_horses_info(raw_hi, keep_scratched=True)
    has_any_odds = any(float(h.get("win_odds") or 0.0) >= 1.0 for h in all_unique)

    if all_unique and has_any_odds:
        active = [h for h in all_unique if float(h.get("win_odds") or 0.0) >= 1.0]
        scratched = [h for h in all_unique if float(h.get("win_odds") or 0.0) < 1.0]
        prev_scratched = [int(x) for x in (race.get("scratched_umabans") or []) if int(x) > 0]
        scratched_ids = sorted(set(prev_scratched + [int(h["umaban"]) for h in scratched]))
        out["horses_info"] = active
        out["scratched_umabans"] = scratched_ids
        out["has_scratch"] = len(scratched_ids) > 0
        out["horse_count"] = len(active)

        ranked = sorted(active, key=lambda x: (float(x["win_odds"]), int(x["umaban"])))
        if ranked:
            f1 = ranked[0]
            out["fav1_umaban"] = int(f1["umaban"])
            out["fav1_waku"] = int(f1["waku"])
            out["fav1_win_odds"] = float(f1["win_odds"])
            out["fav1_leg_type"] = str(f1.get("leg_type") or "不明")
            if len(ranked) >= 2:
                out["fav2_win_odds"] = float(ranked[1]["win_odds"])
    elif all_unique:
        out["horses_info"] = all_unique
        if len(all_unique) > 0:
            out["horse_count"] = len(all_unique)

    out["race_class"] = clean_race_class_str(str(race.get("race_class") or ""), str(race.get("race_name") or ""))

    if recompute_waku_scores and out.get("horses_info"):
        out["waku_leg_scores"] = calc_waku_leg_scores(
            out["horses_info"],
            surface=str(out.get("surface") or "芝"),
            track_condition=str(out.get("track_condition") or "良"),
            venue=str(out.get("venue") or ""),
            distance=int(out.get("distance") or 0),
            race_name=f"{out.get('race_name', '')} {out.get('race_class', '')}",
        )
    return out


# 競馬場ごとの特徴補正（ゼロサム中心設計：±0.05以内の健全なエッジ幅）

def _lookup_keibalab_course(venue: str, surface: str, distance: int, race_name: str = "") -> Optional[Dict]:
    surf = "ダート" if ("ダ" in (surface or "")) else "芝"
    dist = int(distance or 0)
    # 新潟芝2000mは2歳戦・3歳未勝利が内回り、古馬(3歳以上)が外回り
    if venue == "新潟" and surf == "芝" and dist == 2000:
        if any(k in (race_name or "") for k in ("2歳", "２歳", "未勝利", "新馬")):
            return KEIBALAB_COURSE_WAKU_BEST5.get(("新潟", "芝内", 2000))
    return KEIBALAB_COURSE_WAKU_BEST5.get((venue, surf, dist))


def get_course_waku_weights(venue: str, surface: str = "芝", distance: int = 0, race_name: str = "") -> Dict[int, float]:
    """
    v13.17 JRA全10競馬場・競馬ラボ実測枠順データ（全8枠複勝率＋枠順ベスト5順位）統合版 枠番重み係数:
      - 勝利数順位による頭数バイアス（7〜8枠偏重）を抑えるため、全8枠の複勝率偏差を主軸とし、
        ベスト5順位ボーナスを補助加算して8枠平均1.00にゼロサム正規化。
      - 少標本コース(sample='low')は補正幅を0.40倍へベイズ縮小し過学習を防止。
    """
    surf = "ダート" if ("ダ" in (surface or "")) else "芝"
    dist = int(distance or 0)
    spec = JRA_COURSE_WAKU_BIAS.get((venue, surf, dist))
    if not spec:
        if surf == "ダート":
            bias_key = "outer"
        elif any(v in (venue or "") for v in FRONT_BIAS_VENUES) or (dist and dist >= 2000):
            bias_key = "inner"
        else:
            bias_key = "flat"
        spec = {"bias": bias_key}

    base_curve = dict(_BIAS_CURVES.get(spec["bias"], _BIAS_CURVES["flat"]))
    for w, delta in (spec.get("boost") or {}).items():
        if w in base_curve:
            base_curve[w] = round(max(0.84, min(1.18, base_curve[w] + delta)), 3)

    kl = _lookup_keibalab_course(venue, surf, dist, race_name=race_name)
    if not kl:
        return base_curve

    best5 = kl.get("best5") or []
    all8 = kl.get("all8")
    shrink = 0.40 if kl.get("sample") == "low" else 1.00
    rank_by_w = {w: idx for idx, (w, _) in enumerate(best5[:5]) if 1 <= w <= 8}

    kl_curve: Dict[int, float] = {}
    if all8 and len(all8) == 8:
        mean_p3 = sum(all8.values()) / 8.0
        for w in range(1, 9):
            p3 = float(all8[w])
            # 複勝率偏差（±10%差で±0.065、最大±0.095に制限）＋ ベスト5順位ボーナス
            rate_delta = max(-0.095, min(0.095, ((p3 - mean_p3) / 100.0) * 0.65))
            r_idx = rank_by_w.get(w)
            rank_delta = _KL_RANK_BONUS[r_idx] if r_idx is not None else -0.012
            kl_curve[w] = 1.0 + (rate_delta + rank_delta) * shrink
        # 通常施行コースは競馬ラボ実測100%採用（古い一律バイアスでの逆転を防止）、少標本のみ50%ブレンド
        blend = 1.00 if kl.get("sample") == "high" else 0.50
    elif best5:
        is_full = len(best5) >= 4
        avg_p3 = sum(p for _, p in best5) / len(best5)
        ref_p3 = max(18.0, min(30.0, avg_p3))
        for w in range(1, 9):
            kl_curve[w] = 1.0 - (0.018 * shrink if is_full else 0.0)
        for idx, (w, p3) in enumerate(best5[:5]):
            if not (1 <= w <= 8):
                continue
            rate_delta = max(-0.065, min(0.085, ((p3 - ref_p3) / 100.0) * 0.55))
            kl_curve[w] = 1.0 + (rate_delta + _KL_RANK_BONUS[idx]) * shrink
        blend = 1.00 if (is_full and kl.get("sample") == "high") else 0.45
    else:
        return base_curve

    # ゼロサム正規化（8枠平均を 1.000 に揃える）
    mean_kl = sum(kl_curve.values()) / 8.0
    if mean_kl > 0:
        kl_curve = {w: v / mean_kl for w, v in kl_curve.items()}

    merged: Dict[int, float] = {}
    for w in range(1, 9):
        val = (1.0 - blend) * base_curve[w] + blend * kl_curve[w]
        merged[w] = round(max(0.85, min(1.18, val)), 3)
    return merged

# ==============================================================================
# 動画データ：JRA全10競馬場 × 人気別 単勝回収率 (2011年〜2020年)
# ==============================================================================

def get_pop_roi_weight(venue: str, win_pop: int) -> float:
    """競馬場 × 単勝人気別単勝回収率（動画データ）から緩やかな期待値補正係数 (0.86〜1.09) を算出"""
    p = int(win_pop or 0)
    if p <= 0:
        return 1.0
    p_key = min(18, max(1, p))
    ov = OVERALL_POP_WIN_ROI.get(p_key, 75.0)
    v_tbl = None
    for vk, tbl in VENUE_POP_WIN_ROI.items():
        if vk in (venue or ""):
            v_tbl = tbl
            break
    if v_tbl is not None:
        v_roi = v_tbl.get(p_key, ov)
        blend = (0.60 * v_roi + 0.40 * ov) if p_key <= 9 else (0.30 * v_roi + 0.70 * ov)
    else:
        blend = ov

    ratio = 1.0 + ((blend - 78.0) / 78.0) * 0.28
    if p_key >= 10:
        ratio *= 0.96 if p_key <= 12 else 0.92
    return round(max(0.86, min(1.09, ratio)), 3)


def calc_waku_leg_scores(
    horses_info: List[Dict],
    surface: str = "ダート",
    track_condition: str = "良",
    venue: str = "",
    distance: int = 0,
    race_name: str = "",
) -> Dict[str, float]:
    """
    v13.17 前走1・2角(7/18以内割合) × コース適性脚質 × 展開ペース ゼロサム枠スコア (0.80〜1.25):
      - 新馬戦など前走不明馬が50%以上のレースでは脚質ノイズを自動減衰し、枠バイアス＋市場オッズを重視。
      - 逃げ馬頭数＋前走7/18以内ポジション馬比率から展開ペース(H/M/S)を動的判定。
      - 各馬の単勝支持度 (1 / win_odds^0.85) で加重平均し、有効全枠平均=1.000にゼロサム正規化。
    """
    clean_horses = sanitize_horses_info(horses_info)
    if not clean_horses:
        return {str(w): 1.0 for w in range(1, 9)}

    unk_ratio = sum(1 for h in clean_horses if h.get("leg_type") in (None, "", "不明")) / float(len(clean_horses))
    leg_conf = 0.25 if unk_ratio >= 0.50 else (0.70 if unk_ratio >= 0.25 else 1.00)

    odds_ranked = sorted(
        [(float(h.get("win_odds") or 0.0), int(h.get("umaban") or 0)) for h in clean_horses if float(h.get("win_odds") or 0.0) >= 1.0],
        key=lambda x: x[0],
    )
    pop_by_uma = {uma: idx + 1 for idx, (_, uma) in enumerate(odds_ranked)}

    nige_count = sum(1 for h in clean_horses if h.get("leg_type") == "逃げ")
    known_pos = [calc_front7_pos_ratio(h)[0] for h in clean_horses]
    known_pos = [x for x in known_pos if x is not None]
    front_ratio = (sum(1 for x in known_pos if x <= FRONT7_RATIO_TH) / float(len(known_pos))) if known_pos else 0.38
    if nige_count >= 4 or (nige_count >= 3 and front_ratio >= 0.55):
        pace_mode = "H"
    elif nige_count <= 1 and front_ratio <= 0.30:
        pace_mode = "S"
    else:
        pace_mode = "M"

    by_waku: Dict[int, List[Tuple[float, float]]] = {}
    for h in clean_horses:
        w = int(h.get("waku") or 0)
        if not (1 <= w <= 8):
            continue
        uma = int(h.get("umaban") or 0)
        lt = h.get("leg_type")
        c1 = h.get("prev_c1")
        if not c1 and h.get("prev_c1_list"):
            c1 = calc_median_nearest(h.get("prev_c1_list"))
        c2 = h.get("prev_c2")
        if not c2 and h.get("prev_c2_list"):
            c2 = calc_median_nearest(h.get("prev_c2_list"))
        c4 = h.get("prev_c4")
        if not c4 and h.get("prev_c4_list"):
            c4 = calc_median_nearest(h.get("prev_c4_list"))
        pn = int(h.get("prev_field_size") or 14)
        if not lt or lt == "不明":
            lt = classify_leg_type(c1, pn)
        win_pop = int(h.get("win_pop") or pop_by_uma.get(uma, 0))
        win_odds = float(h.get("win_odds") or 0.0)

        raw_wt = get_leg_weight(
            lt, surface=surface, track_condition=track_condition,
            venue=venue, distance=distance, prev_c4=c4, prev_field_size=pn,
            prev_c1=c1, prev_c2=c2, pace_mode=pace_mode, race_name=race_name,
        )
        wt = 1.0 + (raw_wt - 1.0) * leg_conf
        if win_pop > 0:
            wt *= get_pop_roi_weight(venue, win_pop)
        support = (1.0 / (max(1.2, win_odds) ** 0.85)) if win_odds >= 1.0 else 0.15
        by_waku.setdefault(w, []).append((wt, support))

    course_w = get_course_waku_weights(venue, surface=surface, distance=distance, race_name=race_name)
    raw_scores: Dict[int, float] = {}
    for w in range(1, 9):
        items = by_waku.get(w)
        if items:
            tot_sup = sum(s for _, s in items)
            leg_s = sum(wt * s for wt, s in items) / tot_sup if tot_sup > 0 else 1.0
        else:
            leg_s = 1.0
        raw_scores[w] = leg_s * course_w.get(w, 1.0)

    active_ws = [w for w in range(1, 9) if w in by_waku]
    mean_s = (sum(raw_scores[w] for w in active_ws) / float(len(active_ws))) if active_ws else 1.0
    scores: Dict[str, float] = {}
    for w in range(1, 9):
        val = (raw_scores[w] / mean_s) if mean_s > 0 else 1.0
        scores[str(w)] = round(max(0.80, min(1.25, val)), 3)

    # v13.17 新機能A: 直近3走「前走1・2角 7/18以内」一貫性（再現率）ボーナス
    if FRONT7_CONSISTENCY_BONUS > 0.0:
        f7_by_waku: Dict[int, List[Tuple[float, float]]] = {}
        for h in clean_horses:
            w = int(h.get("waku") or 0)
            c1l = h.get("prev_c1_list") or []
            pn = int(h.get("prev_field_size") or 14)
            wo = float(h.get("win_odds") or 0.0)
            if 1 <= w <= 8 and c1l and pn > 0 and wo >= 1.0:
                f7_rate = sum(1 for c in c1l if (float(c) / float(pn)) <= FRONT7_RATIO_TH) / float(len(c1l))
                f7_by_waku.setdefault(w, []).append((f7_rate, 1.0 / (max(1.2, wo) ** 0.85)))
        for w, items in f7_by_waku.items():
            ts = sum(s for _, s in items)
            if ts > 0:
                avg_f7 = sum(r_ * s for r_, s in items) / ts
                scores[str(w)] = round(max(0.78, min(1.30, scores[str(w)] * (1.0 + FRONT7_CONSISTENCY_BONUS * (avg_f7 - 0.40)))), 3)

    # v13.17 新機能B: 同枠・複数有力馬（単勝18倍以下2頭以上）マルチランナー相乗ボーナス
    if MULTI_CONTENDER_BONUS > 0.0:
        contender_cnt: Dict[int, int] = {}
        for h in clean_horses:
            w = int(h.get("waku") or 0)
            wo = float(h.get("win_odds") or 999.0)
            if 1 <= w <= 8 and 1.0 <= wo <= MULTI_CONTENDER_MAX_ODDS:
                contender_cnt[w] = contender_cnt.get(w, 0) + 1
        for w, cnt in contender_cnt.items():
            if cnt >= 2:
                scores[str(w)] = round(max(0.78, min(1.32, scores[str(w)] * (1.0 + MULTI_CONTENDER_BONUS))), 3)

    return scores


def get_field_size_odds_weight(horse_count: int) -> float:
    n = int(horse_count or 0)
    if n <= 0:
        return 1.00
    if n <= 8:
        return 0.88
    if n <= 11:
        return 0.93
    if n <= 14:
        return 1.00
    if n <= 16:
        return 1.07
    return 1.12


def get_field_size_odds_alpha(horse_count: int) -> float:
    n = int(horse_count or 0)
    if n <= 0 or (12 <= n <= 14):
        return 1.00
    if n <= 11:
        return 1.10
    return 0.92


# ==============================================================================
# v13.17 ハービル精密キャリブレーション済み ロジット型1番人気『枠』連対確率モデル
#   ・全出走馬の単勝オッズから exact Harville 枠連対確率を算出（同枠複数馬の連対確率を数学的に厳密反映）
#   ・A/B構造分割を廃止し、予測連対率 p_waku を細分化した純粋20段階 Tier (T1〜T20) へ移行
#   ・枠連1番人気の無条件除外を廃止し、合成オッズ下限 1.80倍（T1:1.80〜T20:3.32）を満たす柔軟点数選択を採用
# ==============================================================================
_BASE_ODDS_CURVE: List[Tuple[float, float]] = [
    (1.0,  0.920),
    (1.15, 0.895),
    (1.30, 0.855),
    (1.50, 0.805),
    (1.75, 0.755),
    (2.05, 0.695),
    (2.40, 0.625),
    (2.80, 0.565),
    (3.30, 0.505),
    (4.00, 0.435),
    (5.00, 0.365),
    (7.00, 0.265),
    (12.0, 0.160),
    (20.0, 0.100),
]


def _interp_base_place2_prob(tan_odds: float) -> float:
    o = max(1.0, float(tan_odds or 3.0))
    if o <= _BASE_ODDS_CURVE[0][0]:
        return _BASE_ODDS_CURVE[0][1]
    for i in range(1, len(_BASE_ODDS_CURVE)):
        x0, y0 = _BASE_ODDS_CURVE[i - 1]
        x1, y1 = _BASE_ODDS_CURVE[i]
        if o <= x1:
            t = (o - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return _BASE_ODDS_CURVE[-1][1]


def _logit(p: float) -> float:
    p_c = max(0.01, min(0.99, p))
    return math.log(p_c / (1.0 - p_c))


def _sigmoid(z: float) -> float:
    if z >= 20:
        return 0.9999
    if z <= -20:
        return 0.0001
    return 1.0 / (1.0 + math.exp(-z))


def _extract_fav2_and_same_waku_odds(race: Dict, fav1_waku: int, fav1_odds: float) -> Tuple[float, List[float]]:
    horses = sanitize_horses_info(race.get("horses_info") or [])
    all_odds: List[Tuple[float, int, int]] = []
    for h in horses:
        o = float(h.get("win_odds") or 0.0)
        if o >= 1.0:
            all_odds.append((o, int(h.get("umaban") or 0), int(h.get("waku") or 0)))
    all_odds.sort(key=lambda x: x[0])

    fav2_odds = float(race.get("fav2_win_odds") or 0.0)
    if len(all_odds) >= 2:
        fav2_odds = all_odds[1][0]

    fav1_uma = int(race.get("fav1_umaban") or (all_odds[0][1] if all_odds else 0))
    same_waku_others: List[float] = []
    skipped_fav1 = False
    for o, uma, w in all_odds:
        if w != fav1_waku:
            continue
        if (not skipped_fav1) and ((fav1_uma > 0 and uma == fav1_uma) or abs(o - fav1_odds) < 0.05):
            skipped_fav1 = True
            continue
        same_waku_others.append(o)

    return fav2_odds, same_waku_others


def classify_race_structure(race: Dict) -> Tuple[str, float]:
    """後方互換ユーティリティ（v13.17のTier判定ではA/B分割を廃止し、参照用指標のみ算出）"""
    horses = sanitize_horses_info(race.get("horses_info") or [])
    all_odds = sorted(
        float(h.get("win_odds") or 0.0)
        for h in horses
        if float(h.get("win_odds") or 0.0) >= 1.0
    )
    rivals = all_odds[1:]
    m = len(rivals)
    if m < 4:
        return "A", 0.0
    inv_r = [1.0 / o for o in rivals]
    tot_inv = sum(inv_r)
    if tot_inv <= 0:
        return "A", 0.0
    probs = [x / tot_inv for x in inv_r]
    h_norm = (-sum(p * math.log(p) for p in probs if p > 0)) / math.log(m)
    chaos = round(max(-1.5, min(1.5, (h_norm - 0.875) / 0.06)), 3)
    return ("B" if chaos > 0.0 else "A"), chaos


def _extract_rival_wakus(race: Dict, fav1_waku: int) -> List[int]:
    horses = sanitize_horses_info(race.get("horses_info") or [])
    ranked = sorted(
        [(float(h.get("win_odds") or 0.0), int(h.get("waku") or 0))
         for h in horses
         if float(h.get("win_odds") or 0.0) >= 1.0 and 1 <= int(h.get("waku") or 0) <= 8],
        key=lambda x: x[0],
    )
    rivals: List[int] = []
    for _, w in ranked:
        if w != fav1_waku and w not in rivals:
            rivals.append(w)
            if len(rivals) >= 2:
                break
    if len(rivals) < 2:
        for c in (race.get("real_odds") or []):
            for w in (int(c.get("w1") or 0), int(c.get("w2") or 0)):
                if 1 <= w <= 8 and w != fav1_waku and w not in rivals:
                    rivals.append(w)
                    if len(rivals) >= 2:
                        break
            if len(rivals) >= 2:
                break
    return rivals


def _calc_harville_waku_and_horse_prob(
    horses: List[Dict],
    fav1_waku: int,
    fav1_uma: int,
    tan_fav1: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    v13.17 Exact Harville 枠連対確率:
      全有効出走馬の単勝暗示確率 q_i = (1/o_i) / Σ(1/o_j) から
        P(枠Wが1着または2着) = 1 - Σ_{a∉W} q_a * (Q_{∉W} - q_a) / (1 - q_a)
      を厳密に算出する（同枠2頭・3頭ケースや相手頭数差をバイアスなく反映）。
    """
    valid_h = [h for h in horses if float(h.get("win_odds") or 0.0) >= 1.0 and 1 <= int(h.get("waku") or 0) <= 8]
    if len(valid_h) < 3 or not (1 <= fav1_waku <= 8):
        return None, None
    raw_q = [1.0 / float(h["win_odds"]) for h in valid_h]
    s_q = sum(raw_q)
    if s_q <= 0:
        return None, None
    qs = [x / s_q for x in raw_q]

    q_not_w = sum(q for h, q in zip(valid_h, qs) if int(h["waku"]) != fav1_waku)
    p_miss = sum(
        q * max(0.0, q_not_w - q) / max(1e-9, 1.0 - q)
        for h, q in zip(valid_h, qs)
        if int(h["waku"]) != fav1_waku
    )
    p0_waku = max(0.05, min(0.96, 1.0 - p_miss))

    p0_horse = None
    for h, q in zip(valid_h, qs):
        if (fav1_uma > 0 and int(h.get("umaban") or 0) == fav1_uma) or (
            fav1_uma <= 0 and int(h.get("waku") or 0) == fav1_waku and abs(float(h["win_odds"]) - tan_fav1) < 0.05
        ):
            p0_horse = q + sum(qk * q / max(1e-9, 1.0 - qk) for hk, qk in zip(valid_h, qs) if hk is not h)
            p0_horse = max(0.03, min(0.95, p0_horse))
            break
    return p0_waku, p0_horse


def calc_fav1_waku_prob(race: Dict) -> Dict:
    """
    v13.17 ハービル精密キャリブレーション版 1番人気『枠』実効連対確率モデル:
      - 全出走馬の単勝オッズから exact Harville 枠連対確率 p0_waku を算出し、
        旧モデルで発生していた全帯域での 8〜13% 過小評価（特に同枠複数馬での過小評価）を根本解消。
      - 環境6ファクター（クラス・場・頭数・脚質4角・コース枠バイアス・断層）のゼロサム補正を
        logit空間で 0.45倍・±0.35クランプで加算し、実測連対率と一致する p_waku を得る。
      - A/B分割を廃止し、p_waku 単独で T1〜T20 の20段階へ細分化。
    """
    clean_race = sanitize_race_record(race)
    horses = clean_race.get("horses_info") or []

    venue = str(clean_race.get("venue", ""))
    rname = str(clean_race.get("race_name", "")) + " " + str(clean_race.get("race_class", ""))
    is_jra = venue in (JRA_AXIS_OK_VENUES + JRA_UPSET_VENUES)
    surface = str(clean_race.get("surface") or ("芝" if is_jra else "ダート"))
    is_dirt = ("ダ" in surface)
    track_cond = str(clean_race.get("track_condition") or "良")
    distance = int(clean_race.get("distance") or 0)
    horse_count = int(clean_race.get("horse_count") or len(horses) or 0)
    tan_fav1 = float(clean_race.get("fav1_win_odds") or 3.0)
    fav1_waku = int(clean_race.get("fav1_waku") or 0)
    fav1_leg = str(clean_race.get("fav1_leg_type") or "不明")

    fav1_uma = int(clean_race.get("fav1_umaban") or 0)
    fav1_c1 = fav1_c2 = fav1_c4 = None
    fav1_pn = 14
    for h in horses:
        if (fav1_uma > 0 and int(h.get("umaban") or 0) == fav1_uma) or (
            fav1_uma <= 0 and int(h.get("waku") or 0) == fav1_waku and abs(float(h.get("win_odds") or 0.0) - tan_fav1) < 0.05
        ):
            fav1_c1 = h.get("prev_c1") or (calc_median_nearest(h.get("prev_c1_list")) if h.get("prev_c1_list") else None)
            fav1_c2 = h.get("prev_c2") or (calc_median_nearest(h.get("prev_c2_list")) if h.get("prev_c2_list") else None)
            fav1_c4 = h.get("prev_c4") or (calc_median_nearest(h.get("prev_c4_list")) if h.get("prev_c4_list") else None)
            fav1_pn = int(h.get("prev_field_size") or 14)
            break

    is_handicap = ("ハンデ" in rname)
    is_listed = ("(L)" in rname) or ("（L）" in rname) or ("リステッド" in rname)
    is_g1 = bool(re.search(r"(?:G1|GⅠ|GI)(?![IⅠ0-9])", rname))
    is_g2 = bool(re.search(r"(?:G2|GⅡ|GII)(?![IⅠ0-9])", rname))
    is_g3 = bool(re.search(r"(?:G3|GⅢ|GIII)(?![0-9])", rname))
    is_open = ("オープン" in rname) or ("OPEN" in rname.upper()) or ("OP" in rname)

    dz1 = 0.0
    if is_jra:
        if ("新馬" in rname) or ("未勝利" in rname):
            dz1 = 0.12
        elif ("1勝" in rname) or ("１勝" in rname) or ("2勝" in rname) or ("２勝" in rname):
            dz1 = 0.00
        elif ("3勝" in rname) or ("３勝" in rname):
            dz1 = -0.14
        elif is_dirt:
            if is_listed:
                dz1 = -0.38
            elif is_open or is_g3:
                dz1 = -0.30
            elif is_g1 or is_g2:
                dz1 = -0.15
        else:
            if is_g1 or is_g3 or (is_open and not is_listed):
                dz1 = -0.36
            elif is_g2 or is_listed:
                dz1 = -0.18
        if is_handicap:
            dz1 -= 0.25

    dz2 = 0.0
    if is_jra:
        if not is_dirt:
            if "東京" in venue:
                dz2 = 0.16
            elif any(v in venue for v in ("札幌", "京都", "阪神", "中山")):
                dz2 = 0.03
            elif any(v in venue for v in ("福島", "小倉")):
                dz2 = -0.22
            elif any(v in venue for v in ("函館", "新潟", "中京")):
                dz2 = -0.14
        else:
            dz2 = 0.04 if not (is_open or is_g1 or is_g2 or is_g3 or is_listed) else -0.04
        for vk, tbl in VENUE_POP_WIN_ROI.items():
            if vk in venue:
                v1_roi = tbl.get(1, 77.4)
                dz2 += round((v1_roi - 77.4) * 0.012, 3)
                break
        kl_meta = _lookup_keibalab_course(venue, surface, distance, race_name=rname)
        if kl_meta and kl_meta.get("fav1_rel"):
            rel = kl_meta["fav1_rel"]
            if rel == "高":
                dz2 += 0.14
            elif rel == "低":
                dz2 -= 0.18
    else:
        if any(v in venue for v in NAR_AXIS_OK_VENUES):
            dz2 = 0.24
        elif any(v in venue for v in NAR_UPSET_VENUES):
            dz2 = 0.00

    if horse_count <= 0:
        dz3 = 0.0
    elif horse_count <= 8:
        dz3 = 0.25
    elif horse_count <= 11:
        dz3 = 0.12
    elif horse_count <= 14:
        dz3 = 0.00
    elif horse_count <= 16:
        dz3 = -0.12
    else:
        dz3 = -0.22

    w_leg = get_leg_weight(
        fav1_leg, surface=surface, track_condition=track_cond,
        venue=venue, distance=distance, prev_c4=fav1_c4, prev_field_size=fav1_pn,
        prev_c1=fav1_c1, prev_c2=fav1_c2, race_name=rname,
    )
    dz4 = 1.15 * (w_leg - 1.02)
    nige_count = sum(1 for h in horses if h.get("leg_type") == "逃げ")
    if nige_count >= 4:
        if fav1_leg == "逃げ":
            dz4 -= 0.20
        elif fav1_leg == "差し":
            dz4 += 0.12

    course_w = get_course_waku_weights(venue, surface=surface, distance=distance, race_name=rname)
    w_course = course_w.get(fav1_waku, 1.0) if 1 <= fav1_waku <= 8 else 1.0
    dz5 = 1.60 * (w_course - 1.00)

    fav2_odds, same_waku_others = _extract_fav2_and_same_waku_odds(clean_race, fav1_waku, tan_fav1)
    dz6 = 0.0
    r21 = (fav2_odds / tan_fav1) if (fav2_odds >= 1.0 and tan_fav1 >= 1.0) else 0.0
    if r21 >= 2.2:
        dz6 = 0.16
    elif r21 >= 1.5:
        dz6 = 0.00
    elif 0.0 < r21 < 1.3:
        dz6 = -0.18

    raw_env = dz1 + dz2 + dz3 + dz4 + dz5 + dz6
    dz_env = max(-ENV_LOGIT_CLAMP, min(ENV_LOGIT_CLAMP, raw_env * ENV_LOGIT_SCALE))

    harv_waku, harv_horse = _calc_harville_waku_and_horse_prob(horses, fav1_waku, fav1_uma, tan_fav1)
    strong_partner = any(o_other <= 12.0 for o_other in same_waku_others)

    if harv_waku is not None:
        p0_waku = harv_waku
        p0_horse = harv_horse if harv_horse is not None else _interp_base_place2_prob(tan_fav1)
        z0 = _logit(p0_waku)
        p_horse = _sigmoid(_logit(p0_horse) + dz_env)
        p_waku = max(0.05, min(0.96, _sigmoid(z0 + dz_env)))
    else:
        p0_horse = _interp_base_place2_prob(tan_fav1)
        z0 = _logit(p0_horse)
        p_horse = _sigmoid(z0 + dz_env)
        miss_prob = 1.0 - p_horse
        for o_other in same_waku_others:
            p_j = _interp_base_place2_prob(o_other) * 0.85
            miss_prob *= (1.0 - min(0.45, p_j))
        p0_waku = 1.0 - miss_prob
        p_waku = max(0.05, min(0.96, p0_waku))

    # v13.17: 枠連実オッズから逆算した1人気枠の市場暗示連対率を20%ブレンド（Brier/LogLoss改善）
    combos_for_mkt = clean_race.get("real_odds") or []
    p_mkt_waku = None
    if combos_for_mkt and 1 <= fav1_waku <= 8 and WAKUREN_MKT_BLEND > 0.0:
        tot_inv = sum(1.0 / float(c["odds"]) for c in combos_for_mkt if float(c.get("odds") or 0.0) > 0.0)
        if tot_inv > 0.0:
            w_inv = sum(
                1.0 / float(c["odds"]) for c in combos_for_mkt
                if float(c.get("odds") or 0.0) > 0.0 and (int(c.get("w1") or 0) == fav1_waku or int(c.get("w2") or 0) == fav1_waku)
            )
            p_mkt_waku = max(0.05, min(0.96, w_inv / tot_inv))
            p_waku = max(0.05, min(0.96, (1.0 - WAKUREN_MKT_BLEND) * p_waku + WAKUREN_MKT_BLEND * p_mkt_waku))

    tier_spec = TIER_SPECS[-1]
    for spec in TIER_SPECS:
        if p_waku >= spec[1]:
            tier_spec = spec
            break

    return {
        "tier_id": tier_spec[0],
        "min_prob": tier_spec[1],
        "tier_name": tier_spec[2],
        "tier_group": tier_spec[3],
        "target_synth": tier_spec[4],
        "axis_tilt": tier_spec[5],
        "odds_alpha": tier_spec[6],
        "leg_power": tier_spec[7],
        "alloc_tilt": tier_spec[8],
        "tan_share": round((0.80 / max(1.0, tan_fav1)) * 100.0, 1),
        "p0": round(p0_waku * 100.0, 1),
        "p_horse": round(p_horse * 100.0, 1),
        "p_waku": round(p_waku * 100.0, 1),
        "strong_partner": strong_partner,
        "z_breakdown": {
            "z0": round(z0, 2), "dz1_cls": round(dz1, 2), "dz2_vn": round(dz2, 2),
            "dz3_hc": round(dz3, 2), "dz4_leg": round(dz4, 2), "dz5_wk": round(dz5, 2),
            "dz6_gap": round(dz6, 2), "dz_env": round(dz_env, 2),
        },
    }


def get_strong_zoro_wakus(
    horses_info: List[Dict],
    max_odds: float = STRONG_ZORO_MAX_ODDS,
    max_pop: int = STRONG_ZORO_MAX_POP,
) -> Set[int]:
    """
    v13.17 有力2頭同居枠の検出（重複馬番・取消馬を自動除去済み）:
      同一枠内に「単勝10倍未満 (1.0 <= win_odds < 10.0) または 単勝上位5人気以内 (1 <= win_pop <= 5)」
      の有力馬が2頭以上同居している枠番の集合を返す。
    """
    clean_horses = sanitize_horses_info(horses_info)
    if not clean_horses:
        return set()

    valid_odds = sorted(
        (float(h.get("win_odds") or 0.0), idx)
        for idx, h in enumerate(clean_horses)
        if float(h.get("win_odds") or 0.0) >= 1.0
    )
    pop_by_idx: Dict[int, int] = {}
    prev_o: Optional[float] = None
    cur_rank = 0
    for pos, (o_val, idx) in enumerate(valid_odds, 1):
        if prev_o is None or o_val > prev_o:
            cur_rank = pos
            prev_o = o_val
        pop_by_idx[idx] = cur_rank

    strong_counts: Dict[int, int] = {}
    for idx, h in enumerate(clean_horses):
        w = int(h.get("waku") or 0)
        if not (1 <= w <= 8):
            continue
        win_odds = float(h.get("win_odds") or 0.0)
        win_pop = int(h.get("win_pop") or pop_by_idx.get(idx, 0))
        is_strong = (1.0 <= win_odds < max_odds) or (1 <= win_pop <= max_pop)
        if is_strong:
            strong_counts[w] = strong_counts.get(w, 0) + 1

    return {w for w, cnt in strong_counts.items() if cnt >= 2}


def rank_combos_by_leg_weight(
    pool: List[Dict],
    waku_leg_scores: Optional[Dict[str, float]] = None,
    candidate_limit: int = 24,
    horse_count: int = 0,
    odds_alpha: Optional[float] = None,
    leg_power: float = 1.05,
    zoro_penalty: float = ZORO_PENALTY_DEFAULT,
    strong_zoro_wakus: Optional[Set[int]] = None,
    strong_zoro_penalty: float = ZORO_PENALTY_STRONG,
    fav1_waku: int = 0,
    axis_tilt: Optional[float] = None,
) -> List[Dict]:
    scored = _score_combos_by_leg_weight(
        pool,
        waku_leg_scores=waku_leg_scores,
        candidate_limit=candidate_limit,
        horse_count=horse_count,
        odds_alpha=odds_alpha,
        leg_power=leg_power,
        zoro_penalty=zoro_penalty,
        strong_zoro_wakus=strong_zoro_wakus,
        strong_zoro_penalty=strong_zoro_penalty,
        fav1_waku=fav1_waku,
        axis_tilt=axis_tilt,
    )
    return [c for c, _ in scored]


def calc_harville_waku_pair_probs(horses_info: List[Dict]) -> Dict[Tuple[int, int], float]:
    """
    v13.17 全出走馬の単勝オッズから全枠連組番 (w1<=w2) の Exact Harville 連対確率を厳密算出。
    単勝プール（最大流動性）の情報から枠連プールの過小評価・歪みを検出するために使用する。
    """
    horses = [
        h for h in (horses_info or [])
        if float(h.get("win_odds") or 0.0) >= 1.0 and 1 <= int(h.get("waku") or 0) <= 8
    ]
    if len(horses) < 2:
        return {}
    inv = [1.0 / float(h["win_odds"]) for h in horses]
    s = sum(inv)
    if s <= 0:
        return {}
    ps = [x / s for x in inv]
    ws = [int(h["waku"]) for h in horses]
    pair_p: Dict[Tuple[int, int], float] = {}
    n = len(horses)
    for i in range(n):
        pi = ps[i]
        denom = 1.0 - pi
        if denom <= 1e-9:
            continue
        wi = ws[i]
        for j in range(n):
            if i == j:
                continue
            pij = pi * (ps[j] / denom)
            a, b = (wi, ws[j]) if wi <= ws[j] else (ws[j], wi)
            pair_p[(a, b)] = pair_p.get((a, b), 0.0) + pij
    return pair_p


def _score_combos_by_leg_weight(
    combos: List[Dict],
    waku_leg_scores: Dict[str, float],
    candidate_limit: int = 36,
    odds_alpha: float = 1.0,
    leg_power: float = 1.0,
    zoro_penalty: float = 1.0,
    strong_zoro_wakus: Optional[set] = None,
    strong_zoro_penalty: float = 0.95,
    market_zoro_max_odds: float = ZORO_MARKET_MAX_ODDS,
    market_zoro_penalty: float = ZORO_PENALTY_MARKET,
    fav1_waku: Optional[int] = None,
    axis_tilt: float = 1.0,
    harville_pair_probs: Optional[Dict[Tuple[int, int], float]] = None,
    harville_blend: float = 0.0,
    value_exp: float = 0.0,
) -> List[Tuple[Dict, float]]:
    """
    v13.17 組番スコアリング:
      ・単勝Exact Harville枠連確率 × 枠連実オッズ市場確率のハイブリッド基礎確率 (55:45)
      ・枠連バリュー歪み指数 (P_harv / P_mkt)^value_exp による妙味検知
      ・前走1・2角(7/18以内)一貫性＆コース適性枠スコアの幾何平均 (leg_power)
      ・連続軸チルト (axis_tilt) ＋ 強ゾロ目緩和ペナルティ
    """
    strong_set = strong_zoro_wakus or set()
    use_harv = bool(harville_pair_probs) and (harville_blend > 0.0 or value_exp > 0.0)
    tot_inv = sum(1.0 / float(c["odds"]) for c in combos if float(c.get("odds") or 0.0) > 0.0)
    scored: List[Tuple[Dict, float]] = []
    for c in combos[:candidate_limit]:
        o = float(c.get("odds") or 0.0)
        if o <= 0:
            continue
        w1 = int(c["w1"])
        w2 = int(c["w2"])
        a, b = (w1, w2) if w1 <= w2 else (w2, w1)
        if use_harv and tot_inv > 0.0:
            p_mkt = (1.0 / o) / tot_inv
            p_harv = float(harville_pair_probs.get((a, b), p_mkt))
            p_base = (1.0 - harville_blend) * p_mkt + harville_blend * p_harv
            base = (max(1e-6, p_base)) ** odds_alpha
            val_mult = (max(0.65, min(1.55, p_harv / p_mkt))) ** value_exp if (value_exp > 0.0 and p_mkt > 0.0) else 1.0
        else:
            base = (1.0 / o) ** odds_alpha
            val_mult = 1.0

        s1 = float(waku_leg_scores.get(str(w1), 1.0))
        s2 = float(waku_leg_scores.get(str(w2), 1.0))
        lw = (s1 * s2) ** (0.5 * leg_power)
        if w1 == w2:
            if w1 in strong_set:
                zp = strong_zoro_penalty
            elif o <= market_zoro_max_odds and market_zoro_penalty > zoro_penalty:
                zp = market_zoro_penalty
            else:
                zp = zoro_penalty
        else:
            zp = 1.0
        tilt = axis_tilt if (fav1_waku is not None and (w1 == fav1_waku or w2 == fav1_waku)) else 1.0
        scored.append((c, base * lw * zp * tilt * val_mult))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


