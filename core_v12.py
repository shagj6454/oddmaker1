import math
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

INITIAL_BANKROLL = 60000
WITHDRAW_THRESHOLD = 120000
WITHDRAW_AMOUNT = 60000
MAX_RUN_HISTORY = 10
MAX_STAGE_IDX = 9  # 0..9 = 1戦目〜10戦目（10戦目不的中で損切りリセット）

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

# v13.4 ハーフマーチンゲール法（前戦×1.5倍・100円単位丸め / 1〜10戦目・累計59,700円）
STAGES = [
    {"stage": 1,  "name": "1戦目",  "target": 1500,  "early_bet": 500,   "late_bet": 500},
    {"stage": 2,  "name": "2戦目",  "target": 2400,  "early_bet": 800,   "late_bet": 800},
    {"stage": 3,  "name": "3戦目",  "target": 3600,  "early_bet": 1200,  "late_bet": 1200},
    {"stage": 4,  "name": "4戦目",  "target": 5400,  "early_bet": 1800,  "late_bet": 1800},
    {"stage": 5,  "name": "5戦目",  "target": 8100,  "early_bet": 2700,  "late_bet": 2700},
    {"stage": 6,  "name": "6戦目",  "target": 12000, "early_bet": 4000,  "late_bet": 4000},
    {"stage": 7,  "name": "7戦目",  "target": 18000, "early_bet": 6000,  "late_bet": 6000},
    {"stage": 8,  "name": "8戦目",  "target": 27000, "early_bet": 9000,  "late_bet": 9000},
    {"stage": 9,  "name": "9戦目",  "target": 40500, "early_bet": 13500, "late_bet": 13500},
    {"stage": 10, "name": "10戦目", "target": 60600, "early_bet": 20200, "late_bet": 20200},
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
    """合成オッズ均等配分（100円単位・予算近似調整付き）で各組番の購入額を決定"""
    valid = [c for c in picked if float(c.get("odds") or 0) > 0]
    inv_sum = sum(1.0 / float(c["odds"]) for c in valid)
    if inv_sum <= 0:
        return []
    items = []
    for c in valid:
        o = float(c["odds"])
        raw = total_budget * ((1.0 / o) / inv_sum)
        amt = max(100, int(round(raw / 100.0) * 100))
        pair_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
        items.append([pair_str, amt, o])

    cur_sum = sum(x[1] for x in items)
    min_possible = len(items) * 100
    target = max(min_possible, int(round(total_budget / 100.0) * 100))
    while cur_sum > target:
        cands = [i for i, x in enumerate(items) if x[1] >= 200]
        if not cands:
            break
        idx = max(cands, key=lambda i: items[i][1] * items[i][2])
        items[idx][1] -= 100
        cur_sum -= 100
    while cur_sum < target:
        idx = min(range(len(items)), key=lambda i: items[i][1] * items[i][2])
        items[idx][1] += 100
        cur_sum += 100

    return [(x[0], x[1]) for x in items]


# ==============================================================================
# 脚質判定（直近最大3走1コーナー通過順の中央値 nearest）＆ 動画5データ＋馬場強重み＋枠バイアス強化モデル
# ==============================================================================
def calc_median_nearest(values: List[int]) -> Optional[int]:
    """動画手法準拠：複数走の1コーナー通過順から quantile(0.5, interpolation='nearest') を算出"""
    valid = sorted(int(v) for v in (values or []) if v and int(v) > 0)
    if not valid:
        return None
    idx = int(round(0.5 * (len(valid) - 1)))
    return valid[idx]


def classify_leg_type(c1_pos: Optional[int], prev_field_size: int = 14) -> str:
    """
    1コーナー通過順と出走頭数から脚質を4分類:
      - 逃げ: 1番手（または14頭以上なら2番手以内かつ上位15%）
      - 先行: 上位35%以内（または4番手以内）
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


# 競馬場ごとの特徴補正（ゼロサム中心設計：±0.05以内の健全なエッジ幅）
VENUE_STYLE_TRAITS: Dict[str, Dict[str, float]] = {
    # JRA：直線長い・差し届きやすい
    "東京": {"逃げ": -0.03, "先行": 0.01, "差し": 0.04, "追込": 0.02},
    "新潟": {"逃げ": -0.02, "先行": 0.01, "差し": 0.03, "追込": 0.02},
    "中京": {"逃げ": -0.01, "先行": 0.02, "差し": 0.02, "追込": 0.00},
    "京都": {"逃げ": 0.01,  "先行": 0.02, "差し": 0.01, "追込": -0.01},
    "阪神": {"逃げ": 0.01,  "先行": 0.02, "差し": 0.01, "追込": -0.01},
    # JRA：小回り・直線短い・前残り強い
    "中山": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "札幌": {"逃げ": 0.03,  "先行": 0.03, "差し": -0.01, "追込": -0.03},
    "函館": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "福島": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "小倉": {"逃げ": 0.05,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    # NAR：小回り前残り系 vs 直線長め系
    "浦和": {"逃げ": 0.05,  "先行": 0.04, "差し": -0.03, "追込": -0.05},
    "川崎": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "園田": {"逃げ": 0.05,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "姫路": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "名古屋": {"逃げ": 0.04, "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "高知": {"逃げ": 0.03,  "先行": 0.03, "差し": -0.01, "追込": -0.03},
    "佐賀": {"逃げ": 0.05,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "水沢": {"逃げ": 0.04,  "先行": 0.03, "差し": -0.02, "追込": -0.04},
    "大井": {"逃げ": 0.00,  "先行": 0.02, "差し": 0.02,  "追込": 0.00},
    "船橋": {"逃げ": 0.02,  "先行": 0.03, "差し": 0.01,  "追込": -0.02},
    "盛岡": {"逃げ": 0.01,  "先行": 0.02, "差し": 0.02,  "追込": 0.00},
    "門別": {"逃げ": 0.02,  "先行": 0.02, "差し": 0.01,  "追込": -0.01},
}


def get_leg_weight(
    leg_type: str,
    surface: str = "ダート",
    track_condition: str = "良",
    venue: str = "",
    distance: int = 0,
    prev_c4: Optional[int] = None,
    prev_field_size: int = 14,
) -> float:
    """
    v13.4 ゼロサム適正化 脚質×前走4コーナー位置×馬場×距離×競馬場ウェイト (レンジ 0.80〜1.24):
      - 平均的な馬が 1.00 になるようキャリブレーションし、市場オッズ(実力)を破壊しない適正エッジ幅へ調整。
      - 前走4コーナー通過順 (prev_c4) がある場合：
          ・4角上位25%以内（勝負どころ好位キープ） -> +0.06
          ・1角逃げなのに4角で半分より後ろへ失速（バテ逃げ） -> -0.09
          ・4角後方75%以降 -> -0.05
    """
    is_dirt = ("ダ" in (surface or "")) or ("ダート" in (surface or ""))
    is_turf = ("芝" in (surface or "")) and (not is_dirt)
    cond = track_condition or "良"
    dist = int(distance or 0)

    if is_turf:
        base = {"逃げ": 1.03, "先行": 1.07, "差し": 1.00, "追込": 0.92, "不明": 0.99}.get(leg_type, 0.99)
        if cond == "良":
            base += {"逃げ": 0.03, "先行": 0.03, "差し": -0.01, "追込": -0.02}.get(leg_type, 0.0)
        elif cond in ("稍重", "稍"):
            base += {"逃げ": -0.01, "先行": 0.02, "差し": 0.02, "追込": 0.00}.get(leg_type, 0.0)
        elif cond == "重":
            base += {"逃げ": -0.04, "先行": 0.02, "差し": 0.05, "追込": 0.02}.get(leg_type, 0.0)
        elif cond == "不良":
            base += {"逃げ": -0.06, "先行": 0.02, "差し": 0.06, "追込": 0.03}.get(leg_type, 0.0)
    else:
        base = {"逃げ": 1.09, "先行": 1.07, "差し": 0.95, "追込": 0.87, "不明": 0.98}.get(leg_type, 0.98)
        if cond in ("稍重", "稍"):
            base += {"逃げ": 0.04, "先行": 0.03, "差し": -0.02, "追込": -0.03}.get(leg_type, 0.0)
        elif cond == "重":
            base += {"逃げ": 0.07, "先行": 0.05, "差し": -0.03, "追込": -0.05}.get(leg_type, 0.0)
        elif cond == "不良":
            base += {"逃げ": 0.09, "先行": 0.06, "差し": -0.04, "追込": -0.06}.get(leg_type, 0.0)

    if dist > 0:
        if dist <= 1400:
            base += {"逃げ": 0.03, "先行": 0.02, "差し": -0.01, "追込": -0.03}.get(leg_type, 0.0)
        elif dist >= 2200:
            base += {"逃げ": -0.03, "先行": 0.00, "差し": 0.03, "追込": 0.02}.get(leg_type, 0.0)

    for v_key, traits in VENUE_STYLE_TRAITS.items():
        if v_key in (venue or ""):
            base += traits.get(leg_type, 0.0)
            break

    # 前走4コーナー通過順（直線入口の位置取り・追走持続力）による補正
    if prev_c4 and int(prev_c4) > 0:
        n = max(int(prev_field_size or 14), int(prev_c4), 6)
        c4_ratio = float(prev_c4) / float(n)
        if c4_ratio <= 0.25 or int(prev_c4) <= 3:
            base += 0.06
        elif c4_ratio <= 0.45:
            base += 0.02
        elif c4_ratio >= 0.75:
            base -= 0.05
        # 1角逃げなのに4角で失速している「バテ逃げ馬」を減点
        if leg_type == "逃げ" and c4_ratio >= 0.50:
            base -= 0.09

    return round(max(0.80, min(1.24, base)), 3)


# ==============================================================================
# JRA 競馬場 × 芝/ダート × 距離別 枠番バイアス (speedkeiba.com/racetracks/ 準拠・適正エッジ幅)
# ==============================================================================
JRA_COURSE_WAKU_BIAS: Dict[Tuple[str, str, int], Dict] = {
    # 東京
    ("東京", "芝", 1400): {"bias": "inner_strong", "boost": {1: 0.03, 2: 0.03, 3: 0.02}},
    ("東京", "芝", 1600): {"bias": "flat"},
    ("東京", "芝", 1800): {"bias": "inner_strong"},
    ("東京", "芝", 2000): {"bias": "inner_strong", "boost": {1: 0.03, 2: 0.02}},
    ("東京", "芝", 2400): {"bias": "inner_strong", "boost": {2: 0.04, 1: 0.02}},
    ("東京", "ダート", 1300): {"bias": "outer"},
    ("東京", "ダート", 1400): {"bias": "outer_strong"},
    ("東京", "ダート", 1600): {"bias": "outer_strong", "boost": {7: 0.03, 8: 0.03, 6: -0.02}},
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
    ("京都", "芝", 1600): {"bias": "flat", "boost": {4: 0.02, 7: 0.02}},
    ("京都", "芝", 1800): {"bias": "inner"},
    ("京都", "芝", 2000): {"bias": "inner"},
    ("京都", "芝", 2200): {"bias": "inner"},
    ("京都", "芝", 2400): {"bias": "inner"},
    ("京都", "ダート", 1200): {"bias": "outer"},
    ("京都", "ダート", 1400): {"bias": "outer"},
    ("京都", "ダート", 1800): {"bias": "inner_strong", "boost": {1: 0.04, 2: 0.03}},
    ("京都", "ダート", 1900): {"bias": "inner"},
    # 阪神
    ("阪神", "芝", 1200): {"bias": "flat", "boost": {2: 0.05, 1: -0.02, 3: -0.02}},
    ("阪神", "芝", 1400): {"bias": "inner"},
    ("阪神", "芝", 1600): {"bias": "flat"},
    ("阪神", "芝", 1800): {"bias": "inner_strong", "boost": {5: 0.03, 1: 0.02, 2: 0.02}},
    ("阪神", "芝", 2000): {"bias": "inner"},
    ("阪神", "芝", 2200): {"bias": "inner"},
    ("阪神", "ダート", 1200): {"bias": "outer"},
    ("阪神", "ダート", 1400): {"bias": "outer_strong"},
    ("阪神", "ダート", 1800): {"bias": "outer", "boost": {8: 0.04, 1: -0.03}},
    ("阪神", "ダート", 2000): {"bias": "outer_strong"},
    # 中京
    ("中京", "芝", 1200): {"bias": "inner", "boost": {2: 0.03, 3: 0.03}},
    ("中京", "芝", 1400): {"bias": "inner"},
    ("中京", "芝", 1600): {"bias": "inner"},
    ("中京", "芝", 2000): {"bias": "inner"},
    ("中京", "芝", 2200): {"bias": "inner"},
    ("中京", "ダート", 1200): {"bias": "outer"},
    ("中京", "ダート", 1400): {"bias": "outer_strong"},
    ("中京", "ダート", 1800): {"bias": "inner"},
    ("中京", "ダート", 1900): {"bias": "outer"},
    # 新潟
    ("新潟", "芝", 1000): {"bias": "outer_strong", "boost": {8: 0.08, 7: 0.05, 1: -0.07, 2: -0.05}},
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

# 枠バイアス幅を現実的なエッジ（±5%〜±9%）へ適正化
_BIAS_CURVES = {
    "inner_strong": {1: 1.09, 2: 1.07, 3: 1.04, 4: 1.02, 5: 0.98, 6: 0.96, 7: 0.93, 8: 0.91},
    "inner":        {1: 1.05, 2: 1.04, 3: 1.02, 4: 1.01, 5: 0.99, 6: 0.98, 7: 0.96, 8: 0.95},
    "outer_strong": {1: 0.91, 2: 0.93, 3: 0.96, 4: 0.98, 5: 1.02, 6: 1.04, 7: 1.07, 8: 1.09},
    "outer":        {1: 0.95, 2: 0.96, 3: 0.98, 4: 0.99, 5: 1.01, 6: 1.02, 7: 1.04, 8: 1.05},
    "flat":         {1: 1.00, 2: 1.00, 3: 1.00, 4: 1.00, 5: 1.00, 6: 1.00, 7: 1.00, 8: 1.00},
}


def get_course_waku_weights(venue: str, surface: str = "芝", distance: int = 0) -> Dict[int, float]:
    """JRA競馬場×芝/ダート×距離から1〜8枠の重み係数を返す（適正エッジ幅）"""
    surf = "ダート" if ("ダ" in (surface or "")) else "芝"
    spec = JRA_COURSE_WAKU_BIAS.get((venue, surf, int(distance or 0)))
    if not spec:
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
            curve[w] = round(max(0.84, min(1.18, curve[w] + delta)), 3)
    return curve


# ==============================================================================
# 動画データ：JRA全10競馬場 × 人気別 単勝回収率 (2011年〜2020年)
# ==============================================================================
OVERALL_POP_WIN_ROI: Dict[int, float] = {
    1: 77.4, 2: 79.5, 3: 79.4, 4: 78.8, 5: 81.0, 6: 83.6, 7: 82.0, 8: 76.1, 9: 79.3,
    10: 73.0, 11: 75.7, 12: 71.7, 13: 64.2, 14: 54.5, 15: 45.7, 16: 38.8, 17: 14.3, 18: 19.8,
}

VENUE_POP_WIN_ROI: Dict[str, Dict[int, float]] = {
    "札幌": {1: 79.1, 2: 76.0, 3: 82.8, 4: 83.3, 5: 76.2, 6: 90.0, 7: 81.9, 8: 64.6, 9: 79.1, 10: 100.7, 11: 76.2, 12: 79.7, 13: 22.6, 14: 40.0, 15: 16.0, 16: 0.0,  17: 0.0,  18: 0.0},
    "函館": {1: 78.5, 2: 74.2, 3: 81.9, 4: 69.1, 5: 85.3, 6: 74.7, 7: 80.4, 8: 46.0, 9: 84.5, 10: 99.6,  11: 65.4, 12: 77.2, 13: 73.7, 14: 60.6, 15: 32.0, 16: 31.0, 17: 0.0,  18: 0.0},
    "福島": {1: 72.3, 2: 82.4, 3: 74.7, 4: 77.6, 5: 91.1, 6: 84.0, 7: 78.7, 8: 75.2, 9: 81.3, 10: 92.5,  11: 63.0, 12: 71.6, 13: 52.7, 14: 83.2, 15: 53.7, 16: 86.0, 17: 0.0,  18: 0.0},
    "新潟": {1: 82.3, 2: 77.6, 3: 74.7, 4: 78.7, 5: 76.6, 6: 76.3, 7: 88.6, 8: 71.1, 9: 69.1, 10: 66.0,  11: 64.8, 12: 72.3, 13: 79.4, 14: 52.0, 15: 69.6, 16: 15.6, 17: 0.0,  18: 76.4},
    "東京": {1: 80.9, 2: 80.1, 3: 76.9, 4: 82.8, 5: 73.3, 6: 82.9, 7: 75.6, 8: 80.2, 9: 85.4, 10: 60.3,  11: 69.5, 12: 75.9, 13: 34.4, 14: 60.4, 15: 14.3, 16: 63.4, 17: 0.0,  18: 0.0},
    "中山": {1: 79.4, 2: 77.3, 3: 77.8, 4: 81.2, 5: 78.8, 6: 81.8, 7: 70.4, 8: 82.0, 9: 85.9, 10: 72.3,  11: 62.0, 12: 82.6, 13: 58.1, 14: 26.0, 15: 59.7, 16: 34.1, 17: 0.0,  18: 0.0},
    "中京": {1: 77.8, 2: 79.8, 3: 86.2, 4: 81.7, 5: 71.9, 6: 84.4, 7: 72.5, 8: 72.1, 9: 76.5, 10: 77.0,  11: 79.6, 12: 78.8, 13: 46.4, 14: 47.3, 15: 17.5, 16: 51.2, 17: 0.0,  18: 0.0},
    "京都": {1: 73.5, 2: 79.7, 3: 81.5, 4: 79.5, 5: 87.4, 6: 86.4, 7: 87.4, 8: 78.8, 9: 81.5, 10: 74.6,  11: 94.2, 12: 61.1, 13: 80.9, 14: 36.3, 15: 42.3, 16: 46.2, 17: 0.0,  18: 41.8},
    "阪神": {1: 77.2, 2: 79.2, 3: 80.5, 4: 76.3, 5: 89.3, 6: 82.1, 7: 92.4, 8: 84.0, 9: 74.2, 10: 62.9,  11: 79.8, 12: 59.3, 13: 92.7, 14: 55.4, 15: 71.6, 16: 0.0,  17: 58.4, 18: 0.0},
    "小倉": {1: 71.9, 2: 87.7, 3: 82.9, 4: 71.9, 5: 77.3, 6: 97.6, 7: 90.0, 8: 72.1, 9: 74.2, 10: 73.3,  11: 95.4, 12: 66.5, 13: 79.4, 14: 111.6,15: 53.6, 16: 25.6, 17: 38.7, 18: 0.0},
}


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
) -> Dict[str, float]:
    """
    v13.4 実力加重平均型 枠スコア算出 (レンジ 0.82〜1.22):
      - 同枠内の大穴馬(単勝100倍)が枠スコアを独占するバグを防ぐため、
        各馬の市場支持度 (1 / win_odds^0.85) で加重平均した枠スコアを算出。
      - 前走4コーナー通過順 (prev_c4) と 1コーナー脚質 (prev_c1) の両方を評価。
      - レース内に前走「逃げ」が4頭以上いる場合、逃げ×0.94、差し×1.05。
      - コース距離別枠バイアス・動画場別人気ROIを統合し、0.82〜1.22 の健全レンジに収める。
    """
    odds_ranked = sorted(
        [(float(h.get("win_odds") or 0.0), int(h.get("umaban") or 0)) for h in (horses_info or []) if float(h.get("win_odds") or 0.0) >= 1.0],
        key=lambda x: x[0],
    )
    pop_by_uma = {uma: idx + 1 for idx, (_, uma) in enumerate(odds_ranked)}

    parsed = []
    nige_count = 0
    for h in horses_info or []:
        w = int(h.get("waku") or 0)
        if not (1 <= w <= 8):
            continue
        uma = int(h.get("umaban") or 0)
        lt = h.get("leg_type")
        if not lt or lt == "不明":
            med_c1 = calc_median_nearest(h.get("prev_c1_list") or ([h.get("prev_c1")] if h.get("prev_c1") else []))
            lt = classify_leg_type(med_c1, int(h.get("prev_field_size") or 14))
        if lt == "逃げ":
            nige_count += 1
        win_pop = int(h.get("win_pop") or pop_by_uma.get(uma, 0))
        win_odds = float(h.get("win_odds") or 0.0)
        prev_c4 = h.get("prev_c4")
        if not prev_c4 and h.get("prev_c4_list"):
            prev_c4 = calc_median_nearest(h.get("prev_c4_list"))
        prev_n = int(h.get("prev_field_size") or 14)
        parsed.append((w, lt, win_pop, win_odds, prev_c4, prev_n))

    high_pace = nige_count >= 4
    by_waku: Dict[int, List[Tuple[float, float]]] = {}
    for w, lt, win_pop, win_odds, prev_c4, prev_n in parsed:
        wt = get_leg_weight(
            lt, surface=surface, track_condition=track_condition,
            venue=venue, distance=distance, prev_c4=prev_c4, prev_field_size=prev_n,
        )
        if high_pace:
            if lt == "逃げ":
                wt *= 0.94
            elif lt == "差し":
                wt *= 1.05
        if win_pop > 0:
            wt *= get_pop_roi_weight(venue, win_pop)
        # 市場支持度ウェイト（オッズがある馬は 1/odds^0.85 で主力馬の脚質を優先反映）
        support = (1.0 / (max(1.2, win_odds) ** 0.85)) if win_odds >= 1.0 else 0.15
        by_waku.setdefault(w, []).append((wt, support))

    course_w = get_course_waku_weights(venue, surface=surface, distance=distance)
    scores: Dict[str, float] = {}
    for w in range(1, 9):
        items = by_waku.get(w)
        if items:
            tot_sup = sum(s for _, s in items)
            leg_s = sum(wt * s for wt, s in items) / tot_sup if tot_sup > 0 else 1.0
        else:
            leg_s = 1.0
        raw_s = leg_s * course_w.get(w, 1.0)
        scores[str(w)] = round(max(0.82, min(1.22, raw_s)), 3)
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
# v13.4 ゼロサム・キャリブレーション済み ロジット型1番人気『枠』連対確率モデル
# ==============================================================================
_BASE_ODDS_CURVE: List[Tuple[float, float]] = [
    (1.0, 0.900),
    (1.1, 0.880),
    (1.25, 0.850),
    (1.45, 0.770),
    (1.65, 0.703),
    (1.85, 0.632),
    (2.20, 0.550),
    (2.70, 0.475),
    (3.40, 0.405),
    (4.40, 0.331),
    (5.50, 0.220),
    (8.00, 0.140),
    (15.0, 0.080),
]

# Tier 1 〜 Tier 10 定義（v13.4: T3からハイブリッド導入・T7の6倍縛り撤廃・市場重視度適正化）
TIER_SPECS = [
    # (tier_id, min_prob, short_name, mode_group, target_synth, odds_alpha, leg_power)
    ("T1",  0.78, "T1:極鉄板・本線集中",     "軸",   1.70, 1.15, 0.85),
    ("T2",  0.68, "T2:超鉄板・本線流し",     "軸",   1.90, 1.12, 0.90),
    ("T3",  0.60, "T3:軸主力ハイブリッド",   "混合", 2.15, 1.05, 1.00),
    ("T4",  0.52, "T4:軸寄りハイブリッド",   "混合", 2.30, 1.00, 1.05),
    ("T5",  0.45, "T5:完全均衡ハイブリッド", "混合", 2.45, 0.96, 1.10),
    ("T6",  0.38, "T6:消寄ハイブリッド",     "混合", 2.60, 0.94, 1.15),
    ("T7",  0.32, "T7:準消し・上位堅実",     "消し", 2.55, 0.92, 1.15),
    ("T8",  0.26, "T8:標準消し・中穴網羅",   "消し", 2.80, 0.88, 1.20),
    ("T9",  0.20, "T9:強消し・高配当狙い",   "消し", 3.30, 0.84, 1.25),
    ("T10", 0.00, "T10:極荒れ・大穴挑戦",    "消し", 3.80, 0.80, 1.30),
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
    horses = race.get("horses_info") or []
    all_odds: List[Tuple[float, int, int]] = []
    for h in horses:
        o = float(h.get("win_odds") or 0.0)
        if o >= 1.0:
            all_odds.append((o, int(h.get("umaban") or 0), int(h.get("waku") or 0)))
    all_odds.sort(key=lambda x: x[0])

    fav2_odds = float(race.get("fav2_win_odds") or 0.0)
    if fav2_odds < 1.0 and len(all_odds) >= 2:
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


def calc_fav1_waku_prob(race: Dict) -> Dict:
    """
    v13.4 ゼロサム・キャリブレーション版 1番人気『枠』実効連対確率モデル:
      - 標準レースで基礎連対率 P0 からの系統的プラスインフレを完全除去。
      - 同枠アシストは相関(1-2着奪い合い)と控除率を織り込んだ現実値 0.85 / (o^1.15) に調整。
      - 1番人気馬自身の前走4コーナー通過順 (prev_c4) も信頼度評価に統合。
    """
    venue = str(race.get("venue", ""))
    rname = str(race.get("race_name", "")) + " " + str(race.get("race_class", ""))
    is_jra = venue in (JRA_AXIS_OK_VENUES + JRA_UPSET_VENUES)
    surface = str(race.get("surface") or ("芝" if is_jra else "ダート"))
    is_dirt = ("ダ" in surface)
    track_cond = str(race.get("track_condition") or "良")
    distance = int(race.get("distance") or 0)
    horse_count = int(race.get("horse_count") or len(race.get("horses_info") or []) or 0)
    tan_fav1 = float(race.get("fav1_win_odds") or 3.0)
    fav1_waku = int(race.get("fav1_waku") or 0)
    fav1_leg = str(race.get("fav1_leg_type") or "不明")

    # 1番人気馬の prev_c4 / prev_field_size を horses_info から取得
    fav1_uma = int(race.get("fav1_umaban") or 0)
    fav1_c4 = None
    fav1_pn = 14
    for h in (race.get("horses_info") or []):
        if (fav1_uma > 0 and int(h.get("umaban") or 0) == fav1_uma) or (
            fav1_uma <= 0 and int(h.get("waku") or 0) == fav1_waku and abs(float(h.get("win_odds") or 0.0) - tan_fav1) < 0.05
        ):
            fav1_c4 = h.get("prev_c4")
            if not fav1_c4 and h.get("prev_c4_list"):
                fav1_c4 = calc_median_nearest(h.get("prev_c4_list"))
            fav1_pn = int(h.get("prev_field_size") or 14)
            break

    p0 = _interp_base_place2_prob(tan_fav1)
    z0 = _logit(p0)

    # Δz1: クラス × 芝/ダート（ゼロサム寄り：平場1勝/2勝=0.00、未勝利+0.12、OP/重賞/ハンデは明確減点）
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

    # Δz2: 競馬場 × 芝/ダート（ゼロサム設計：中央値0.00中心）
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
    else:
        if any(v in venue for v in NAR_AXIS_OK_VENUES):
            dz2 = 0.32
        elif any(v in venue for v in NAR_UPSET_VENUES):
            dz2 = 0.00

    # Δz3: 出走頭数（12-14頭=0.00基準）
    if horse_count <= 0:
        dz3 = 0.0
    elif horse_count <= 8:
        dz3 = 0.30
    elif horse_count <= 11:
        dz3 = 0.14
    elif horse_count <= 14:
        dz3 = 0.00
    elif horse_count <= 16:
        dz3 = -0.16
    else:
        dz3 = -0.28

    # Δz4: 1人気馬の脚質 × 前走4角 × 馬場（中心1.02を基準にゼロサム化）
    w_leg = get_leg_weight(
        fav1_leg, surface=surface, track_condition=track_cond,
        venue=venue, distance=distance, prev_c4=fav1_c4, prev_field_size=fav1_pn,
    )
    dz4 = 1.15 * (w_leg - 1.02)
    nige_count = sum(1 for h in (race.get("horses_info") or []) if h.get("leg_type") == "逃げ")
    if nige_count >= 4:
        if fav1_leg == "逃げ":
            dz4 -= 0.20
        elif fav1_leg == "差し":
            dz4 += 0.12

    # Δz5: 1人気馬の距離別枠バイアス（中心1.00）
    course_w = get_course_waku_weights(venue, surface=surface, distance=distance)
    w_course = course_w.get(fav1_waku, 1.0) if 1 <= fav1_waku <= 8 else 1.0
    dz5 = 1.60 * (w_course - 1.00)

    # Δz6: 単勝オッズ断層 (2人気 / 1人気)
    fav2_odds, same_waku_others = _extract_fav2_and_same_waku_odds(race, fav1_waku, tan_fav1)
    dz6 = 0.0
    r21 = (fav2_odds / tan_fav1) if (fav2_odds >= 1.0 and tan_fav1 >= 1.0) else 0.0
    if r21 >= 2.2:
        dz6 = 0.16
    elif r21 >= 1.5:
        dz6 = 0.00
    elif 0.0 < r21 < 1.3:
        dz6 = -0.18

    z_total = z0 + dz1 + dz2 + dz3 + dz4 + dz5 + dz6
    p_horse = _sigmoid(z_total)

    # 同枠アシスト（条件付き確率・控除率考慮の現実値：単勝12倍で約4.9%加算）
    miss_prob = 1.0 - p_horse
    strong_partner = False
    for o_other in same_waku_others:
        if o_other <= 12.0:
            strong_partner = True
        p_j = min(0.42, 0.85 / (max(1.5, o_other) ** 1.15))
        miss_prob *= (1.0 - p_j)
    p_waku = 1.0 - miss_prob

    tier_spec = TIER_SPECS[-1]
    for spec in TIER_SPECS:
        if p_waku >= spec[1]:
            tier_spec = spec
            break

    return {
        "tier_id": tier_spec[0],
        "tier_name": tier_spec[2],
        "tier_group": tier_spec[3],
        "target_synth": tier_spec[4],
        "odds_alpha": tier_spec[5],
        "leg_power": tier_spec[6],
        "p0": round(p0 * 100.0, 1),
        "p_horse": round(p_horse * 100.0, 1),
        "p_waku": round(p_waku * 100.0, 1),
        "strong_partner": strong_partner,
        "z_breakdown": {
            "z0": round(z0, 2), "dz1_cls": round(dz1, 2), "dz2_vn": round(dz2, 2),
            "dz3_hc": round(dz3, 2), "dz4_leg": round(dz4, 2), "dz5_wk": round(dz5, 2),
            "dz6_gap": round(dz6, 2),
        },
    }


def rank_combos_by_leg_weight(
    pool: List[Dict],
    waku_leg_scores: Optional[Dict[str, float]] = None,
    candidate_limit: int = 18,
    horse_count: int = 0,
    odds_alpha: Optional[float] = None,
    leg_power: float = 1.05,
    zoro_penalty: float = 0.50,
) -> List[Dict]:
    """
    候補組番（人気上位 candidate_limit 点以内）に対し、
    「市場暗示確率 (1/odds)^odds_alpha × 枠スコア^leg_power × 同枠ペナルティ」でスコア付けして並べ替える。
    """
    if not pool:
        return []
    sub = list(pool[:candidate_limit])
    alpha = odds_alpha if odds_alpha is not None else get_field_size_odds_alpha(horse_count)

    def score(c: Dict) -> float:
        o = float(c.get("odds") or 999.0)
        if o <= 0:
            return 0.0
        if waku_leg_scores:
            s1 = float(waku_leg_scores.get(str(c["w1"]), 1.0))
            s2 = float(waku_leg_scores.get(str(c["w2"]), 1.0))
            leg_boost = ((s1 + s2) / 2.0) ** leg_power
        else:
            leg_boost = 1.0
        zp = zoro_penalty if (c.get("w1") == c.get("w2")) else 1.0
        return ((1.0 / o) ** alpha) * leg_boost * zp

    return sorted(sub, key=score, reverse=True)


def _select_axis_reduce_count(
    ranked_pool: List[Dict],
    min_synth: float = 1.90,
    max_n: int = 4,
    min_n: int = 3,
) -> List[Dict]:
    if not ranked_pool:
        return []
    eff_min = min(min_n, len(ranked_pool))
    for k in range(min(max_n, len(ranked_pool)), eff_min - 1, -1):
        cand = ranked_pool[:k]
        if calc_synth([c["odds"] for c in cand]) >= min_synth:
            return cand
    return ranked_pool[:eff_min]


def _expand_picks_keeping_synth(
    ranked_pool: List[Dict],
    min_n: int,
    max_n: int,
    min_synth: float,
    min_single_odds: float = 0.0,
) -> List[Dict]:
    if not ranked_pool:
        return []

    if min_single_odds > 0:
        high_pool = [c for c in ranked_pool if float(c.get("odds") or 0) >= min_single_odds]
        low_pool = [c for c in ranked_pool if float(c.get("odds") or 0) < min_single_odds]
        work = high_pool + low_pool
    else:
        work = list(ranked_pool)

    eff_min = min(min_n, len(work))
    picked = work[:eff_min]
    rest = work[eff_min:]

    while picked and rest and calc_synth([c["odds"] for c in picked]) < min_synth:
        low_idx = min(range(len(picked)), key=lambda idx: picked[idx]["odds"])
        nxt = rest[0]
        if nxt["odds"] <= picked[low_idx]["odds"]:
            rest.pop(0)
            continue
        picked.pop(low_idx)
        picked.append(rest.pop(0))

    for cand in list(rest):
        if len(picked) >= max_n:
            break
        trial = picked + [cand]
        if calc_synth([c["odds"] for c in trial]) >= min_synth:
            picked = trial

    return picked


def _select_hybrid_portfolio(
    ranked_axis: List[Dict],
    ranked_fade: List[Dict],
    n_axis: int,
    n_fade: int,
    target_synth: float,
    max_total: int = 7,
    prefer_fade_expand: bool = True,
) -> List[Dict]:
    a_pick = list(ranked_axis[:n_axis])
    a_rest = list(ranked_axis[n_axis:])
    f_pick = list(ranked_fade[:n_fade])
    f_rest = list(ranked_fade[n_fade:])

    if not (a_pick or f_pick):
        return []

    slides = 0
    while f_pick and f_rest and slides < 2 and calc_synth([c["odds"] for c in (a_pick + f_pick)]) < target_synth:
        low_i = min(range(len(f_pick)), key=lambda i: f_pick[i]["odds"])
        if f_rest[0]["odds"] <= f_pick[low_i]["odds"]:
            f_rest.pop(0)
            continue
        f_pick.pop(low_i)
        f_pick.append(f_rest.pop(0))
        slides += 1

    seen = set()
    out: List[Dict] = []
    for c in (a_pick + f_pick):
        k = (c["w1"], c["w2"])
        if k not in seen:
            seen.add(k)
            out.append(c)

    expand_queue: List[Dict] = []
    if prefer_fade_expand:
        for i in range(max(len(f_rest), len(a_rest))):
            if i < len(f_rest):
                expand_queue.append(f_rest[i])
            if i < len(a_rest) and n_axis >= 2:
                expand_queue.append(a_rest[i])
    else:
        for i in range(max(len(a_rest), len(f_rest))):
            if i < len(a_rest):
                expand_queue.append(a_rest[i])
            if i < len(f_rest):
                expand_queue.append(f_rest[i])

    for cand in expand_queue:
        if len(out) >= max_total:
            break
        k = (cand["w1"], cand["w2"])
        if k in seen:
            continue
        trial = out + [cand]
        if calc_synth([c["odds"] for c in trial]) >= target_synth:
            seen.add(k)
            out = trial

    return out


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
    return _expand_picks_keeping_synth(work, min_n=min(3, target_n), max_n=target_n, min_synth=min_synth)


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


def _make_pick(
    race: Dict,
    picked: List[Dict],
    min_synth: float = 1.01,
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


def evaluate_v12_standard(race: Dict, bet_label: str = "枠連") -> Dict:
    """
    v13.4 ゼロサム・キャリブレーション済み 10段階モード(T1〜T10)モデル:
      - T1 (78%+): 極鉄板・本線集中（軸3点・同枠除外）
      - T2 (68-77%): 超鉄板・本線流し（軸3〜4点・同枠除外・合成1.90+）
      - T3 (60-67%): 軸主力ハイブリッド（軸3＋消し1〜2 ＝ 4〜5点・合成2.15+）
      - T4 (52-59%): 軸寄りハイブリッド（軸3＋消し2〜3 ＝ 5〜6点・合成2.30+）
      - T5 (45-51%): 完全均衡ハイブリッド（軸2＋消し3〜5 ＝ 5〜7点・合成2.45+）
      - T6 (38-44%): 消寄ハイブリッド（軸1＋消し4〜7 ＝ 5〜8点・合成2.60+）
      - T7 (32-37%): 準消し・上位堅実（単体オッズ下限なし・消し上位5〜7点・合成2.55+）
      - T8 (26-31%): 標準消し・中穴網羅（単体5.0倍+優先・5〜8点・合成2.80+）
      - T9 (20-25%): 強消し・高配当狙い（単体7.0倍+優先・6〜9点・合成3.30+）
      - T10 (~19%): 極荒れ・大穴挑戦（単体9.0倍+優先・6〜10点・合成3.80+）
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
    distance = int(race.get("distance") or 0)
    is_jra = venue in (JRA_AXIS_OK_VENUES + JRA_UPSET_VENUES)
    surface = str(race.get("surface") or ("芝" if is_jra else "ダート"))
    track_cond = str(race.get("track_condition") or "良")

    if fav1_waku <= 0:
        fav1_waku = int(combos[0]["w1"])
    if tan_fav1 < 1.0:
        tan_fav1 = 3.0

    race_eval_view = dict(race, fav1_waku=fav1_waku, fav1_win_odds=tan_fav1)
    prob_info = calc_fav1_waku_prob(race_eval_view)
    tid = prob_info["tier_id"]
    t_name = prob_info["tier_name"]
    t_synth = prob_info["target_synth"]
    alpha = prob_info["odds_alpha"]
    leg_pow = prob_info["leg_power"]

    waku_leg_scores = calc_waku_leg_scores(
        race.get("horses_info") or [], surface=surface, track_condition=track_cond, venue=venue, distance=distance
    )

    axis_no_zoro = [c for c in combos if (c["w1"] == fav1_waku or c["w2"] == fav1_waku) and (c["w1"] != c["w2"])] or combos
    axis_with_zoro = [c for c in combos if (c["w1"] == fav1_waku or c["w2"] == fav1_waku)] or combos
    fade_pool = [c for c in combos if c["w1"] != fav1_waku and c["w2"] != fav1_waku] or combos

    ranked_axis = rank_combos_by_leg_weight(axis_no_zoro, waku_leg_scores, candidate_limit=8, odds_alpha=alpha, leg_power=leg_pow, zoro_penalty=0.50)
    ranked_axis_z = rank_combos_by_leg_weight(axis_with_zoro, waku_leg_scores, candidate_limit=8, odds_alpha=alpha, leg_power=leg_pow, zoro_penalty=0.50)
    ranked_fade = rank_combos_by_leg_weight(fade_pool, waku_leg_scores, candidate_limit=22, odds_alpha=alpha, leg_power=leg_pow, zoro_penalty=0.50)

    if tid == "T1":
        picked = ranked_axis[:3]
    elif tid == "T2":
        picked = _select_axis_reduce_count(ranked_axis, min_synth=t_synth, max_n=4, min_n=3)
    elif tid == "T3":
        src_t3 = ranked_axis_z if prob_info["strong_partner"] else ranked_axis
        picked = _select_hybrid_portfolio(src_t3, ranked_fade, n_axis=3, n_fade=1, target_synth=t_synth, max_total=5, prefer_fade_expand=False)
    elif tid == "T4":
        picked = _select_hybrid_portfolio(ranked_axis, ranked_fade, n_axis=3, n_fade=2, target_synth=t_synth, max_total=6, prefer_fade_expand=False)
    elif tid == "T5":
        picked = _select_hybrid_portfolio(ranked_axis, ranked_fade, n_axis=2, n_fade=3, target_synth=t_synth, max_total=7, prefer_fade_expand=True)
    elif tid == "T6":
        picked = _select_hybrid_portfolio(ranked_axis, ranked_fade, n_axis=1, n_fade=4, target_synth=t_synth, max_total=8, prefer_fade_expand=True)
    elif tid == "T7":
        picked = _expand_picks_keeping_synth(ranked_fade, min_n=5, max_n=7, min_synth=t_synth, min_single_odds=0.0)
    elif tid == "T8":
        picked = _expand_picks_keeping_synth(ranked_fade, min_n=5, max_n=8, min_synth=t_synth, min_single_odds=5.0)
    elif tid == "T9":
        picked = _expand_picks_keeping_synth(ranked_fade, min_n=6, max_n=9, min_synth=t_synth, min_single_odds=7.0)
    else:
        picked = _expand_picks_keeping_synth(ranked_fade, min_n=6, max_n=10, min_synth=t_synth, min_single_odds=9.0)

    lbl = f"{tid}({fav1_waku}枠:{prob_info['p_waku']}%/単{tan_fav1})"
    res = _make_pick(race, picked, min_synth=1.01, label=lbl, bet_label=bet_label, prob_info=prob_info)
    if res["participate"]:
        res["bucket"] = f"{t_name} ({bet_label})"
    return res


def build_100_strategies(bet_label: str = "枠複") -> List[Tuple[str, Callable[[Dict], Dict]]]:
    """中央・地方共通の100パターン総当たりロジック（v13.4新ロジック＋脚質重視パターン搭載）"""
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

    # v13.4 コア戦略群を先頭に登録
    strats.append(("v13.4_全データ+逃げ先行馬場重み", lambda r: evaluate_v12_standard(r, bet_label)))
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
    v13.4 ハーフマーチンゲール法・シンプル遷移（戻しロジック完全撤廃）:
      - 的中したら無条件で 1戦目へリセット ("HIT")
      - 不的中なら次戦へ進み、10戦目不的中で損切りリセット ("STOP_LOSS")
    """
    return 0, 0, 0, "HIT"


def run_progression_sim(races: List[Dict], eval_fn: Callable[[Dict], Dict], log_skips: bool = False) -> Dict:
    """
    v13.4 追い上げシミュレーション共通エンジン:
      - 9連敗までOK（最大10戦目・10戦目不的中で損切りリセット）
      - 軽傷リセット廃止（未回収的中時は 1〜3戦目:次戦進み / 4〜10戦目:2戦戻し）
    """
    balance, stage_idx = INITIAL_BANKROLL, 0
    set_bet = set_payout = 0
    total_bet = total_payout = withdrawn = w_cnt = stop_loss = 0
    hits = step_backs = step_ups = misses = skips = max_ls = cur_ls = 0
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
                    "p_waku": None, "tier_id": "-",
                    "bought_tickets": [], "hit_ticket": None,
                    "status": "SKIP", "total_bet": 0, "payout": 0,
                    "result_pair": win_str or "-", "result_pop": res_pop, "result_odds": res_odds,
                    "balance": balance,
                })
            continue

        st = STAGES[stage_idx]
        budget = st["late_bet"] if int(r["race_num"]) >= 9 else st["early_bet"]

        picked_list = ev.get("picked") or []
        alloc_map = dict(allocate_bets(picked_list, budget))
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

        bet = sum(t["amount"] for t in bought_tickets) if bought_tickets else budget
        balance -= bet
        total_bet += bet
        set_bet += bet

        if not win_str:
            payout, status = 0, "PENDING"
        elif ev["is_hit"]:
            if hit_ticket and hit_ticket["odds"] > 0:
                payout = int((hit_ticket["amount"] * hit_ticket["odds"]) // 10) * 10
            else:
                payout = int(round(bet * ev["synth_odds"] / 10.0) * 10)
            balance += payout
            total_payout += payout
            set_payout += payout
            hits += 1
            cur_ls = 0

            stage_idx, set_bet, set_payout, status = resolve_next_stage_on_hit(
                stage_idx=stage_idx,
                bet=bet,
                payout=payout,
                set_bet=set_bet,
                set_payout=set_payout,
                synth_odds=float(ev.get("synth_odds") or 0.0),
            )
            if status == "HIT_STEP_BACK":
                step_backs += 1
            elif status == "HIT_STEP_UP":
                step_ups += 1

            if balance >= WITHDRAW_THRESHOLD:
                balance -= WITHDRAW_AMOUNT
                withdrawn += WITHDRAW_AMOUNT
                w_cnt += 1
        else:
            payout = 0
            misses += 1
            cur_ls += 1
            max_ls = max(max_ls, cur_ls)
            if stage_idx >= MAX_STAGE_IDX:
                stop_loss += 1
                stage_idx = 0
                set_bet = 0
                set_payout = 0
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
            flat_alloc = dict(allocate_bets(picked_list, 1000))
            f_bet = sum(flat_alloc.values()) or 1000
            prog_stats[bkt]["flat_bet"] += f_bet
            if ev["is_hit"]:
                if hit_ticket and hit_ticket["odds"] > 0 and hit_ticket["pair"] in flat_alloc:
                    f_pay = int((flat_alloc[hit_ticket["pair"]] * hit_ticket["odds"]) // 10) * 10
                else:
                    f_pay = int(round(f_bet * ev["synth_odds"] / 10.0) * 10)
                prog_stats[bkt]["flat_payout"] += f_pay
        if status in ("HIT", "HIT_STEP_BACK", "HIT_STEP_UP", "HIT_KEEP"):
            prog_stats[bkt]["hits"] += 1

        logs.append({
            "timestamp": r["timestamp"], "race_title": title, "bet_type": ev["bet_type"],
            "stage_name": st_name, "mode_name": bkt, "surface_cond": surf_cond,
            "combos_summary": ev["combos_summary"], "synth_odds": ev.get("synth_odds", 0.0),
            "p_waku": ev.get("p_waku"), "p_horse": ev.get("p_horse"), "tier_id": ev.get("tier_id", "-"),
            "bought_tickets": bought_tickets, "hit_ticket": hit_ticket,
            "status": status, "total_bet": bet, "payout": payout,
            "result_pair": ev["result_pair"], "result_pop": ev.get("result_pop", res_pop),
            "result_odds": ev.get("result_odds", res_odds), "balance": balance,
        })

    def _tier_sort_key(item):
        m = re.match(r"T(\d+):", item[0])
        return (0, int(m.group(1))) if m else (1, item[0])
    prog_stats = dict(sorted(prog_stats.items(), key=_tier_sort_key))

    roi = round((total_payout / total_bet * 100.0), 1) if total_bet > 0 else 0.0
    hr = round((hits / (hits + misses) * 100.0), 1) if (hits + misses) > 0 else 0.0
    return {
        "races": hits + misses, "hits": hits, "step_backs": step_backs, "step_ups": step_ups,
        "misses": misses, "skips": skips, "hit_rate": hr,
        "balance": balance, "withdrawn_total": withdrawn, "withdraw_count": w_cnt,
        "daily_stats": {
            "total_bet": total_bet, "total_payout": total_payout,
            "net_profit": total_payout - total_bet, "recovery_rate": roi,
            "hit_count": hits, "step_back_count": step_backs, "step_up_count": step_ups,
            "miss_count": misses, "skip_count": skips,
            "max_losing_streak": max_ls, "stop_loss_count": stop_loss, "bankrupt_count": 0,
        },
        "prog_stats": prog_stats, "race_logs": logs,
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

    lines = ["# 🟢 v13.4 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象 | 区分 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
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
        "version": "v13.4",
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    }


def save_prod_state(state: Dict) -> None:
    state["version"] = "v13.4"
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(PROD_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
