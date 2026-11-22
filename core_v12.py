import math
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROD_STATE_PATH = DATA_DIR / "wakuren_state_v11_9.json"
DRY_RUN_STATE_PATH = DATA_DIR / "wakuren_dry_run_state_v11_9.json"
CACHE_INDEX_JSON = DATA_DIR / "cache_index.json"
CACHE_LIST_MD = DATA_DIR / "CACHE_LIST.md"

INITIAL_BANKROLL = 520000
WITHDRAW_THRESHOLD = 1040000
WITHDRAW_AMOUNT = 520000
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

# v13.13 マーチンゲール法（前戦×2.0倍 / 1〜10戦目・累計511,500円 / 合成2.0倍以上で全戦プラス回収）
STAGES = [
    {"stage": 1,  "name": "1戦目",  "target": 1000,   "early_bet": 500,    "late_bet": 500},
    {"stage": 2,  "name": "2戦目",  "target": 2000,   "early_bet": 1000,   "late_bet": 1000},
    {"stage": 3,  "name": "3戦目",  "target": 4000,   "early_bet": 2000,   "late_bet": 2000},
    {"stage": 4,  "name": "4戦目",  "target": 8000,   "early_bet": 4000,   "late_bet": 4000},
    {"stage": 5,  "name": "5戦目",  "target": 16000,  "early_bet": 8000,   "late_bet": 8000},
    {"stage": 6,  "name": "6戦目",  "target": 32000,  "early_bet": 16000,  "late_bet": 16000},
    {"stage": 7,  "name": "7戦目",  "target": 64000,  "early_bet": 32000,  "late_bet": 32000},
    {"stage": 8,  "name": "8戦目",  "target": 128000, "early_bet": 64000,  "late_bet": 64000},
    {"stage": 9,  "name": "9戦目",  "target": 256000, "early_bet": 128000, "late_bet": 128000},
    {"stage": 10, "name": "10戦目", "target": 512000, "early_bet": 256000, "late_bet": 256000},
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


def trim_picks_for_budget(picked: List[Dict], total_budget: int, min_synth: float = 1.65) -> List[Dict]:
    """
    実運用予算ガード（例：1戦目500円時に6点以上買って予算超過するのを防止）:
      - budget // 100 を超える点数の場合、スコア上位から budget // 100 点へ絞り込む
        （合成オッズ >= min_synth を維持できる範囲で適用）。
    """
    if not picked:
        return []
    max_affordable = max(1, int(total_budget) // 100)
    if len(picked) <= max_affordable:
        return list(picked)
    trimmed = list(picked[:max_affordable])
    if calc_synth([float(c["odds"]) for c in trimmed]) >= min_synth:
        return trimmed
    return list(picked)


def allocate_bets(
    picked: List[Dict],
    total_budget: int,
    tilt_strength: float = 0.0,
    cover_ratio: float = 0.58,
) -> List[Tuple[str, int]]:
    """
    v13.13 傾斜配分（本線厚め・トリガミ防止）＆ 均等配分 統合エンジン:
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
            min_safe = max(100, int(math.ceil(target / o / 100.0) * 100)) if synth >= 1.20 else 100
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
# ==============================================================================
JRA_G1_NAMES = {
    "フェブラリーS", "高松宮記念", "大阪杯", "桜花賞", "皐月賞", "天皇賞(春)",
    "NHKマイルC", "ヴィクトリアマイル", "オークス", "日本ダービー", "安田記念",
    "宝塚記念", "スプリンターズS", "秋華賞", "菊花賞", "天皇賞(秋)",
    "エリザベス女王杯", "マイルCS", "ジャパンC", "チャンピオンズC",
    "阪神JF", "朝日杯FS", "有馬記念", "ホープフルS", "中山グランドジャンプ", "中山大障害",
}
JRA_G2_NAMES = {
    "京都記念", "中山記念", "チューリップ賞", "弥生賞", "フィリーズレビュー", "金鯱賞",
    "スプリングS", "阪神大賞典", "日経賞", "毎日杯", "ニュージーランドT", "阪神牝馬S",
    "フローラS", "マイラーズC", "青葉賞", "京都新聞杯", "京王杯SC", "目黒記念",
    "札幌記念", "セントウルS", "紫苑S", "ローズS", "セントライト記念", "オールカマー",
    "神戸新聞杯", "毎日王冠", "京都大賞典", "アイルランドT", "富士S", "スワンS",
    "京王杯2歳S", "アルゼンチン共和国杯", "デイリー杯2歳S", "東京スポーツ杯2歳S", "ステイヤーズS", "阪神C",
    "日経新春杯", "アメリカJCC", "プロキオンS",
}
JRA_G3_NAMES = {
    "シルクロードS", "根岸S", "きさらぎ賞", "東京新聞杯", "クイーンC", "共同通信杯",
    "京都牝馬S", "ダイヤモンドS", "阪急杯", "小倉大賞典", "オーシャンS", "中山牝馬S",
    "フラワーC", "ファルコンS", "愛知杯", "マーチS", "ダービー卿CT",
    "チャーチルダウンズC", "アンタレスS", "福島牝馬S", "ユニコーンS", "新潟大賞典",
    "札幌2歳S", "新潟記念", "京成杯AH", "チャレンジC", "シリウスS", "サウジアラビアRC",
    "アルテミスS", "ファンタジーS", "みやこS", "武蔵野S", "福島記念", "京都2歳S", "京阪杯",
}


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
    v13.13: 平場・未勝利・条件戦・障害戦に誤付与された末尾 G1/G2/G3 を除去し、
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
    v13.13 防御的サニタイザ:
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
    v13.13 レースレコード正規化（旧汚染キャッシュを自動修復して安全に評価可能にする）:
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
    v13.13 ゼロサム適正化 脚質×前走4コーナー位置×馬場×距離×競馬場ウェイト (レンジ 0.80〜1.24):
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


# ==============================================================================
# 競馬ラボ コース解析（2024年1月1日〜集計）JRA全10競馬場 実測枠順データ
# ・best5: 枠順ベスト5（勝利数順位 1〜5位: [(枠番, 複勝率%), ...]）
# ・all8 : 全8枠の実測複勝率 {1: %, ..., 8: %}（頭数バイアス補正の率ベース評価に使用）
# ・fav1_rel: 1番人気の信頼度 ("高" / "中" / "低" / None)
# ・sample: "high"（通常施行数） / "low"（長距離等の少標本 → ベイズ縮小0.40適用）
# ==============================================================================
KEIBALAB_COURSE_WAKU_BEST5: Dict[Tuple[str, str, int], Dict] = {
    # --- 東京 (13コース) ---
    ("東京", "芝", 1400): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(7, 20.7), (6, 24.1), (5, 20.4), (8, 21.7), (2, 20.0)],
    },
    ("東京", "芝", 1600): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 23.0), (1, 28.9), (7, 19.6), (5, 20.9), (6, 22.3)],
        "all8": {1: 28.9, 2: 23.0, 3: 20.8, 4: 20.2, 5: 20.9, 6: 22.3, 7: 19.6, 8: 23.0},
    },
    ("東京", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 27.2), (8, 22.6), (4, 25.4), (6, 23.5), (3, 17.8)],
        "all8": {1: 21.3, 2: 22.2, 3: 17.8, 4: 25.4, 5: 23.1, 6: 23.5, 7: 27.2, 8: 22.6},
    },
    ("東京", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 26.0), (3, 28.8), (5, 25.1), (1, 30.9)],
    },
    ("東京", "芝", 2300): {
        "fav1_rel": None, "sample": "low",
        "best5": [(2, 40.0), (6, 57.1), (7, 28.6), (1, 20.0), (5, 20.0)],
    },
    ("東京", "芝", 2400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 27.0), (5, 27.9), (1, 26.2), (7, 26.7)],
    },
    ("東京", "芝", 2500): {
        "fav1_rel": "中", "sample": "low",
        "best5": [(5, 45.5), (2, 44.4), (7, 15.4), (3, 20.0), (6, 18.2)],
    },
    ("東京", "芝", 3400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(8, 28.0)],
    },
    ("東京", "ダート", 1300): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(4, 20.6), (8, 20.8), (2, 24.0), (7, 22.5), (6, 15.5)],
        "all8": {1: 20.0, 2: 24.0, 3: 14.8, 4: 20.6, 5: 18.0, 6: 15.5, 7: 22.5, 8: 20.8},
    },
    ("東京", "ダート", 1400): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(4, 21.1), (2, 20.6), (6, 18.9), (5, 22.5), (8, 19.3)],
        "all8": {1: 16.5, 2: 20.6, 3: 17.3, 4: 21.1, 5: 22.5, 6: 18.9, 7: 19.7, 8: 19.3},
    },
    ("東京", "ダート", 1600): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(8, 24.8), (5, 20.5), (6, 22.7), (7, 20.3), (4, 19.8)],
        "all8": {1: 18.3, 2: 16.8, 3: 18.0, 4: 19.8, 5: 20.5, 6: 22.7, 7: 20.3, 8: 24.8},
    },
    ("東京", "ダート", 2100): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(6, 22.2), (4, 23.2), (3, 24.2), (7, 21.4), (5, 17.2)],
    },
    ("東京", "ダート", 2400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(6, 50.0), (8, 25.0), (5, 25.0), (7, 25.0), (4, 0.0)],
    },
    # --- 中山 (9コース) ---
    ("中山", "芝", 1200): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(5, 24.5), (1, 26.7), (3, 23.8), (6, 16.7), (7, 19.5)],
    },
    ("中山", "芝", 1600): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(1, 24.6), (2, 22.5), (3, 24.3), (4, 23.5), (5, 21.1)],
    },
    ("中山", "芝", 1800): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(7, 26.1), (6, 24.6), (3, 21.3), (4, 27.4), (8, 15.7)],
        "all8": {1: 23.2, 2: 25.0, 3: 21.3, 4: 27.4, 5: 19.8, 6: 24.6, 7: 26.1, 8: 15.7},
    },
    ("中山", "芝", 2000): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(2, 25.9), (6, 20.9), (8, 14.6), (7, 20.3), (1, 24.4)],
    },
    ("中山", "芝", 2200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 22.3), (4, 21.1), (8, 18.3), (5, 14.0), (3, 22.0)],
    },
    ("中山", "芝", 3600): {
        "fav1_rel": None, "sample": "low",
        "best5": [(6, 25.0), (8, 25.0), (1, 33.3)],
    },
    ("中山", "ダート", 1200): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(6, 21.4), (8, 23.8), (7, 22.8), (5, 18.5), (3, 18.1)],
    },
    ("中山", "ダート", 1800): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(8, 25.6), (7, 22.8), (4, 23.1), (6, 21.6), (2, 17.7)],
    },
    ("中山", "ダート", 2400): {
        "fav1_rel": "高", "sample": "low",
        "best5": [(1, 40.5), (8, 18.8), (7, 22.0), (6, 18.0), (3, 22.5)],
    },
    # --- 阪神 (14コース) ---
    ("阪神", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 33.3), (8, 22.1), (2, 29.8), (6, 20.0), (4, 24.2)],
        "all8": {1: 15.7, 2: 29.8, 3: 22.0, 4: 24.2, 5: 19.7, 6: 20.0, 7: 33.3, 8: 22.1},
    },
    ("阪神", "芝", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 21.6), (3, 28.4), (8, 19.1), (6, 19.6), (2, 18.8)],
        "all8": {1: 23.8, 2: 18.8, 3: 28.4, 4: 21.1, 5: 16.1, 6: 19.6, 7: 21.6, 8: 19.1},
    },
    ("阪神", "芝", 1600): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 25.9), (4, 23.2), (6, 25.7), (7, 20.5), (8, 18.1)],
        "all8": {1: 21.4, 2: 19.8, 3: 23.2, 4: 23.2, 5: 25.9, 6: 25.7, 7: 20.5, 8: 18.1},
    },
    ("阪神", "芝", 1800): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(3, 34.0), (6, 33.3), (2, 27.1), (7, 22.6), (4, 26.0)],
    },
    ("阪神", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 27.8), (8, 27.7), (3, 26.7), (1, 31.3), (2, 28.6)],
        "all8": {1: 31.3, 2: 28.6, 3: 26.7, 4: 31.9, 5: 27.8, 6: 23.8, 7: 17.6, 8: 27.7},
    },
    ("阪神", "芝", 2200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 20.4), (3, 34.2), (7, 25.5), (1, 25.7), (6, 20.0)],
    },
    ("阪神", "芝", 2400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(4, 36.2), (3, 41.3), (2, 38.6), (1, 35.7), (8, 23.4)],
        "all8": {1: 35.7, 2: 38.6, 3: 41.3, 4: 36.2, 5: 17.3, 6: 25.5, 7: 18.3, 8: 23.4},
    },
    ("阪神", "芝", 2600): {
        "fav1_rel": "高", "sample": "low",
        "best5": [(2, 40.0), (5, 83.3), (4, 50.0), (1, 20.0), (8, 33.3)],
    },
    ("阪神", "芝", 3000): {
        "fav1_rel": "中", "sample": "low",
        "best5": [(5, 30.0), (7, 33.3), (2, 33.3)],
    },
    ("阪神", "芝", 3200): {
        "fav1_rel": "低", "sample": "low",
        "best5": [(6, 40.0), (8, 14.3), (1, 25.0), (3, 40.0), (4, 20.0)],
    },
    ("阪神", "ダート", 1200): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(8, 23.6), (7, 25.5), (5, 19.6), (6, 20.6), (4, 20.0)],
        "all8": {1: 20.1, 2: 13.1, 3: 17.6, 4: 20.0, 5: 19.6, 6: 20.6, 7: 25.5, 8: 23.6},
    },
    ("阪神", "ダート", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 23.9), (7, 20.7), (4, 20.6), (3, 21.0), (6, 19.6)],
        "all8": {1: 17.4, 2: 20.7, 3: 21.0, 4: 20.6, 5: 17.7, 6: 19.6, 7: 20.7, 8: 23.9},
    },
    ("阪神", "ダート", 1800): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(5, 25.2), (8, 23.8), (7, 21.9), (6, 25.0), (4, 23.9)],
    },
    ("阪神", "ダート", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 26.2), (5, 19.0), (2, 15.2), (8, 27.3), (7, 28.1)],
        "all8": {1: 25.6, 2: 15.2, 3: 25.5, 4: 14.5, 5: 19.0, 6: 26.2, 7: 28.1, 8: 27.3},
    },
    # --- 京都 (13コース) ---
    ("京都", "芝", 1200): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(6, 23.9), (2, 23.1), (8, 19.5), (7, 22.2), (5, 20.3)],
        "all8": {1: 18.3, 2: 23.1, 3: 21.6, 4: 15.4, 5: 20.3, 6: 23.9, 7: 22.2, 8: 19.5},
    },
    ("京都", "芝", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 30.6), (7, 16.5), (5, 21.4), (1, 19.0), (6, 15.7)],
    },
    ("京都", "芝", 1600): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(8, 26.6), (4, 24.2), (6, 20.8), (7, 20.1), (5, 27.8)],
    },
    ("京都", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 25.8), (7, 23.8), (5, 26.1), (2, 28.6), (6, 21.4)],
        "all8": {1: 27.2, 2: 28.6, 3: 27.6, 4: 25.8, 5: 26.1, 6: 21.4, 7: 23.8, 8: 25.8},
    },
    ("京都", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 28.0), (8, 28.7), (1, 30.8), (5, 23.7), (4, 22.9)],
    },
    ("京都", "芝", 2200): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(3, 30.2), (8, 22.5), (7, 20.2), (6, 25.0), (1, 33.3)],
    },
    ("京都", "芝", 2400): {
        "fav1_rel": "高", "sample": "high",
        "best5": [(7, 24.7), (5, 31.1), (1, 22.8), (4, 19.1)],
    },
    ("京都", "芝", 3000): {
        "fav1_rel": "中", "sample": "low",
        "best5": [(7, 26.7), (2, 30.0), (4, 8.3), (8, 37.5)],
    },
    ("京都", "芝", 3200): {
        "fav1_rel": "高", "sample": "low",
        "best5": [(3, 25.0), (8, 25.0), (5, 25.0)],
    },
    ("京都", "ダート", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 26.0), (8, 24.2), (6, 22.8), (3, 16.7), (5, 20.0)],
    },
    ("京都", "ダート", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 24.9), (7, 21.4), (8, 21.5), (6, 19.7), (3, 18.3)],
        "all8": {1: 20.5, 2: 18.0, 3: 18.3, 4: 20.8, 5: 24.9, 6: 19.7, 7: 21.4, 8: 21.5},
    },
    ("京都", "ダート", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 28.4), (6, 24.3), (5, 25.6), (7, 24.4), (4, 22.0)],
    },
    ("京都", "ダート", 1900): {
        "fav1_rel": "中", "sample": "high",
        "best5": [(8, 25.0), (7, 21.2), (6, 25.5), (3, 28.4), (4, 22.4)],
    },
    # --- 新潟 (9コース + 芝2000内回り専用キー) ---
    ("新潟", "芝", 1000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 39.5), (7, 19.6), (6, 23.5), (4, 16.9), (5, 17.8)],
        "all8": {1: 4.2, 2: 12.4, 3: 7.3, 4: 16.9, 5: 17.8, 6: 23.5, 7: 19.6, 8: 39.5},
    },
    ("新潟", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(4, 23.6), (7, 25.9), (5, 26.0), (6, 24.3), (3, 13.0)],
        "all8": {1: 6.2, 2: 10.3, 3: 13.0, 4: 23.6, 5: 26.0, 6: 24.3, 7: 25.9, 8: 20.0},
    },
    ("新潟", "芝", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 20.1), (3, 24.6), (6, 25.0), (2, 16.7), (5, 19.0)],
        "all8": {1: 17.9, 2: 16.7, 3: 24.6, 4: 14.8, 5: 19.0, 6: 25.0, 7: 16.7, 8: 20.1},
    },
    ("新潟", "芝", 1600): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 19.7), (7, 21.6), (4, 25.4), (6, 27.8), (5, 21.9)],
        "all8": {1: 21.6, 2: 19.4, 3: 20.4, 4: 25.4, 5: 21.9, 6: 27.8, 7: 21.6, 8: 19.7},
    },
    ("新潟", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 23.0), (6, 22.9), (8, 23.8), (5, 25.0), (7, 22.9)],
        "all8": {1: 15.7, 2: 21.4, 3: 23.0, 4: 22.0, 5: 25.0, 6: 22.9, 7: 22.9, 8: 23.8},
    },
    ("新潟", "芝", 2000): {
        # 古馬外回り（デフォルト）
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 27.3), (8, 20.6), (1, 31.0), (7, 22.4), (6, 23.2)],
        "all8": {1: 31.0, 2: 21.7, 3: 22.4, 4: 21.6, 5: 27.3, 6: 23.2, 7: 22.4, 8: 20.6},
    },
    ("新潟", "芝内", 2000): {
        # 2歳・3歳未勝利内回り
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 23.5), (6, 29.0), (5, 30.4), (2, 17.7), (7, 18.8)],
        "all8": {1: 6.8, 2: 17.7, 3: 19.4, 4: 20.9, 5: 30.4, 6: 29.0, 7: 18.8, 8: 23.5},
    },
    ("新潟", "芝", 2200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(4, 21.7), (2, 26.5), (8, 23.6), (7, 32.7), (5, 18.8)],
        "all8": {1: 9.1, 2: 26.5, 3: 19.0, 4: 21.7, 5: 18.8, 6: 18.8, 7: 32.7, 8: 23.6},
    },
    ("新潟", "ダート", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 27.5), (5, 19.5), (3, 21.7), (4, 19.2), (7, 23.5)],
        "all8": {1: 16.7, 2: 16.4, 3: 21.7, 4: 19.2, 5: 19.5, 6: 27.5, 7: 23.5, 8: 22.2},
    },
    ("新潟", "ダート", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 22.2), (5, 25.1), (8, 21.2), (7, 23.4), (3, 24.5)],
        "all8": {1: 16.7, 2: 20.3, 3: 24.5, 4: 22.7, 5: 25.1, 6: 22.2, 7: 23.4, 8: 21.2},
    },
    # --- 福島 (7コース) ---
    ("福島", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 21.3), (1, 25.7), (2, 22.9), (6, 21.3), (4, 18.4)],
        "all8": {1: 25.7, 2: 22.9, 3: 21.3, 4: 18.4, 5: 17.9, 6: 21.3, 7: 19.9, 8: 20.6},
    },
    ("福島", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 22.2), (1, 27.3), (7, 23.3), (2, 21.1), (3, 27.6)],
        "all8": {1: 27.3, 2: 21.1, 3: 27.6, 4: 22.2, 5: 22.2, 6: 13.3, 7: 23.3, 8: 12.6},
    },
    ("福島", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 26.1), (7, 23.9), (3, 24.7), (4, 22.2), (1, 21.8)],
        "all8": {1: 21.8, 2: 17.3, 3: 24.7, 4: 22.2, 5: 26.1, 6: 15.3, 7: 23.9, 8: 17.5},
    },
    ("福島", "芝", 2600): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 20.6), (8, 29.4), (1, 23.5), (7, 17.6), (5, 16.7)],
        "all8": {1: 23.5, 2: 29.3, 3: 16.9, 4: 19.4, 5: 16.7, 6: 20.6, 7: 17.6, 8: 29.4},
    },
    ("福島", "ダート", 1150): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 22.0), (3, 21.8), (7, 19.8), (4, 21.5), (5, 18.8)],
        "all8": {1: 20.0, 2: 18.7, 3: 21.8, 4: 21.5, 5: 18.8, 6: 16.7, 7: 19.8, 8: 22.0},
    },
    ("福島", "ダート", 1700): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 23.9), (8, 20.6), (7, 21.0), (4, 19.9), (2, 23.5)],
        "all8": {1: 16.2, 2: 23.5, 3: 20.7, 4: 19.9, 5: 17.5, 6: 23.9, 7: 21.0, 8: 20.6},
    },
    ("福島", "ダート", 2400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(8, 50.0), (3, 25.0), (4, 25.0), (1, 25.0), (7, 33.3)],
        "all8": {1: 25.0, 2: 0.0, 3: 25.0, 4: 25.0, 5: 0.0, 6: 16.7, 7: 33.3, 8: 50.0},
    },
    # --- 中京 (9コース) ---
    ("中京", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 29.0), (8, 14.0), (4, 22.2), (6, 16.4), (7, 12.2)],
        "all8": {1: 26.9, 2: 16.4, 3: 29.0, 4: 22.2, 5: 31.5, 6: 16.4, 7: 12.2, 8: 14.0},
    },
    ("中京", "芝", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 18.0), (6, 17.9), (3, 23.4), (8, 17.1), (4, 25.8)],
        "all8": {1: 17.6, 2: 19.1, 3: 23.4, 4: 25.8, 5: 25.0, 6: 17.9, 7: 18.0, 8: 17.1},
    },
    ("中京", "芝", 1600): {
        "fav1_rel": None, "sample": "high",
        "best5": [(1, 32.2), (8, 21.2), (5, 20.2), (2, 22.8), (6, 22.7)],
        "all8": {1: 32.2, 2: 22.8, 3: 22.4, 4: 20.3, 5: 20.2, 6: 22.7, 7: 18.5, 8: 21.2},
    },
    ("中京", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 28.9), (4, 24.4), (8, 23.7), (5, 22.1), (1, 29.9)],
        "all8": {1: 29.9, 2: 31.5, 3: 22.5, 4: 24.4, 5: 22.1, 6: 28.9, 7: 17.3, 8: 23.7},
    },
    ("中京", "芝", 2200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(2, 29.7), (5, 26.1), (6, 24.3), (8, 23.6), (7, 18.0)],
        "all8": {1: 30.5, 2: 29.7, 3: 17.5, 4: 25.0, 5: 26.1, 6: 24.3, 7: 18.0, 8: 23.6},
    },
    ("中京", "ダート", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 22.7), (8, 24.9), (1, 18.4), (2, 26.0), (6, 16.4)],
        "all8": {1: 18.4, 2: 26.0, 3: 15.3, 4: 17.4, 5: 22.7, 6: 16.4, 7: 21.3, 8: 24.9},
    },
    ("中京", "ダート", 1400): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 19.7), (7, 23.7), (6, 22.3), (4, 17.6), (2, 18.2)],
        "all8": {1: 17.3, 2: 18.2, 3: 19.7, 4: 17.6, 5: 19.0, 6: 22.3, 7: 23.7, 8: 21.9},
    },
    ("中京", "ダート", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 22.0), (6, 25.1), (7, 24.3), (5, 22.4), (3, 21.0)],
        "all8": {1: 21.6, 2: 23.5, 3: 21.0, 4: 22.8, 5: 22.4, 6: 25.1, 7: 24.3, 8: 22.0},
    },
    ("中京", "ダート", 1900): {
        "fav1_rel": None, "sample": "high",
        "best5": [(1, 26.5), (2, 24.3), (5, 24.4), (3, 22.4), (4, 21.4)],
        "all8": {1: 26.5, 2: 24.3, 3: 22.4, 4: 21.4, 5: 24.4, 6: 17.6, 7: 24.7, 8: 16.7},
    },
    # --- 小倉 (7コース) ---
    ("小倉", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 20.9), (3, 21.5), (7, 19.0), (1, 19.0), (5, 17.1)],
        "all8": {1: 19.0, 2: 20.2, 3: 21.5, 4: 20.2, 5: 17.1, 6: 16.9, 7: 19.0, 8: 20.9},
    },
    ("小倉", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(2, 22.4), (8, 17.1), (5, 22.5), (7, 26.6), (3, 24.7)],
        "all8": {1: 26.5, 2: 22.4, 3: 24.7, 4: 21.1, 5: 22.5, 6: 19.5, 7: 26.6, 8: 17.1},
    },
    ("小倉", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(4, 21.8), (8, 23.0), (5, 22.5), (2, 23.7), (7, 16.9)],
        "all8": {1: 16.3, 2: 23.7, 3: 26.7, 4: 21.8, 5: 22.5, 6: 21.3, 7: 16.9, 8: 23.0},
    },
    ("小倉", "芝", 2600): {
        "fav1_rel": None, "sample": "low",
        "best5": [(1, 35.7), (4, 34.4), (6, 16.2), (8, 27.0), (5, 20.6)],
        "all8": {1: 35.7, 2: 17.2, 3: 23.3, 4: 34.4, 5: 20.6, 6: 16.2, 7: 10.8, 8: 27.0},
    },
    ("小倉", "ダート", 1000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 21.0), (8, 22.1), (5, 23.8), (3, 23.0), (4, 23.7)],
        "all8": {1: 29.3, 2: 30.7, 3: 23.0, 4: 23.7, 5: 23.8, 6: 21.0, 7: 16.3, 8: 22.1},
    },
    ("小倉", "ダート", 1700): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 21.1), (1, 22.6), (6, 22.6), (5, 19.4), (4, 19.9)],
        "all8": {1: 22.6, 2: 19.6, 3: 19.1, 4: 19.9, 5: 19.4, 6: 22.6, 7: 19.7, 8: 21.1},
    },
    ("小倉", "ダート", 2400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(4, 25.0), (3, 60.0), (1, 25.0), (6, 25.0), (8, 12.5)],
        "all8": {1: 25.0, 2: 25.0, 3: 60.0, 4: 25.0, 5: 12.5, 6: 25.0, 7: 14.3, 8: 12.5},
    },
    # --- 札幌 (8コース) ---
    ("札幌", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 28.7), (5, 22.5), (3, 21.3), (6, 17.3), (1, 25.0)],
        "all8": {1: 25.0, 2: 21.8, 3: 21.3, 4: 21.6, 5: 22.5, 6: 17.3, 7: 19.3, 8: 28.7},
    },
    ("札幌", "芝", 1500): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 27.6), (5, 29.0), (6, 23.2), (8, 21.4), (4, 27.9)],
        "all8": {1: 20.8, 2: 33.3, 3: 17.3, 4: 27.9, 5: 29.0, 6: 23.2, 7: 27.6, 8: 21.4},
    },
    ("札幌", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 34.3), (6, 27.8), (8, 29.4), (5, 24.5), (4, 24.7)],
        "all8": {1: 27.7, 2: 33.8, 3: 23.5, 4: 24.7, 5: 24.5, 6: 27.8, 7: 34.3, 8: 29.4},
    },
    ("札幌", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(4, 21.6), (3, 22.9), (2, 25.3), (5, 19.1), (1, 22.7)],
        "all8": {1: 22.7, 2: 25.3, 3: 22.9, 4: 21.6, 5: 19.1, 6: 23.7, 7: 26.3, 8: 20.0},
    },
    ("札幌", "芝", 2600): {
        "fav1_rel": None, "sample": "low",
        "best5": [(6, 29.4), (5, 37.5), (3, 39.3), (8, 21.6), (2, 29.4)],
        "all8": {1: 10.5, 2: 29.4, 3: 39.3, 4: 10.3, 5: 37.5, 6: 29.4, 7: 17.1, 8: 21.6},
    },
    ("札幌", "ダート", 1000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 29.9), (6, 28.8), (8, 29.9), (3, 38.9), (2, 27.8)],
        "all8": {1: 18.9, 2: 27.8, 3: 38.9, 4: 16.2, 5: 23.4, 6: 28.8, 7: 29.9, 8: 29.9},
    },
    ("札幌", "ダート", 1700): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 26.9), (6, 24.4), (8, 25.5), (3, 20.2), (4, 20.6)],
        "all8": {1: 27.3, 2: 19.7, 3: 20.2, 4: 20.6, 5: 26.9, 6: 24.4, 7: 22.9, 8: 25.5},
    },
    ("札幌", "ダート", 2400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(8, 50.0), (7, 28.6), (2, 75.0), (4, 25.0), (5, 28.6)],
        "all8": {1: 0.0, 2: 75.0, 3: 0.0, 4: 25.0, 5: 28.6, 6: 0.0, 7: 28.6, 8: 50.0},
    },
    # --- 函館 (7コース) ---
    ("函館", "芝", 1200): {
        "fav1_rel": None, "sample": "high",
        "best5": [(5, 23.2), (2, 32.0), (3, 26.9), (4, 25.9), (8, 20.5)],
        "all8": {1: 22.6, 2: 32.0, 3: 26.9, 4: 25.9, 5: 23.2, 6: 25.5, 7: 16.6, 8: 20.5},
    },
    ("函館", "芝", 1800): {
        "fav1_rel": None, "sample": "high",
        "best5": [(3, 34.9), (1, 28.8), (4, 22.0), (2, 35.5), (7, 17.3)],
        "all8": {1: 28.8, 2: 35.5, 3: 34.9, 4: 22.0, 5: 14.7, 6: 18.9, 7: 17.3, 8: 20.4},
    },
    ("函館", "芝", 2000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(6, 26.2), (4, 22.0), (3, 17.0), (8, 18.2), (7, 21.9)],
        "all8": {1: 23.3, 2: 24.4, 3: 17.0, 4: 22.0, 5: 30.4, 6: 26.2, 7: 21.9, 8: 18.2},
    },
    ("函館", "芝", 2600): {
        "fav1_rel": None, "sample": "low",
        "best5": [(4, 40.9), (7, 16.7), (6, 33.3), (1, 31.6), (2, 28.6)],
        "all8": {1: 31.6, 2: 28.6, 3: 27.3, 4: 40.9, 5: 24.0, 6: 33.3, 7: 16.7, 8: 13.8},
    },
    ("函館", "ダート", 1000): {
        "fav1_rel": None, "sample": "high",
        "best5": [(8, 28.3), (7, 27.8), (6, 30.2), (1, 40.4), (5, 18.9)],
        "all8": {1: 40.4, 2: 23.1, 3: 27.5, 4: 20.8, 5: 18.9, 6: 30.2, 7: 27.8, 8: 28.3},
    },
    ("函館", "ダート", 1700): {
        "fav1_rel": None, "sample": "high",
        "best5": [(7, 31.2), (6, 24.9), (8, 18.3), (4, 23.1), (3, 25.9)],
        "all8": {1: 33.0, 2: 25.5, 3: 25.9, 4: 23.1, 5: 20.5, 6: 24.9, 7: 31.2, 8: 18.3},
    },
    ("函館", "ダート", 2400): {
        "fav1_rel": None, "sample": "low",
        "best5": [(7, 36.4), (8, 66.7), (3, 42.9), (1, 28.6), (4, 14.3)],
        "all8": {1: 28.6, 2: 14.3, 3: 42.9, 4: 14.3, 5: 22.2, 6: 0.0, 7: 36.4, 8: 66.7},
    },
}

_KL_RANK_BONUS = (0.024, 0.016, 0.010, 0.005, 0.002)


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
    v13.13 JRA全10競馬場・競馬ラボ実測枠順データ（全8枠複勝率＋枠順ベスト5順位）統合版 枠番重み係数:
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
    race_name: str = "",
) -> Dict[str, float]:
    """
    v13.13 実力加重平均型 枠スコア算出 (レンジ 0.82〜1.22):
      - 同枠内の大穴馬(単勝100倍)が枠スコアを独占するバグを防ぐため、
        各馬の市場支持度 (1 / win_odds^0.85) で加重平均した枠スコアを算出。
      - 前走4コーナー通過順 (prev_c4) と 1コーナー脚質 (prev_c1) の両方を評価。
      - レース内に前走「逃げ」が4頭以上いる場合、逃げ×0.94、差し×1.05。
      - コース距離別枠バイアス・動画場別人気ROIを統合し、0.82〜1.22 の健全レンジに収める。
    """
    clean_horses = sanitize_horses_info(horses_info)
    odds_ranked = sorted(
        [(float(h.get("win_odds") or 0.0), int(h.get("umaban") or 0)) for h in clean_horses if float(h.get("win_odds") or 0.0) >= 1.0],
        key=lambda x: x[0],
    )
    pop_by_uma = {uma: idx + 1 for idx, (_, uma) in enumerate(odds_ranked)}

    parsed = []
    nige_count = 0
    for h in clean_horses:
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

    course_w = get_course_waku_weights(venue, surface=surface, distance=distance, race_name=race_name)
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
# v13.13 ハービル精密キャリブレーション済み ロジット型1番人気『枠』連対確率モデル
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

ENV_LOGIT_SCALE = 0.45
ENV_LOGIT_CLAMP = 0.35
MIN_SYNTH_HARD_FLOOR = 1.65   # 全Tier共通の絶対下限合成オッズ（連敗抑止＆トリガミゼロ設計）
DEFENSIVE_STAGE_IDX = 4       # 5戦目(4連敗)以降に自動で連敗ストッパー発動
DEFENSIVE_SYNTH_CAP = 1.95    # 防衛フェーズ時の目標合成上限（マーチンゲール回収ラインと高的中率40%の最適点）
ZORO_PENALTY_DEFAULT = 0.50
ZORO_PENALTY_STRONG = 0.95
STRONG_ZORO_MAX_ODDS = 10.0   # 単勝10倍未満 (< 10.0)
STRONG_ZORO_MAX_POP = 5       # または単勝上位5人気以内 (<= 5)
SKIP_TIERS: Tuple[str, ...] = ()

# Tier 1 〜 Tier 20 定義（v13.13: 連敗9連敗以内・損切りゼロ特化 合成オッズ再設計 1.85〜2.40倍 × 全盤面連続チルト × 5戦目防衛キャップ）
# (tier_id, min_prob, short_name, mode_group, target_synth, axis_tilt, odds_alpha, leg_power, alloc_tilt)
#   ・全Tierで極端な高倍率(3〜7倍)による的中率低下と連敗沼を解消し、1.85〜2.40倍の高回転ゾーンへ圧縮
#   ・T1(×1.90)→T20(×0.50) の連続軸チルトにより、高確率帯は1人気枠重視・低確率帯は1人気枠抑制を保ちつつ
#     強制プール除外による取りこぼし（連敗延長）を根絶
#   ・5戦目(4連敗)以降は自動で目標合成上限を 1.95倍 へ引き締める連敗ストッパーを搭載
TIER_SPECS = [
    ("T1",  0.91, "T1:極鉄板重視(91%〜)",     "軸",   1.85, 1.90, 1.18, 0.82, 0.55),
    ("T2",  0.86, "T2:超鉄板重視(86〜90%)",   "軸",   1.88, 1.83, 1.16, 0.85, 0.52),
    ("T3",  0.82, "T3:鉄板厳選重視(82〜85%)", "軸",   1.91, 1.75, 1.14, 0.87, 0.50),
    ("T4",  0.79, "T4:最強軸混合(79〜81%)",   "混合", 1.94, 1.68, 1.12, 0.90, 0.47),
    ("T5",  0.76, "T5:強軸混合上(76〜78%)",   "混合", 1.97, 1.61, 1.10, 0.93, 0.44),
    ("T6",  0.73, "T6:強軸混合下(73〜75%)",   "混合", 1.99, 1.53, 1.08, 0.95, 0.42),
    ("T7",  0.70, "T7:軸優勢混合上(70〜72%)", "混合", 2.02, 1.46, 1.06, 0.98, 0.39),
    ("T8",  0.67, "T8:軸優勢混合下(67〜69%)", "混合", 2.05, 1.38, 1.04, 1.00, 0.37),
    ("T9",  0.64, "T9:軸寄り混合上(64〜66%)", "混合", 2.08, 1.31, 1.02, 1.03, 0.34),
    ("T10", 0.61, "T10:軸寄り混合下(61〜63%)", "混合", 2.11, 1.24, 1.00, 1.06, 0.31),
    ("T11", 0.58, "T11:均衡混合上(58〜60%)",  "混合", 2.14, 1.16, 0.98, 1.08, 0.29),
    ("T12", 0.55, "T12:均衡混合中(55〜57%)",  "混合", 2.17, 1.09, 0.96, 1.11, 0.26),
    ("T13", 0.52, "T13:均衡混合下(52〜54%)",  "混合", 2.20, 1.02, 0.94, 1.14, 0.23),
    ("T14", 0.48, "T14:消し寄り混合(48〜51%)", "混合", 2.23, 0.94, 0.92, 1.16, 0.21),
    ("T15", 0.45, "T15:消し優勢混合(45〜47%)", "混合", 2.26, 0.87, 0.90, 1.19, 0.18),
    ("T16", 0.41, "T16:準消し抑止(41〜44%)",  "消し", 2.28, 0.79, 0.88, 1.21, 0.16),
    ("T17", 0.37, "T17:標準消し抑止(37〜40%)", "消し", 2.31, 0.72, 0.86, 1.24, 0.13),
    ("T18", 0.33, "T18:強消し抑止(33〜36%)",  "消し", 2.34, 0.65, 0.84, 1.27, 0.10),
    ("T19", 0.29, "T19:波乱抑止(29〜32%)",    "消し", 2.37, 0.57, 0.82, 1.29, 0.08),
    ("T20", 0.00, "T20:極荒れ抑止(〜28%)",    "消し", 2.40, 0.50, 0.80, 1.32, 0.05),
]
TIER_SPEC_MAP: Dict[str, Tuple] = {spec[0]: spec for spec in TIER_SPECS}


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
    """後方互換ユーティリティ（v13.13のTier判定ではA/B分割を廃止し、参照用指標のみ算出）"""
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
    v13.13 Exact Harville 枠連対確率:
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
    v13.13 ハービル精密キャリブレーション版 1番人気『枠』実効連対確率モデル:
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
    fav1_c4 = None
    fav1_pn = 14
    for h in horses:
        if (fav1_uma > 0 and int(h.get("umaban") or 0) == fav1_uma) or (
            fav1_uma <= 0 and int(h.get("waku") or 0) == fav1_waku and abs(float(h.get("win_odds") or 0.0) - tan_fav1) < 0.05
        ):
            fav1_c4 = h.get("prev_c4")
            if not fav1_c4 and h.get("prev_c4_list"):
                fav1_c4 = calc_median_nearest(h.get("prev_c4_list"))
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
    v13.13 有力2頭同居枠の検出（重複馬番・取消馬を自動除去済み）:
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


def _score_combos_by_leg_weight(
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
) -> List[Tuple[Dict, float]]:
    if not pool:
        return []
    sub = list(pool[:candidate_limit])
    alpha = odds_alpha if odds_alpha is not None else get_field_size_odds_alpha(horse_count)

    out: List[Tuple[Dict, float]] = []
    for c in sub:
        o = float(c.get("odds") or 999.0)
        if o <= 0:
            continue
        if waku_leg_scores:
            s1 = float(waku_leg_scores.get(str(c["w1"]), 1.0))
            s2 = float(waku_leg_scores.get(str(c["w2"]), 1.0))
            leg_boost = ((s1 + s2) / 2.0) ** leg_power
        else:
            leg_boost = 1.0
        if c.get("w1") == c.get("w2"):
            w_id = int(c.get("w1") or 0)
            zp = strong_zoro_penalty if (strong_zoro_wakus and w_id in strong_zoro_wakus) else zoro_penalty
        else:
            zp = 1.0
        tilt = 1.0
        if axis_tilt is not None and axis_tilt > 0 and fav1_waku > 0:
            is_ax = (int(c.get("w1") or 0) == fav1_waku or int(c.get("w2") or 0) == fav1_waku)
            tilt = float(axis_tilt) if is_ax else 1.0
        sc = ((1.0 / o) ** alpha) * leg_boost * zp * tilt
        out.append((c, sc))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _select_flexible_portfolio(
    scored_pool: List[Tuple[Dict, float]],
    target_synth: float,
    hard_floor: float = MIN_SYNTH_HARD_FLOOR,
    max_candidates: int = 14,
    mode_group: str = "混合",
) -> List[Dict]:
    """
    v13.13 ワイドレンジ(1.70〜7.00倍)対応・柔軟点数ポートフォリオ選択:
      - 目標合成オッズ target_synth に連動して候補窓(14〜28件)・単体オッズ上限・相対スコア下限を動的スケール。
      - 上位軸帯(1.70〜2.50倍)では超穴ノイズの詰め込みを防ぎつつ、
        下位波乱帯(4.71〜7.00倍)では高配当組番を自然に組み込んで目標合成を達成する。
      - 枠連1人気(pop==1)もオッズが許容する限り自然評価で組み込み、合成オッズ絶対下限 1.70倍を厳守。
    """
    if not scored_pool:
        return []
    eff_target = max(hard_floor, float(target_synth))
    top_sc = scored_pool[0][1]

    cand_win = max(max_candidates, 16 if eff_target <= 2.6 else 22)
    max_o_cap = max(35.0, eff_target * 16.0)
    min_sr = 0.08 if eff_target <= 3.0 else 0.04

    def _filter_quality(pool: List[Tuple[Dict, float]], eff_t: float, strict: bool = True) -> List[Tuple[Dict, float]]:
        out: List[Tuple[Dict, float]] = []
        for c, sc in pool[:cand_win]:
            o = float(c.get("odds") or 0.0)
            if o < eff_t:
                continue
            if strict and (o > max_o_cap or sc < top_sc * min_sr):
                continue
            out.append((c, sc))
        return out

    cands = _filter_quality(scored_pool, eff_target, strict=True)
    if not cands:
        cands = _filter_quality(scored_pool, eff_target, strict=False)
    if not cands:
        eff_target = hard_floor
        cands = [(c, sc) for c, sc in scored_pool if float(c.get("odds") or 0.0) >= hard_floor]
    if not cands:
        return []

    cap = 1.0 / eff_target

    def _pack(sub_cands: List[Tuple[Dict, float]]) -> Tuple[List[Dict], float]:
        picked: List[Dict] = []
        inv_sum = 0.0
        val_sum = 0.0
        for c, sc in sub_cands:
            w = 1.0 / float(c["odds"])
            if inv_sum + w <= cap + 1e-9:
                c_copy = dict(c)
                c_copy["_model_score"] = sc
                picked.append(c_copy)
                inv_sum += w
                val_sum += sc
        return picked, val_sum

    best_p, best_v = _pack(cands)
    for skip_i in (1, 2):
        if skip_i < len(cands):
            p2, v2 = _pack(cands[skip_i:])
            if p2 and v2 > best_v * 1.03:
                best_p, best_v = p2, v2

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


def evaluate_v12_standard(race: Dict, bet_label: str = "枠連", stage_idx: Optional[int] = None) -> Dict:
    """
    v13.13 予測連対率 細分化20段階 (T1〜T20) × 柔軟点数ポートフォリオモデル:
      - A/B構造分割を廃止し、ハービル精密キャリブレーション済み p_waku 単独で T1〜T20 を決定。
      - 枠連1番人気(pop==1)の無条件除外を廃止。
      - 買い目点数の固定制限を撤廃し、各Tierの目標合成オッズ（T1:1.80〜T20:3.32、絶対下限1.80倍）を
        満たす範囲で柔軟に最適組番をピックアップする。
    """
    clean_race = sanitize_race_record(race)
    venue = str(clean_race.get("venue", ""))
    rname = str(clean_race.get("race_name", "")) + " " + str(clean_race.get("race_class", ""))

    if any(ex in venue for ex in EXCLUDED_VENUES) or ("障害" in rname):
        return {"participate": False, "skip_reason": "除外競馬場/障害戦(見送り)"}

    combos = clean_race.get("real_odds") or []
    if len(combos) < 2:
        return {"participate": False, "skip_reason": "オッズ未取得(見送り)"}

    tan_fav1 = float(clean_race.get("fav1_win_odds") or 0.0)
    fav1_waku = int(clean_race.get("fav1_waku") or 0)
    distance = int(clean_race.get("distance") or 0)
    is_jra = venue in (JRA_AXIS_OK_VENUES + JRA_UPSET_VENUES)
    surface = str(clean_race.get("surface") or ("芝" if is_jra else "ダート"))
    track_cond = str(clean_race.get("track_condition") or "良")

    if fav1_waku <= 0:
        fav1_waku = int(combos[0]["w1"])
    if tan_fav1 < 1.0:
        tan_fav1 = 3.0

    race_eval_view = dict(clean_race, fav1_waku=fav1_waku, fav1_win_odds=tan_fav1)
    prob_info = calc_fav1_waku_prob(race_eval_view)
    tid = prob_info["tier_id"]
    t_name = prob_info["tier_name"]
    t_group = prob_info["tier_group"]
    eff_stage = int(stage_idx) if stage_idx is not None else int(race.get("_stage_idx") or 0)
    t_synth = max(MIN_SYNTH_HARD_FLOOR, float(prob_info["target_synth"]))
    defensive_active = eff_stage >= DEFENSIVE_STAGE_IDX
    if defensive_active:
        t_synth = min(t_synth, DEFENSIVE_SYNTH_CAP)
    axis_tilt = prob_info["axis_tilt"]
    alpha = prob_info["odds_alpha"]
    leg_pow = prob_info["leg_power"]

    if tid in SKIP_TIERS:
        return {"participate": False, "skip_reason": f"{tid}見送り設定"}

    horses_info = clean_race.get("horses_info") or []
    waku_leg_scores = calc_waku_leg_scores(
        horses_info, surface=surface, track_condition=track_cond, venue=venue, distance=distance, race_name=rname
    )
    strong_zoro_wakus = get_strong_zoro_wakus(horses_info)

    # v13.13: 全Tierで全組番を対象に連続軸チルト(1.90→0.50)を適用し、強制除外による連敗沼を根絶
    scored_pool = _score_combos_by_leg_weight(
        combos, waku_leg_scores, candidate_limit=36, odds_alpha=alpha, leg_power=leg_pow,
        zoro_penalty=ZORO_PENALTY_DEFAULT, strong_zoro_wakus=strong_zoro_wakus,
        strong_zoro_penalty=ZORO_PENALTY_STRONG, fav1_waku=fav1_waku, axis_tilt=axis_tilt,
    )

    picked = _select_flexible_portfolio(
        scored_pool, target_synth=t_synth, hard_floor=MIN_SYNTH_HARD_FLOOR, mode_group="混合"
    )

    def_tag = "🛡️防衛" if defensive_active else ""
    lbl = f"{tid}{def_tag}({fav1_waku}枠:{prob_info['p_waku']}%/単{tan_fav1})"
    res = _make_pick(clean_race, picked, min_synth=MIN_SYNTH_HARD_FLOOR, label=lbl, bet_label=bet_label, prob_info=prob_info)
    if res["participate"]:
        res["bucket"] = f"{t_name} ({bet_label})"
        res["strong_zoro_wakus"] = sorted(strong_zoro_wakus)
        res["alloc_tilt"] = float(prob_info.get("alloc_tilt") or 0.0)
    return res


def build_100_strategies(bet_label: str = "枠複") -> List[Tuple[str, Callable[[Dict], Dict]]]:
    """中央・地方共通の100パターン総当たりロジック（v13.13新ロジック＋脚質重視パターン搭載）"""
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

    # v13.13 コア戦略群を先頭に登録
    strats.append(("v13.13_全データ+逃げ先行馬場重み", lambda r: evaluate_v12_standard(r, bet_label)))
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
    v13.13 マーチンゲール法・シンプル遷移（戻しロジック完全撤廃）:
      - 的中したら無条件で 1戦目へリセット ("HIT")
      - 不的中なら次戦へ進み、10戦目不的中で損切りリセット ("STOP_LOSS")
    """
    return 0, 0, 0, "HIT"


def run_progression_sim(races: List[Dict], eval_fn: Callable[[Dict], Dict], log_skips: bool = False) -> Dict:
    """
    v13.13 追い上げシミュレーション共通エンジン:
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
        r_with_stage = dict(r, _stage_idx=stage_idx)
        ev = eval_fn(r_with_stage)
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

        raw_picked = ev.get("picked") or []
        picked_list = trim_picks_for_budget(raw_picked, budget, min_synth=MIN_SYNTH_HARD_FLOOR)
        alloc_tilt = float(ev.get("alloc_tilt") or 0.0)
        eff_cover = min(0.92, 0.62 + 0.05 * max(0, stage_idx - 2))
        alloc_map = dict(allocate_bets(picked_list, budget, tilt_strength=alloc_tilt, cover_ratio=eff_cover))

        # 予算トリム後に的中有無を再確定
        hit_pair_str = None
        if "-" in win_str:
            try:
                a_s, b_s = win_str.split("-")
                hw1, hw2 = min(int(a_s), int(b_s)), max(int(a_s), int(b_s))
                for c in picked_list:
                    if int(c["w1"]) == hw1 and int(c["w2"]) == hw2:
                        hit_pair_str = c.get("pair") or f"{hw1}-{hw2}"
                        break
            except ValueError:
                hit_pair_str = None
        actual_is_hit = hit_pair_str is not None

        bought_tickets = []
        hit_ticket = None
        for c in picked_list:
            p_str = c.get("pair") or f"{c['w1']}-{c['w2']}"
            amt = alloc_map.get(p_str, 100)
            is_h = bool(actual_is_hit and p_str == hit_pair_str)
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
        eff_synth = calc_synth([float(c["odds"]) for c in picked_list]) or float(ev.get("synth_odds") or 0.0)
        balance -= bet
        total_bet += bet
        set_bet += bet

        if not win_str:
            payout, status = 0, "PENDING"
        elif actual_is_hit:
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
            flat_alloc = dict(allocate_bets(picked_list, 1000, tilt_strength=alloc_tilt, cover_ratio=0.62))
            f_bet = sum(flat_alloc.values()) or 1000
            prog_stats[bkt]["flat_bet"] += f_bet
            if actual_is_hit:
                if hit_ticket and hit_ticket["odds"] > 0 and hit_ticket["pair"] in flat_alloc:
                    f_pay = int((flat_alloc[hit_ticket["pair"]] * hit_ticket["odds"]) // 10) * 10
                else:
                    f_pay = int(round(f_bet * eff_synth / 10.0) * 10)
                prog_stats[bkt]["flat_payout"] += f_pay
        if status in ("HIT", "HIT_STEP_BACK", "HIT_STEP_UP", "HIT_KEEP"):
            prog_stats[bkt]["hits"] += 1

        logs.append({
            "timestamp": r["timestamp"], "race_title": title, "bet_type": ev["bet_type"],
            "stage_name": st_name, "mode_name": bkt, "surface_cond": surf_cond,
            "combos_summary": ev["combos_summary"], "synth_odds": eff_synth,
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

    lines = ["# 🟢 v13.13 ローデータ キャッシュ済み一覧", "", "| 状態 | 対象 | 区分 | 日数 | レース数 |", "| :--- | :--- | :--- | ---: | ---: |"]
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
        "version": "v13.13",
        "updated_at": "",
        "balance": INITIAL_BANKROLL,
        "withdrawn_total": 0,
        "set_loss": 0,
        "stage_idx": 0,
        "daily_stats": {"total_bet": 0, "total_payout": 0},
        "race_logs": [],
    }


def save_prod_state(state: Dict) -> None:
    state["version"] = "v13.13"
    state["updated_at"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    json.dump(state, open(PROD_STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
