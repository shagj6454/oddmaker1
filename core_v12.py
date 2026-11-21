import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROD_STATE_PATH = DATA_DIR / "wakuren_state_v11_9.json"
DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"
CACHE_INDEX_JSON = DATA_DIR / "cache_index.json"
CACHE_LIST_MD = DATA_DIR / "CACHE_LIST.md"

INITIAL_BANKROLL = 52000
WITHDRAW_THRESHOLD = 104000
WITHDRAW_AMOUNT = 52000
MAX_RUN_HISTORY = 10

EXCLUDED_VENUES = ("金沢", "笠松", "帯広", "帯広ば", "ばんえい")

# 1番人気信頼度データに基づく競馬場分類
JRA_AXIS_OK_VENUES = ("札幌", "函館", "新潟", "東京", "中山", "京都", "阪神")
JRA_UPSET_VENUES = ("福島", "中京", "小倉")

NAR_AXIS_OK_VENUES = ("名古屋", "佐賀", "高知", "園田", "姫路", "盛岡", "水沢", "門別")
NAR_UPSET_VENUES = ("大井", "浦和", "川崎", "船橋")

# 荒れやすいクラス（1人気連対率が低いクラス）
UPSET_CLASS_KEYWORDS = ("ハンデ", "G3", "GⅢ", "GIII", "2勝", "２勝")

# 小回り・直線短めで逃げ/先行がさらに有利な競馬場
FRONT_BIAS_VENUES = (
    "札幌", "函館", "福島", "小倉", "中山",
    "浦和", "川崎", "園田", "姫路", "名古屋", "高知", "佐賀", "水沢",
)
# 直線が長く差しもある程度届く競馬場
LONG_STRAIGHT_VENUES = ("東京", "新潟", "京都", "阪神", "中京", "大井", "盛岡", "門別", "船橋")

# v12.5 追い上げテーブル（1〜8戦目・8戦目損切りリセット）
STAGES = [
    {"stage": 1, "name": "1戦目", "target": 1350,  "early_bet": 700,   "late_bet": 500},
    {"stage": 2, "name": "2戦目", "target": 2400,  "early_bet": 1300,  "late_bet": 900},
    {"stage": 3, "name": "3戦目", "target": 4050,  "early_bet": 2100,  "late_bet": 1500},
    {"stage": 4, "name": "4戦目", "target": 7000,  "early_bet": 3700,  "late_bet": 2600},
    {"stage": 5, "name": "5戦目", "target": 11900, "early_bet": 6300,  "late_bet": 4400},
    {"stage": 6, "name": "6戦目", "target": 20250, "early_bet": 10600, "late_bet": 7500},
    {"stage": 7, "name": "7戦目", "target": 34500, "early_bet": 18100, "late_bet": 12800},
    {"stage": 8, "name": "8戦目", "target": 58800, "early_bet": 21800, "late_bet": 21800},
]


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


def calc_synth(odds_list: List[float]) -> float:
    valid = [o for o in odds_list if o > 0]
    if not valid:
        return 0.0
    inv = sum(1.0 / o for o in valid)
    return round(1.0 / inv, 2) if inv > 0 else 0.0


def allocate_bets(picked: List[Dict], total_budget: int) -> List[Tuple[str, int]]:
    """合成オッズ均等配分（100円単位）で各組番の購入額を決定"""
    inv_sum = sum(1.0 / c["odds"] for c in picked if c["odds"] > 0)
    if inv_sum <= 0:
        return []
    tickets: List[Tuple[str, int]] = []
    for c in picked:
        raw = total_budget * ((1.0 / c["odds"]) / inv_sum)
        amt = max(100, int(round(raw / 100.0) * 100))
        pair_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
        tickets.append((pair_str, amt))
    return tickets


# ==============================================================================
# 脚質判定（前走1コーナー位置取り）＆ 馬場・競馬場重み付けモデル
# ==============================================================================
def classify_leg_type(c1_pos: Optional[int], prev_field_size: int = 14) -> str:
    """
    前走1コーナー通過順と出走頭数から脚質を4分類:
      - 逃げ: 1番手（または14頭以上なら2番手以内かつ上位15%）
      - 先行: 上位35%以内
      - 差し: 上位70%以内
      - 追込: それより後ろ
    """
    if not c1_pos or c1_pos <= 0:
        return "不明"
    n = max(prev_field_size or 14, c1_pos, 6)
    ratio = c1_pos / float(n)
    if c1_pos == 1 or (c1_pos == 2 and ratio <= 0.15):
        return "逃げ"
    if ratio <= 0.35 or c1_pos <= 4:
        return "先行"
    if ratio <= 0.70:
        return "差し"
    return "追込"


def get_leg_weight(leg_type: str, surface: str = "ダート", track_condition: str = "良", venue: str = "") -> float:
    """
    動画のデータ検証（逃げ・先行優位、特にダートの逃げ強・芝の差し補正）＋当日馬場・競馬場補正を統合した脚質ウェイト
    """
    is_dirt = ("ダ" in (surface or "")) or ("ダート" in (surface or ""))
    is_turf = ("芝" in (surface or "")) and (not is_dirt)
    cond = track_condition or "良"

    if is_turf:
        base = {"逃げ": 1.20, "先行": 1.22, "差し": 1.00, "追込": 0.82, "不明": 0.98}.get(leg_type, 0.98)
        if cond in ("良",):
            if leg_type in ("逃げ", "先行"):
                base += 0.05
        elif cond in ("重", "不良"):
            if leg_type == "逃げ":
                base -= 0.05
            elif leg_type in ("先行", "差し"):
                base += 0.05
    else:
        # ダート（地方競馬デフォルト含む）：逃げ・先行が顕著に有利
        base = {"逃げ": 1.35, "先行": 1.25, "差し": 0.90, "追込": 0.72, "不明": 0.96}.get(leg_type, 0.96)
        if cond in ("重", "不良"):
            # 脚抜きの良い道悪ダートは前が止まらない
            if leg_type in ("逃げ", "先行"):
                base += 0.15
            elif leg_type == "追込":
                base -= 0.08
        elif cond in ("稍重", "稍"):
            if leg_type in ("逃げ", "先行"):
                base += 0.07

    # 競馬場ごとのコーナー・直線長補正
    if any(v in (venue or "") for v in FRONT_BIAS_VENUES):
        if leg_type in ("逃げ", "先行"):
            base += 0.10
        elif leg_type == "追込":
            base -= 0.05
    elif any(v in (venue or "") for v in LONG_STRAIGHT_VENUES):
        if leg_type in ("逃げ", "先行"):
            base += 0.03
        elif leg_type == "差し":
            base += 0.04

    return round(max(0.55, min(1.75, base)), 3)


# ==============================================================================
# JRA 競馬場 × 芝/ダート × 距離別 枠番バイアス (speedkeiba.com/racetracks/ 準拠)
#   タイプ:
#     "inner_strong" : 内枠(1-4枠)が明確に有利、外枠(7-8枠)不利
#     "inner"        : 内枠(1-4枠)がやや有利
#     "outer_strong" : 外枠(5-8枠)が明確に有利、内枠(1-2枠)不利
#     "outer"        : 外枠(5-8枠)がやや有利
#     "flat"         : 枠順の有利不利ほぼなし
#   特定枠ブースト (waku_boost): 顕著な好成績枠への追加補正
# ==============================================================================
JRA_COURSE_WAKU_BIAS: Dict[Tuple[str, str, int], Dict] = {
    # 東京
    ("東京", "芝", 1400): {"bias": "inner_strong", "boost": {1: 0.04, 2: 0.04, 3: 0.03}},
    ("東京", "芝", 1600): {"bias": "flat"},
    ("東京", "芝", 1800): {"bias": "inner_strong"},
    ("東京", "芝", 2000): {"bias": "inner_strong", "boost": {1: 0.04, 2: 0.03}},
    ("東京", "芝", 2400): {"bias": "inner_strong", "boost": {2: 0.06, 1: 0.03}},
    ("東京", "ダート", 1300): {"bias": "outer"},
    ("東京", "ダート", 1400): {"bias": "outer_strong"},
    ("東京", "ダート", 1600): {"bias": "outer_strong", "boost": {7: 0.05, 8: 0.04, 6: -0.04}},
    ("東京", "ダート", 2100): {"bias": "outer"},
    # 中山
    ("中山", "芝", 1200): {"bias": "inner"},
    ("中山", "芝", 1600): {"bias": "inner_strong"},
    ("中山", "芝", 1800): {"bias": "inner"},
    ("中山", "芝", 2000): {"bias": "outer"},
    ("中山", "芝", 2200): {"bias": "outer"},
    ("中山", "芝", 2500): {"bias": "inner_strong"},
    ("中山", "ダート", 1200): {"bias": "outer_strong"},
    ("中山", "ダート", 1800): {"bias": "outer_strong"},
    # 京都
    ("京都", "芝", 1200): {"bias": "inner"},
    ("京都", "芝", 1400): {"bias": "inner"},
    ("京都", "芝", 1600): {"bias": "flat", "boost": {4: 0.03, 7: 0.03}},
    ("京都", "芝", 1800): {"bias": "inner"},
    ("京都", "芝", 2000): {"bias": "inner"},
    ("京都", "芝", 2200): {"bias": "inner"},
    ("京都", "芝", 2400): {"bias": "inner"},
    ("京都", "ダート", 1200): {"bias": "outer"},
    ("京都", "ダート", 1400): {"bias": "outer"},
    ("京都", "ダート", 1800): {"bias": "inner_strong", "boost": {1: 0.06, 2: 0.04}},
    ("京都", "ダート", 1900): {"bias": "inner"},
    # 阪神
    ("阪神", "芝", 1200): {"bias": "flat", "boost": {2: 0.08, 1: -0.03, 3: -0.03}},
    ("阪神", "芝", 1400): {"bias": "inner"},
    ("阪神", "芝", 1600): {"bias": "flat"},
    ("阪神", "芝", 1800): {"bias": "inner_strong", "boost": {5: 0.04, 1: 0.03, 2: 0.03}},
    ("阪神", "芝", 2000): {"bias": "inner"},
    ("阪神", "芝", 2200): {"bias": "inner"},
    ("阪神", "ダート", 1200): {"bias": "outer"},
    ("阪神", "ダート", 1400): {"bias": "outer_strong"},
    ("阪神", "ダート", 1800): {"bias": "outer", "boost": {8: 0.06, 1: -0.04}},
    ("阪神", "ダート", 2000): {"bias": "outer_strong"},
    # 中京
    ("中京", "芝", 1200): {"bias": "inner", "boost": {2: 0.05, 3: 0.05}},
    ("中京", "芝", 1400): {"bias": "inner"},
    ("中京", "芝", 1600): {"bias": "inner"},
    ("中京", "芝", 2000): {"bias": "inner"},
    ("中京", "芝", 2200): {"bias": "inner"},
    ("中京", "ダート", 1200): {"bias": "outer"},
    ("中京", "ダート", 1400): {"bias": "outer_strong"},
    ("中京", "ダート", 1800): {"bias": "inner"},
    ("中京", "ダート", 1900): {"bias": "outer"},
    # 新潟
    ("新潟", "芝", 1000): {"bias": "outer_strong", "boost": {8: 0.12, 7: 0.08, 1: -0.10, 2: -0.08}},
    ("新潟", "芝", 1200): {"bias": "outer"},
    ("新潟", "芝", 1400): {"bias": "inner"},
    ("新潟", "芝", 1600): {"bias": "outer"},
    ("新潟", "芝", 1800): {"bias": "flat"},
    ("新潟", "芝", 2000): {"bias": "flat"},
    ("新潟", "ダート", 1200): {"bias": "outer_strong"},
    ("新潟", "ダート", 1800): {"bias": "outer"},
    # 小倉
    ("小倉", "芝", 1200): {"bias": "inner"},
    ("小倉", "芝", 1800): {"bias": "inner"},
    ("小倉", "芝", 2000): {"bias": "inner"},
    ("小倉", "ダート", 1000): {"bias": "outer"},
    ("小倉", "ダート", 1700): {"bias": "outer"},
    # 札幌
    ("札幌", "芝", 1200): {"bias": "inner"},
    ("札幌", "芝", 1500): {"bias": "inner_strong"},
    ("札幌", "芝", 1800): {"bias": "inner"},
    ("札幌", "芝", 2000): {"bias": "inner"},
    ("札幌", "ダート", 1000): {"bias": "outer"},
    ("札幌", "ダート", 1700): {"bias": "outer"},
    # 函館
    ("函館", "芝", 1200): {"bias": "inner_strong"},
    ("函館", "芝", 1800): {"bias": "inner"},
    ("函館", "芝", 2000): {"bias": "inner"},
    ("函館", "ダート", 1000): {"bias": "outer"},
    ("函館", "ダート", 1700): {"bias": "outer"},
    # 福島
    ("福島", "芝", 1200): {"bias": "inner"},
    ("福島", "芝", 1800): {"bias": "inner"},
    ("福島", "芝", 2000): {"bias": "inner"},
    ("福島", "ダート", 1150): {"bias": "outer_strong"},
    ("福島", "ダート", 1700): {"bias": "outer"},
}

_BIAS_CURVES = {
    "inner_strong": {1: 1.12, 2: 1.10, 3: 1.07, 4: 1.04, 5: 0.98, 6: 0.95, 7: 0.91, 8: 0.88},
    "inner":        {1: 1.06, 2: 1.05, 3: 1.03, 4: 1.02, 5: 0.99, 6: 0.97, 7: 0.95, 8: 0.94},
    "outer_strong": {1: 0.88, 2: 0.91, 3: 0.95, 4: 0.98, 5: 1.04, 6: 1.07, 7: 1.10, 8: 1.12},
    "outer":        {1: 0.94, 2: 0.95, 3: 0.97, 4: 0.99, 5: 1.02, 6: 1.03, 7: 1.05, 8: 1.06},
    "flat":         {1: 1.00, 2: 1.00, 3: 1.00, 4: 1.00, 5: 1.00, 6: 1.00, 7: 1.00, 8: 1.00},
}


def get_course_waku_weights(venue: str, surface: str = "芝", distance: int = 0) -> Dict[int, float]:
    """JRA競馬場×芝/ダート×距離から1〜8枠の重み係数を返す"""
    surf = "ダート" if ("ダ" in (surface or "")) else "芝"
    spec = JRA_COURSE_WAKU_BIAS.get((venue, surf, int(distance or 0)))
    if not spec:
        # 未登録距離のフォールバック：ダートはやや外有利、芝小回り/長距離はやや内有利
        if surf == "ダート":
            bias_key = "outer"
        elif any(v in (venue or "") for v in FRONT_BIAS_VENUES) or (distance and distance >= 2000):
            bias_key = "inner"
        else:
            bias_key = "flat"
        spec = {"bias": bias_key}

    curve = dict(_BIAS_CURVES.get(spec["bias"], _BIAS_CURVES["flat"]))
    for w, delta in (spec.get("boost") or {}).items():
        if w in curve:
            curve[w] = round(max(0.75, min(1.30, curve[w] + delta)), 3)
    return curve


def calc_waku_leg_scores(
    horses_info: List[Dict],
    surface: str = "ダート",
    track_condition: str = "良",
    venue: str = "",
    distance: int = 0,
) -> Dict[str, float]:
    """
    各馬の (waku, leg_type) から枠番1〜8ごとの「前残り・馬場適性 × ハイペース補正 × コース距離別枠バイアス」を算出。
      - レース内に前走「逃げ」が3頭以上いる場合はハイペース自滅リスクを考慮し、逃げの過大評価を抑え差しを微増。
      - 単勝オッズが極端に高い馬(50倍超)が前走たまたま逃げた場合は脚質ブーストを減衰。
      - 枠内最大値(70%) + 枠内平均値(30%) にコース距離別枠係数を乗算する。
    """
    parsed = []
    nige_count = 0
    for h in horses_info or []:
        w = int(h.get("waku") or 0)
        if not (1 <= w <= 8):
            continue
        lt = h.get("leg_type") or classify_leg_type(h.get("prev_c1"), int(h.get("prev_field_size") or 14))
        if lt == "逃げ":
            nige_count += 1
        parsed.append((w, lt, float(h.get("win_odds") or 0.0)))

    high_pace = nige_count >= 3
    by_waku: Dict[int, List[float]] = {}
    for w, lt, w_odds in parsed:
        wt = get_leg_weight(lt, surface=surface, track_condition=track_condition, venue=venue)
        if high_pace:
            if lt == "逃げ":
                wt *= 0.86
            elif lt == "差し":
                wt *= 1.08
        # 大穴(単勝50倍以上)の単発逃げノイズを減衰
        if w_odds >= 50.0 and wt > 1.0:
            wt = 1.0 + (wt - 1.0) * 0.35
        by_waku.setdefault(w, []).append(wt)

    course_w = get_course_waku_weights(venue, surface=surface, distance=distance)
    scores: Dict[str, float] = {}
    for w in range(1, 9):
        wts = by_waku.get(w)
        leg_s = (max(wts) * 0.70 + (sum(wts) / len(wts)) * 0.30) if wts else 1.0
        scores[str(w)] = round(leg_s * course_w.get(w, 1.0), 3)
    return scores


def rank_combos_by_leg_weight(
    pool: List[Dict],
    waku_leg_scores: Optional[Dict[str, float]] = None,
    candidate_limit: int = 12,
) -> List[Dict]:
    """
    候補組番（人気上位 candidate_limit 点以内）に対し、
    「市場暗示確率 (1/odds) × 枠の逃げ・先行＆馬場重み」でスコア付けして並べ替える。
    """
    if not pool:
        return []
    sub = list(pool[:candidate_limit])
    if not waku_leg_scores:
        return sub

    def score(c: Dict) -> float:
        o = float(c.get("odds") or 999.0)
        if o <= 0:
            return 0.0
        s1 = float(waku_leg_scores.get(str(c["w1"]), 1.0))
        s2 = float(waku_leg_scores.get(str(c["w2"]), 1.0))
        # 逃げ・先行馬が入る枠の組番を大きく優先
        leg_boost = ((s1 + s2) / 2.0) ** 1.35
        return (1.0 / o) * leg_boost

    return sorted(sub, key=score, reverse=True)


def _select_with_synth_floor(
    ranked_pool: List[Dict],
    target_n: int,
    min_synth: float,
    skip_cheapest_first: bool = False,
) -> List[Dict]:
    """
    重み付け上位から target_n 点を抽出。
    合成オッズが min_synth 未満の場合は、最もオッズが低い(トリガミ要因の)1点を外して次点へスライドし、
    合成オッズ >= min_synth かつ逃げ・先行重視の組み合わせを構築する。
    """
    if not ranked_pool:
        return []
    work = list(ranked_pool)
    if skip_cheapest_first and len(work) > target_n:
        cheapest_idx = min(range(len(work)), key=lambda idx: work[idx]["odds"])
        work.pop(cheapest_idx)

    picked = work[:target_n]
    rest = work[target_n:]

    while picked and calc_synth([c["odds"] for c in picked]) < min_synth:
        if rest:
            # 最もオッズが低い組番を外して次の候補を入れる
            low_idx = min(range(len(picked)), key=lambda idx: picked[idx]["odds"])
            picked.pop(low_idx)
            picked.append(rest.pop(0))
        elif len(picked) > 2:
            # 残り候補がない場合は最安1点を削って合成オッズを引き上げる
            low_idx = min(range(len(picked)), key=lambda idx: picked[idx]["odds"])
            picked.pop(low_idx)
        else:
            break

    return picked


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
    """確定枠連組番(例:'4-8')の枠連人気とオッズを real_odds から逆引き"""
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


def _make_pick(race: Dict, picked: List[Dict], min_synth: float = 1.20, label: str = "", bet_label: str = "枠複") -> Dict:
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
    pairs = ",".join(f"{c['w1']}-{c['w2']}" for c in picked[:5])
    surf = race.get("surface") or ""
    cond = race.get("track_condition") or ""
    env_tag = f" {surf}{cond}" if (surf or cond) else ""
    return {
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


def evaluate_v12_standard(race: Dict, bet_label: str = "枠連") -> Dict:
    """
    v12.5 原則全レース参戦（見送り廃止）＋ JRA距離別枠バイアス＆逃げ先行・馬場重み付け:
      - 除外のみ: 金沢・笠松・帯広・障害戦・オッズ未取得
      - モードA (単勝 1.0〜1.4倍): 1人気枠軸固定・最安1点切り＋重み上位5点 (目標合成2.20+)
      - モードB (単勝 1.5〜2.0倍): 1人気枠軸固定＋重み上位5点 (目標合成2.40+)
      - モードC1 (単勝 2.1〜2.9倍): 1人気枠外し＋重み上位5点 (目標合成2.50+ / 差し追込2.35+)
      - モードC2 (単勝 3.0倍以上): 1人気枠完全消し＋重み上位5点 (目標合成2.40+)
      ※目標合成に届かない場合も「見送り」にせず、最も期待値の高い組み合わせで参戦する。
    """
    venue = str(race.get("venue", ""))
    rname = str(race.get("race_name", "")) + " " + str(race.get("race_class", ""))

    if any(ex in venue for ex in EXCLUDED_VENUES) or ("障害" in rname):
        return {"participate": False, "skip_reason": "除外競馬場/障害戦(見送り)"}

    combos = race.get("real_odds") or []
    if len(combos) < 2:
        return {"participate": False, "skip_reason": "オッズ未取得(見送り)"}

    tan_fav1 = float(race.get("fav1_win_odds") or 0.0)
    fav1_waku = int(race.get("fav1_waku") or 0)
    fav1_leg = str(race.get("fav1_leg_type") or "不明")
    distance = int(race.get("distance") or 0)
    surface = str(race.get("surface") or ("ダート" if venue not in (JRA_AXIS_OK_VENUES + JRA_UPSET_VENUES) else "芝"))
    track_cond = str(race.get("track_condition") or "良")

    # 距離別枠バイアス＋脚質・馬場スコアを常に最新ロジックで算出
    waku_leg_scores = calc_waku_leg_scores(
        race.get("horses_info") or [], surface=surface, track_condition=track_cond, venue=venue, distance=distance
    )

    # 万一 fav1_waku が未取得なら枠連1人気の内枠側を代替軸として見送りを防ぐ
    if fav1_waku <= 0:
        fav1_waku = int(combos[0]["w1"])
    if tan_fav1 < 1.0:
        tan_fav1 = 3.0

    fav1_is_closer = fav1_leg in ("差し", "追込")

    # モードA：単勝 1.0〜1.4倍（超鉄板軸・最安1点切り＋距離枠＆脚質重み）
    if tan_fav1 <= 1.4:
        axis_pool = [c for c in combos if c["w1"] == fav1_waku or c["w2"] == fav1_waku] or combos
        ranked = rank_combos_by_leg_weight(axis_pool, waku_leg_scores, candidate_limit=8)
        picked = _select_with_synth_floor(ranked, target_n=5, min_synth=2.20, skip_cheapest_first=True)
        res = _make_pick(race, picked, min_synth=1.01, label=f"超軸{fav1_waku}枠(単{tan_fav1})", bet_label=bet_label)
        if res["participate"]:
            res["bucket"] = f"モードA:超鉄板軸(単1.0-1.4 / {bet_label})"
        return res

    # モードB：単勝 1.5〜2.0倍（1人気枠軸＋距離枠＆脚質重み）
    if tan_fav1 <= 2.0:
        axis_pool = [c for c in combos if c["w1"] == fav1_waku or c["w2"] == fav1_waku] or combos
        ranked = rank_combos_by_leg_weight(axis_pool, waku_leg_scores, candidate_limit=8)
        picked = _select_with_synth_floor(ranked, target_n=5, min_synth=2.40, skip_cheapest_first=False)
        res = _make_pick(race, picked, min_synth=1.01, label=f"選別軸{fav1_waku}枠(単{tan_fav1})", bet_label=bet_label)
        if res["participate"]:
            res["bucket"] = f"モードB:選別軸(単1.5-2.0 / {bet_label})"
        return res

    # モードC1：単勝 2.1〜2.9倍（1人気枠外し＋距離枠＆脚質重み）
    if tan_fav1 < 3.0:
        fade_pool = [c for c in combos if c["w1"] != fav1_waku and c["w2"] != fav1_waku] or combos
        ranked = rank_combos_by_leg_weight(fade_pool, waku_leg_scores, candidate_limit=12)
        target_s = 2.35 if fav1_is_closer else 2.50
        picked = _select_with_synth_floor(ranked, target_n=5, min_synth=target_s, skip_cheapest_first=False)
        res = _make_pick(race, picked, min_synth=1.01, label=f"外し{fav1_waku}枠(単{tan_fav1}/{fav1_leg})", bet_label=bet_label)
        if res["participate"]:
            res["bucket"] = f"モードC1:中穴外し(単2.1-2.9 / {bet_label})"
        return res

    # モードC2：単勝 3.0倍以上（1人気枠完全消し＋距離枠＆脚質重み）
    fade_pool = [c for c in combos if c["w1"] != fav1_waku and c["w2"] != fav1_waku] or combos
    ranked = rank_combos_by_leg_weight(fade_pool, waku_leg_scores, candidate_limit=12)
    picked = _select_with_synth_floor(ranked, target_n=5, min_synth=2.40, skip_cheapest_first=False)
    res = _make_pick(race, picked, min_synth=1.01, label=f"消{fav1_waku}枠(単{tan_fav1})", bet_label=bet_label)
    if res["participate"]:
        tag = "単4.0+" if tan_fav1 >= 4.0 else "単3.0-3.9"
        res["bucket"] = f"モードC2:1人気枠消し({tag} / {bet_label})"
    return res


def build_100_strategies(bet_label: str = "枠複") -> List[Tuple[str, Callable[[Dict], Dict]]]:
    """中央・地方共通の100パターン総当たりロジック（v12.5新ロジック＋脚質重視パターン搭載）"""
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
            ranked = rank_combos_by_leg_weight(pool, wls, candidate_limit=12)
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

    # v12.5 コア戦略群を先頭に登録
    strats.append(("v12.5_全データ+逃げ先行馬場重み", lambda r: evaluate_v12_standard(r, bet_label)))
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


def run_progression_sim(races: List[Dict], eval_fn: Callable[[Dict], Dict], log_skips: bool = False) -> Dict:
    """追い上げシミュレーション共通エンジン"""
    balance, stage_idx = INITIAL_BANKROLL, 0
    total_bet = total_payout = withdrawn = w_cnt = stop_loss = hits = misses = skips = max_ls = cur_ls = 0
    prog_stats: Dict[str, Dict] = {}
    logs: List[Dict] = []

    for r in races:
        ev = eval_fn(r)
        st_name = STAGES[stage_idx]["name"]
        title = f"{r['venue']} {r['race_num']}R"
        win_str = r.get("wf_pair") or ""
        res_pop, res_odds = lookup_result_combo(r.get("real_odds") or [], win_str)
        dist_s = f"{r.get('distance')}m" if r.get("distance") else ""
        surf_cond = f"{r.get('surface') or ''}{dist_s}{r.get('track_condition') or ''}"

        if not ev["participate"]:
            skips += 1
            if log_skips:
                logs.append({
                    "timestamp": r["timestamp"], "race_title": title, "bet_type": "-",
                    "stage_name": st_name, "mode_name": "見送り", "surface_cond": surf_cond,
                    "combos_summary": ev["skip_reason"], "synth_odds": 0.0,
                    "bought_tickets": [], "hit_ticket": None,
                    "status": "SKIP", "total_bet": 0, "payout": 0,
                    "result_pair": win_str or "-", "result_pop": res_pop, "result_odds": res_odds,
                    "balance": balance,
                })
            continue

        st = STAGES[stage_idx]
        bet = st["late_bet"] if int(r["race_num"]) >= 9 else st["early_bet"]
        balance -= bet
        total_bet += bet

        # 買った馬券（各組番・枠連人気・オッズ・均等配分額・的中フラグ）を構築
        picked_list = ev.get("picked") or []
        alloc_map = dict(allocate_bets(picked_list, bet))
        hit_pair_str = (ev.get("hit_combo") or {}).get("pair")
        bought_tickets = []
        hit_ticket = None
        for c in picked_list:
            p_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
            amt = alloc_map.get(p_str, 100)
            is_h = bool(ev["is_hit"] and p_str == hit_pair_str)
            item = {
                "pair": p_str,
                "pop": int(c.get("pop") or 0),
                "odds": float(c.get("odds") or 0.0),
                "amount": amt,
                "is_hit": is_h,
            }
            bought_tickets.append(item)
            if is_h:
                hit_ticket = dict(item)

        if not win_str:
            payout, status = 0, "PENDING"
        elif ev["is_hit"]:
            payout = int(round(bet * ev["synth_odds"] / 10.0) * 10)
            balance += payout
            total_payout += payout
            hits += 1
            cur_ls = 0
            stage_idx = 0
            status = "HIT"
            if balance >= WITHDRAW_THRESHOLD:
                balance -= WITHDRAW_AMOUNT
                withdrawn += WITHDRAW_AMOUNT
                w_cnt += 1
        else:
            payout = 0
            misses += 1
            cur_ls += 1
            max_ls = max(max_ls, cur_ls)
            if stage_idx >= 7:
                stop_loss += 1
                stage_idx = 0
                status = "STOP_LOSS"
            else:
                stage_idx += 1
                status = "MISS"

        bkt = ev.get("bucket", "全体")
        prog_stats.setdefault(bkt, {"races": 0, "hits": 0, "bet": 0, "payout": 0, "flat_bet": 0, "flat_payout": 0})
        prog_stats[bkt]["races"] += 1
        prog_stats[bkt]["bet"] += bet
        prog_stats[bkt]["payout"] += payout
        if win_str:
            prog_stats[bkt]["flat_bet"] += 1000
            if status == "HIT":
                prog_stats[bkt]["flat_payout"] += int(round(1000 * ev["synth_odds"] / 10.0) * 10)
        if status == "HIT":
            prog_stats[bkt]["hits"] += 1

        logs.append({
            "timestamp": r["timestamp"], "race_title": title, "bet_type": ev["bet_type"],
            "stage_name": st_name, "mode_name": bkt, "surface_cond": surf_cond,
            "combos_summary": ev["combos_summary"], "synth_odds": ev.get("synth_odds", 0.0),
            "bought_tickets": bought_tickets, "hit_ticket": hit_ticket,
            "status": status, "total_bet": bet, "payout": payout,
            "result_pair": ev["result_pair"], "result_pop": ev.get("result_pop", res_pop),
            "result_odds": ev.get("result_odds", res_odds), "balance": balance,
        })

    roi = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    hr = round((hits / (hits + misses) * 100.0), 1) if (hits + misses) > 0 else 0.0
    return {
        "races": hits + misses, "hits": hits, "misses": misses, "skips": skips, "hit_rate": hr,
        "balance": balance, "withdrawn_total": withdrawn, "withdraw_count": w_cnt,
        "daily_stats": {
            "total_bet": total_bet, "total_payout": total_payout,
            "net_profit": total_payout - total_bet, "recovery_rate": roi,
            "hit_count": hits, "miss_count": misses, "skip_count": skips,
            "max_losing_streak": max_ls, "stop_loss_count": stop_loss, "bankrupt_count": 0,
        },
        "prog_stats": prog_stats, "race_logs": logs[-120:],
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

    lines = ["# 🟢 v12.5 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象 | 区分 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
    for e in entries:
        lines.append(f"| 🟢 キャッシュ済 | **{e['label']}** | `{e['venue']}` | {e['days']}日 | {e['races']:,}R |")
    open(CACHE_LIST_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return entries


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
        "balance": sim_res["balance"], "withdrawn_total": sim_res["withdrawn_total"],
        "withdraw_count": sim_res["withdraw_count"], "daily_stats": sim_res["daily_stats"],
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
        "active_ticket": None, "cached_items": cached_items, "run_history": new_hist[:MAX_RUN_HISTORY],
    }
    json.dump(out, open(DRY_RUN_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_prod_state() -> Dict:
    if PROD_STATE_PATH.exists():
        try:
            return json.load(open(PROD_STATE_PATH, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "version": "v12.5",
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    }


def save_prod_state(state: Dict) -> None:
    state["version"] = "v12.5"
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(PROD_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
