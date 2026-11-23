#!/usr/bin/env python3
"""
engine/environment.py (Oddmaker v15.1 Clean Adaptive Architecture)
Layer 2: 完全自律・クロスデイ環境オンライン適応＆馬の状態・ローテ層（騎手評価なし）
  - 手動介入・直前オッズ変動は完全排除（過去シミュレーションとの100%整合を保証）
  - クロスデイ LiveTrackTracker: 同日＋同開催前日(減衰0.45)＋芝内/外サブキー分離＋JSON永続化
  - 芝320件(経年高速化補正付)＋ダート112件の基準タイム表
  - 馬体重増減・馬格・斤量変化・芝⇔ダート替わり・休養週数・昇級初戦・前走ペース（騎手評価は完全除外）
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from engine.master_data import (
    BASE_BODY_WEIGHT_KG,
    BENCHMARK_ERA_OFFSET_SEC,
    CLASS_BENCHMARK_TIMES,
    COURSE_GEOMETRY,
    COURSE_LEG_FIT,
    DIRT_CLASS_BENCHMARK_TIMES,
    FRONT7_RATIO_TH,
    LIVE_TRACKER_DAY_DECAY,
    LIVE_TRACKER_DEADZONE,
    LIVE_TRACKER_MIN_EFF_N,
    LIVE_TRACKER_PRIOR_K,
    LIVE_TRACKER_STATE_PATH,
    LIVE_TURF_WAKU_GAIN,
    SUMMER_COOL_VENUES,
    SUMMER_HEAT_VENUES,
    LATE_SUMMER_MONTHS,
    SUMMER_MONTHS,
    WINTER_MONTHS,
    V14_TRACK_CHAR_STRENGTH,
    VENUE_STYLE_TRAITS,
)


def infer_inner_outer(venue: str, surface: str, distance: int, race_class: str = "") -> str:
    if "ダ" in (surface or "") or "障" in (surface or ""):
        return ""
    if "外" in (race_class or ""):
        return "外"
    if "内" in (race_class or ""):
        return "内"
    d = int(distance or 0)
    if "新潟" in (venue or ""):
        return "外" if d in (1600, 1800) else "内"
    if "阪神" in (venue or ""):
        return "外" if d in (1600, 1800, 2400, 2600) else "内"
    if "京都" in (venue or ""):
        return "外" if d in (1600, 1800, 2200, 2400, 3000, 3200) else "内"
    return ""


def normalize_benchmark_class(race_name: str = "", race_class: str = "") -> str:
    txt = f"{race_name} {race_class}"
    if re.search(r"(?:G1|G2|G3|GⅠ|GⅡ|GⅢ|GI|GII|GIII|重賞)(?![IⅠ0-9])", txt):
        return "重賞クラス"
    if ("2歳" in txt or "２歳" in txt) and ("オープン" in txt or "OP" in txt or "特別" in txt or "賞" in txt):
        return "2歳オープン"
    if "オープン" in txt or "OPEN" in txt.upper() or "OP" in txt:
        return "古馬オープン"
    if "3勝" in txt or "３勝" in txt:
        return "3勝クラス"
    if "2勝" in txt or "２勝" in txt:
        return "2勝クラス"
    if "1勝" in txt or "１勝" in txt or "500万" in txt:
        return "1勝クラス"
    if "未勝利" in txt or "新馬" in txt:
        return "3歳未勝利"
    return "1勝クラス"


def _class_rank(txt: str) -> int:
    if not txt:
        return 0
    if re.search(r"(?:G1|G2|G3|GⅠ|GⅡ|GⅢ|重賞)", txt):
        return 6
    if "オープン" in txt or "OPEN" in txt.upper() or "OP" in txt:
        return 5
    if "3勝" in txt or "３勝" in txt:
        return 4
    if "2勝" in txt or "２勝" in txt:
        return 3
    if "1勝" in txt or "１勝" in txt or "500万" in txt:
        return 2
    if "未勝利" in txt or "新馬" in txt:
        return 1
    return 0


def get_class_benchmark_time(
    venue: str,
    distance: int,
    race_name: str = "",
    race_class: str = "",
    course_dir: Optional[str] = None,
    surface: str = "芝",
    apply_era_offset: bool = True,
) -> Optional[float]:
    v_clean = (venue or "").replace("競馬場", "").strip()
    d = int(distance or 0)
    if not v_clean or d <= 0:
        return None
    cls = normalize_benchmark_class(race_name, race_class)
    if "ダ" in (surface or ""):
        val = DIRT_CLASS_BENCHMARK_TIMES.get((v_clean, d, cls))
        if val is not None:
            return val
        for fcls in ("1勝クラス", "2勝クラス", "3勝クラス", "3歳未勝利", "古馬オープン", "重賞クラス"):
            val = DIRT_CLASS_BENCHMARK_TIMES.get((v_clean, d, fcls))
            if val is not None:
                return val
        return None

    io = course_dir if course_dir is not None else infer_inner_outer(v_clean, "芝", d, race_class)
    era_adj = (BENCHMARK_ERA_OFFSET_SEC * (float(d) / 1600.0)) if apply_era_offset else 0.0
    for try_io in (io, "", "外", "内"):
        val = CLASS_BENCHMARK_TIMES.get((v_clean, d, try_io, cls))
        if val is not None:
            return round(val + era_adj, 2)
    for try_io in (io, "", "外", "内"):
        for fcls in ("1勝クラス", "2勝クラス", "3勝クラス", "古馬オープン", "3歳未勝利", "重賞クラス", "2歳オープン"):
            val = CLASS_BENCHMARK_TIMES.get((v_clean, d, try_io, fcls))
            if val is not None:
                return round(val + era_adj, 2)
    return None


def calc_front7_pos_ratio(h: Dict) -> Tuple[Optional[float], Optional[float]]:
    c1 = h.get("prev_c1")
    c2 = h.get("prev_c2")
    c4 = h.get("prev_c4")
    pn = h.get("prev_field_size")
    if not c1 or not pn or int(pn) <= 0:
        return None, None
    n = max(int(pn), int(c1), 6)
    if c2 and int(c2) > 0:
        c12 = 0.5 * (float(c1) + float(c2))
    elif c4 and int(c4) > 0:
        c12 = 0.78 * float(c1) + 0.22 * float(c4)
    else:
        c12 = float(c1)
    r4 = (float(c4) / float(n)) if (c4 and int(c4) > 0) else None
    return c12 / float(n), r4


def classify_leg_type_6(h: Dict) -> str:
    c1l = [int(x) for x in (h.get("prev_c1_list") or []) if x and int(x) > 0]
    c1 = h.get("prev_c1")
    c4 = h.get("prev_c4")
    pn = int(h.get("prev_field_size") or 14)
    if not c1 or pn <= 0:
        return str(h.get("leg_type") or "不明")
    n = max(pn, int(c1), 6)
    r1 = float(c1) / float(n)
    r4 = (float(c4) / float(n)) if (c4 and int(c4) > 0) else r1
    if r1 >= 0.50 and (r1 - r4) >= 0.25 and r4 <= 0.36:
        return "マクリ"
    if len(c1l) >= 2:
        rs = [float(x) / float(max(n, x)) for x in c1l]
        if (max(rs) - min(rs)) >= 0.34 and min(rs) <= 0.32 and max(rs) >= 0.52:
            return "自在"
    return str(h.get("leg_type") or "不明")


def estimate_day_track_speed_bias(completed_races: List[Dict], venue: str, surface: str = "芝") -> float:
    want_dirt = "ダ" in (surface or "")
    diffs: List[Tuple[float, float]] = []
    for r in completed_races or []:
        if (venue or "") not in str(r.get("venue") or ""):
            continue
        r_surf = str(r.get("surface") or "")
        if want_dirt != ("ダ" in r_surf):
            continue
        win_sec = float(r.get("win_time_sec") or 0.0)
        dist = int(r.get("distance") or 0)
        if win_sec <= 40.0 or dist <= 0:
            continue
        bench = get_class_benchmark_time(
            str(r.get("venue") or ""), dist,
            str(r.get("race_name") or ""), str(r.get("race_class") or ""),
            surface=r_surf,
        )
        if bench is None:
            continue
        norm_diff = (win_sec - bench) * (1600.0 / float(dist))
        weight = 2.0 if dist <= 1400 else (1.5 if dist <= 1600 else 0.7)
        diffs.append((norm_diff, weight))
    if not diffs:
        return 0.0
    tot_w = sum(w for _, w in diffs)
    return round(sum(d * w for d, w in diffs) / tot_w, 2) if tot_w > 0 else 0.0


def get_track_speed_level(
    venue: str,
    surface: str,
    month: int = 5,
    track_condition: str = "良",
    cushion_value: Optional[float] = None,
    day_speed_bias_sec: float = 0.0,
) -> int:
    is_dirt = "ダ" in (surface or "")
    cond = track_condition or "良"
    v = venue or ""
    if is_dirt:
        if any(x in v for x in ("東京", "京都")):
            lvl = 1
        elif any(x in v for x in ("中京", "中山")):
            lvl = -2
        else:
            lvl = -1
        if cond in ("稍重", "稍"):
            lvl += 1
        elif cond in ("重", "不良"):
            lvl += 2
        if day_speed_bias_sec <= -0.8:
            lvl += 1
        elif day_speed_bias_sec >= 0.8:
            lvl -= 1
        return max(-2, min(2, lvl))

    if any(x in v for x in ("函館", "札幌")):
        lvl = -2
    elif "新潟" in v:
        lvl = 2 if (7 <= month <= 9) else 1
    elif "東京" in v:
        lvl = 1
    elif "京都" in v:
        lvl = -1 if month in (1, 2, 12) else 1
    elif "小倉" in v:
        lvl = 2 if (7 <= month <= 9) else -1
    elif any(x in v for x in ("中山", "阪神", "中京")):
        lvl = 2 if month in (9, 10) else -1
    elif "福島" in v:
        lvl = -1
    else:
        lvl = 0

    if cond in ("稍重", "稍"):
        lvl -= 1
    elif cond in ("重", "不良"):
        lvl -= 2
    if cushion_value is not None:
        if cushion_value >= 10.5:
            lvl += 1
        elif cushion_value <= 7.5:
            lvl -= 1
    if day_speed_bias_sec <= -0.7:
        lvl += 1
    elif day_speed_bias_sec >= 0.7:
        lvl -= 1
    return max(-2, min(2, lvl))


class LiveTrackTracker:
    """
    v15.1 クロスデイ・ベイズ適応トラッカー（内/外サブキー分離 ＋ 土曜→日曜引き継ぎ ＋ JSON永続化）
    """

    def __init__(
        self,
        prior_k: float = LIVE_TRACKER_PRIOR_K,
        min_eff_n: float = LIVE_TRACKER_MIN_EFF_N,
        deadzone: float = LIVE_TRACKER_DEADZONE,
        day_decay: float = LIVE_TRACKER_DAY_DECAY,
    ):
        self.prior_k = float(prior_k)
        self.min_eff_n = float(min_eff_n)
        self.deadzone = float(deadzone)
        self.day_decay = float(day_decay)
        self._history: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._observed_ids: Set[str] = set()

    @staticmethod
    def _keys(race: Dict) -> Tuple[str, str, str]:
        v = str(race.get("venue") or "")
        s = "ダート" if "ダ" in str(race.get("surface") or "") else "芝"
        io = infer_inner_outer(v, s, int(race.get("distance") or 0), str(race.get("race_class") or ""))
        dk = str(race.get("date_key") or "")
        return dk, f"{v}|{s}|{io}", f"{v}|{s}|"

    @staticmethod
    def _day_gap(dk1: str, dk2: str) -> int:
        if len(dk1) != 8 or len(dk2) != 8 or not dk1.isdigit() or not dk2.isdigit():
            return 99
        try:
            d1 = date(int(dk1[:4]), int(dk1[4:6]), int(dk1[6:8]))
            d2 = date(int(dk2[:4]), int(dk2[4:6]), int(dk2[6:8]))
            return (d2 - d1).days
        except ValueError:
            return 99

    def get_live_bias(self, race: Dict) -> Dict[str, Any]:
        dk, sub_k, base_k = self._keys(race)
        eff_n = obs_in = exp_in = obs_out = exp_out = obs_fr = exp_fr = obs_bk = exp_bk = 0.0
        td_num = td_den = 0.0
        pairs = ((sub_k, 0.70), (base_k, 0.30)) if sub_k != base_k else ((base_k, 1.0),)

        for bk, bw in pairs:
            for p_dk, st in self._history.get(bk, {}).items():
                if dk and p_dk > dk:
                    continue
                gap = self._day_gap(p_dk, dk) if dk else 0
                if gap == 0:
                    w = 1.0 * bw
                elif 1 <= gap <= 2 and self.day_decay > 0.0:
                    w = self.day_decay * bw
                else:
                    continue
                eff_n += float(st.get("n", 0)) * w
                obs_in += float(st.get("obs_in", 0.0)) * w
                exp_in += float(st.get("exp_in", 0.0)) * w
                obs_out += float(st.get("obs_out", 0.0)) * w
                exp_out += float(st.get("exp_out", 0.0)) * w
                obs_fr += float(st.get("obs_fr", 0.0)) * w
                exp_fr += float(st.get("exp_fr", 0.0)) * w
                obs_bk += float(st.get("obs_bk", 0.0)) * w
                exp_bk += float(st.get("exp_bk", 0.0)) * w
                for d_val, d_wt in (st.get("time_diffs") or []):
                    td_num += float(d_val) * float(d_wt) * w
                    td_den += float(d_wt) * w

        spd = (td_num / td_den) if td_den > 0 else 0.0
        if eff_n < self.min_eff_n:
            return {"n": round(eff_n, 1), "waku_tilt": 0.0, "leg_tilt": 0.0, "speed_bias_sec": round(spd, 2), "active": False}

        k = self.prior_k
        wt = (obs_out + k) / (exp_out + k) - (obs_in + k) / (exp_in + k)
        if abs(wt) < self.deadzone:
            wt = 0.0
        lt = (obs_fr + k) / (exp_fr + k) - (obs_bk + k) / (exp_bk + k)
        if abs(lt) < self.deadzone:
            lt = 0.0
        return {
            "n": round(eff_n, 1),
            "waku_tilt": round(max(-0.20, min(0.20, wt)), 4),
            "leg_tilt": round(max(-0.20, min(0.20, lt)), 4),
            "speed_bias_sec": round(spd, 2),
            "active": True,
        }

    def observe(self, race: Dict) -> None:
        rid = str(race.get("race_id") or "")
        if rid and rid in self._observed_ids:
            return
        ro = race.get("real_odds") or []
        wf = str(race.get("wf_pair") or "")
        rname = str(race.get("race_name") or "") + " " + str(race.get("race_class") or "")
        if len(ro) < 2 or "-" not in wf or "障害" in rname:
            return
        try:
            wf_set = {int(x) for x in wf.split("-")}
        except ValueError:
            return
        tot_inv = sum(1.0 / float(c["odds"]) for c in ro if float(c.get("odds") or 0) > 0)
        if tot_inv <= 0:
            return

        by_w: Dict[int, List[Dict]] = {}
        for h in (race.get("horses_info") or []):
            if float(h.get("win_odds") or 0) >= 1.0 and 1 <= int(h.get("waku") or 0) <= 8:
                by_w.setdefault(int(h["waku"]), []).append(h)

        dk, sub_k, base_k = self._keys(race)
        win_sec = float(race.get("win_time_sec") or 0.0)
        dist = int(race.get("distance") or 0)
        td_entry = None
        if win_sec > 40.0 and dist > 0:
            bench = get_class_benchmark_time(
                str(race.get("venue") or ""), dist,
                str(race.get("race_name") or ""), str(race.get("race_class") or ""),
                surface=str(race.get("surface") or "芝"),
            )
            if bench is not None:
                norm_diff = (win_sec - bench) * (1600.0 / float(dist))
                wt_t = 2.0 if dist <= 1400 else (1.5 if dist <= 1600 else 0.7)
                td_entry = [round(norm_diff, 3), wt_t]

        for bk in {sub_k, base_k}:
            st = self._history.setdefault(bk, {}).setdefault(dk, {
                "n": 0, "obs_in": 0.0, "exp_in": 0.0, "obs_out": 0.0, "exp_out": 0.0,
                "obs_fr": 0.0, "exp_fr": 0.0, "obs_bk": 0.0, "exp_bk": 0.0, "time_diffs": [],
            })
            st["n"] += 1
            for w, hs in by_w.items():
                p_mkt = sum(
                    1.0 / float(c["odds"]) for c in ro
                    if float(c.get("odds") or 0) > 0 and (int(c["w1"]) == w or int(c["w2"]) == w)
                ) / tot_inv
                hit = 1.0 if w in wf_set else 0.0
                if w <= 3:
                    st["obs_in"] += hit; st["exp_in"] += p_mkt
                elif w >= 6:
                    st["obs_out"] += hit; st["exp_out"] += p_mkt
                top_h = min(hs, key=lambda x: float(x["win_odds"]))
                r12, _ = calc_front7_pos_ratio(top_h)
                if r12 is not None:
                    if r12 <= FRONT7_RATIO_TH:
                        st["obs_fr"] += hit; st["exp_fr"] += p_mkt
                    else:
                        st["obs_bk"] += hit; st["exp_bk"] += p_mkt
            if td_entry is not None:
                st["time_diffs"].append(td_entry)
        if rid:
            self._observed_ids.add(rid)

    def prune_old_days(self, current_ymd: str, keep_days: int = 3) -> None:
        if len(current_ymd) != 8 or not current_ymd.isdigit():
            return
        for bk in list(self._history.keys()):
            dmap = self._history[bk]
            for dk in list(dmap.keys()):
                if self._day_gap(dk, current_ymd) > keep_days:
                    del dmap[dk]
            if not dmap:
                del self._history[bk]

    def to_dict(self) -> Dict[str, Any]:
        return {"history": self._history, "observed_ids": sorted(self._observed_ids)[-500:]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LiveTrackTracker":
        tr = cls()
        if isinstance(data, dict):
            if isinstance(data.get("history"), dict):
                tr._history = data["history"]
            if isinstance(data.get("observed_ids"), list):
                tr._observed_ids = {str(x) for x in data["observed_ids"]}
        return tr


def load_live_tracker(path: Optional[Path] = None, current_ymd: str = "") -> LiveTrackTracker:
    p = Path(path) if path is not None else LIVE_TRACKER_STATE_PATH
    if not p.exists():
        return LiveTrackTracker()
    try:
        tr = LiveTrackTracker.from_dict(json.loads(p.read_text(encoding="utf-8")))
        if current_ymd:
            tr.prune_old_days(current_ymd, keep_days=3)
        return tr
    except Exception:
        return LiveTrackTracker()


def save_live_tracker(tracker: LiveTrackTracker, path: Optional[Path] = None, current_ymd: str = "") -> None:
    p = Path(path) if path is not None else LIVE_TRACKER_STATE_PATH
    if current_ymd:
        tracker.prune_old_days(current_ymd, keep_days=3)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tracker.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class RaceEnvironment:
    venue: str
    surface: str
    is_dirt: bool
    distance: int
    track_condition: str
    month: int
    day_num: int
    course_dir: str
    straight_m: float
    slope: str
    toughness: str
    course_type: str
    speed_level: int
    pace_mode: str
    cushion_value: Optional[float]
    live_n: float
    live_waku_tilt: float
    live_leg_tilt: float
    live_speed_bias_sec: float
    explain: List[str] = field(default_factory=list)


def build_race_environment(
    race: Dict,
    horses: List[Dict],
    live_tracker: Optional[LiveTrackTracker] = None,
) -> RaceEnvironment:
    venue = str(race.get("venue") or "")
    surface = str(race.get("surface") or "ダート")
    is_dirt = ("ダ" in surface)
    dist = int(race.get("distance") or 0)
    cond = str(race.get("track_condition") or "良")
    rclass = str(race.get("race_class") or "")
    dkey = str(race.get("date_key") or "")
    month = int(dkey[4:6]) if len(dkey) >= 6 and dkey[4:6].isdigit() else 5
    m_day = re.search(r"(\d+)日目", rclass)
    day_num = int(m_day.group(1)) if m_day else 0

    live = (
        live_tracker.get_live_bias(race)
        if live_tracker is not None
        else {"n": 0.0, "waku_tilt": 0.0, "leg_tilt": 0.0, "speed_bias_sec": 0.0, "active": False}
    )

    io = infer_inner_outer(venue, surface, dist, rclass)
    geo = COURSE_GEOMETRY.get((venue, io)) or COURSE_GEOMETRY.get(
        (venue, ""), {"straight": 350.0, "slope": "平坦", "toughness": "中間", "type": "両用"}
    )

    cushion = race.get("cushion_value")
    if cushion is not None:
        try:
            cushion = float(cushion)
        except (TypeError, ValueError):
            cushion = None

    day_spd_sec = float(live.get("speed_bias_sec") or race.get("day_speed_bias_sec") or 0.0)
    speed_level = get_track_speed_level(venue, surface, month, cond, cushion, day_spd_sec)

    scratched = {int(x) for x in (race.get("scratched_umabans") or []) if str(x).isdigit()}
    active_horses = [h for h in horses if int(h.get("umaban") or 0) not in scratched]
    nige_cnt = sum(1 for h in active_horses if h.get("leg_type") == "逃げ")
    kp = [calc_front7_pos_ratio(h)[0] for h in active_horses]
    kp = [x for x in kp if x is not None]
    fr = (sum(1 for x in kp if x <= FRONT7_RATIO_TH) / float(len(kp))) if kp else 0.38

    if nige_cnt >= 4 or (nige_cnt >= 3 and fr >= 0.55):
        pace_mode = "H"
    elif nige_cnt <= 1 and fr <= 0.30:
        pace_mode = "S"
    else:
        pace_mode = "M"

    explain: List[str] = [
        f"ペース:{pace_mode}(逃{nige_cnt}/前{int(fr*100)}%)",
        f"馬場Lv:{speed_level:+d}({month}月/{cond})",
    ]
    if live.get("active") and abs(live.get("waku_tilt", 0.0)) > 0:
        explain.append(f"実測枠({live['n']}R):{live['waku_tilt']:+.2f}")
    if cushion is not None:
        explain.append(f"クッション:{cushion:.1f}")

    return RaceEnvironment(
        venue=venue, surface=surface, is_dirt=is_dirt, distance=dist,
        track_condition=cond, month=month, day_num=day_num, course_dir=io,
        straight_m=float(geo["straight"]), slope=str(geo["slope"]),
        toughness=str(geo["toughness"]), course_type=str(geo["type"]),
        speed_level=speed_level, pace_mode=pace_mode, cushion_value=cushion,
        live_n=float(live.get("n") or 0.0),
        live_waku_tilt=float(live.get("waku_tilt") or 0.0),
        live_leg_tilt=float(live.get("leg_tilt") or 0.0),
        live_speed_bias_sec=day_spd_sec,
        explain=explain,
    )


def get_expected_body_weight(sex: str, age: Optional[int], is_dirt: bool) -> float:
    """性別(牡/セ vs 牝) × 年齢(2/3/4+) × 芝/ダートの基準馬体重(kg)を返す"""
    sg = "F" if sex == "牝" else "M"
    ag = 2 if age == 2 else (3 if age == 3 else 4)
    return float(BASE_BODY_WEIGHT_KG.get((sg, ag, bool(is_dirt)), 475.0))


def evaluate_horse_condition_mult(
    h: Dict,
    race_name: str = "",
    cur_surface: str = "",
    cur_class: str = "",
    venue: str = "",
    month: int = 0,
    distance: int = 0,
) -> float:
    """
    v15.1 性別正規化・馬体重 × 季節 × レース間隔 状態評価モデル (0.80〜1.14)
    ※ 騎手評価・種牡馬評価・手動介入・直前オッズ変動は一切不使用。過去キャッシュ未取得項目は完全中立(1.0)。

      1) 性別×年齢×芝ダ正規化馬格（牡馬・牝馬の絶対体重偏りを完全排除）:
           - 個体馬体重 bw と基準馬体重 base_bw の相対偏差 rel_bw = bw - base_bw で評価
           - 牝馬(平均454〜474kg)も牡馬(平均478〜498kg)も「標準馬格なら完全に同一評価(1.00)」
           - 夏(7〜8月)×暑熱4場(中京・新潟・小倉・福島)のみ放熱負荷(絶対>=520kg or 相対>=+30kg)を割引、軽量馬を加点
           - 芝2000m以上の長距離では超大型(相対>=+45kg)をスタミナ面で微割引
      2) 性別×季節（混合戦のみ・穏やかな補正で多重カウント防止）:
           - 7〜8月混合戦: 牝馬 +1.5%（暑熱4場の大型牡馬のみ -1.0%）
           - 9月残暑混合戦: 牝馬 +1.0% / 1〜3月冬春混合戦: 牝馬 -1.0%
      3) 馬体重増減(%) × レース間隔クロス:
           - 新馬戦は初出走(diff=0)のため増減評価をスキップ
           - 絶対kgだけでなく体重比(diff_pct = d_kg / bw * 100)を併用
           - 「休んだのに減(6〜12週 × <=-4kg)」「使い詰め増(5週以内 × +4〜+8kg)」を軸に評価
      4) 障害戦ガード: 障害戦では平地馬格・夏反転をスキップし極端な増減(±14kg以上)のみ軽微に評価
      5) サブブロック安全クリップ: 馬体・性別・間隔の合成倍率を [0.86, 1.10] に収め過剰な偏りを防止
    """
    is_dirt = "ダ" in (cur_surface or "")
    rtxt = f"{race_name} {cur_class}"
    is_obstacle = ("障" in (cur_surface or "")) or ("障害" in rtxt)
    is_shinba = ("新馬" in rtxt)
    is_mare_only = ("牝" in rtxt)
    v_str = str(venue or "")
    m_int = int(month or 0)
    dist_int = int(distance or 0)

    is_summer = m_int in SUMMER_MONTHS
    is_late_summer = m_int in LATE_SUMMER_MONTHS
    is_winter = m_int in WINTER_MONTHS
    is_summer_heat = is_summer and any(hv in v_str for hv in SUMMER_HEAT_VENUES)

    # 年齢判定（馬個別の age を優先、無ければレース名から推定）
    age_val = h.get("age")
    age_int: Optional[int] = None
    if age_val is not None:
        try:
            age_int = int(age_val)
        except (TypeError, ValueError):
            age_int = None
    if age_int is None:
        if ("2歳" in rtxt) or ("２歳" in rtxt):
            age_int = 2
        elif (("3歳" in rtxt) or ("３歳" in rtxt)) and ("以上" not in rtxt):
            age_int = 3
    is_young = (age_int is not None and age_int <= 3)

    sex = str(h.get("sex") or "").strip()

    bw: Optional[int] = None
    if h.get("body_weight") is not None:
        try:
            bv = int(h["body_weight"])
            if 340 <= bv <= 640:
                bw = bv
        except (TypeError, ValueError):
            bw = None

    rw: Optional[int] = None
    if h.get("rest_weeks") is not None:
        try:
            rw = int(h["rest_weeks"])
        except (TypeError, ValueError):
            rw = None

    # 障害戦ガード: 平地馬格・夏反転はスキップし、極端な馬体重増減のみ軽微に評価
    if is_obstacle:
        obs_m = 1.0
        bw_diff = h.get("body_weight_diff")
        if bw_diff is not None:
            try:
                dk = int(bw_diff)
                if dk <= -14 or dk >= 18:
                    obs_m *= 0.96
            except (TypeError, ValueError):
                pass
        return round(obs_m, 4)

    # 性別×年齢×芝ダ正規化による相対馬格 (rel_bw)
    base_bw = get_expected_body_weight(sex, age_int, is_dirt)
    rel_bw: Optional[float] = (float(bw) - base_bw) if bw is not None else None
    is_small_frame = (rel_bw is not None) and (rel_bw <= -30.0)
    is_tight_5w = (rw is not None and rw <= 5)
    is_mid_layoff = (rw is not None and 6 <= rw <= 12)
    is_layoff_return = (rw is not None and rw >= 4)

    body_sex_mult = 1.0

    # 1) 性別正規化・馬格評価 × 季節・暑熱会場・距離
    if bw is not None and rel_bw is not None:
        if is_summer_heat:
            # 7〜8月暑熱4場: 絶対520kg以上(放熱限界)または相対+30kg超を割引、相対-18kg以下を加点
            if bw >= 520 or rel_bw >= 30.0:
                body_sex_mult *= 0.960
            elif rel_bw <= -18.0:
                body_sex_mult *= 1.025
        else:
            # 通常期(9〜6月)および夏の札幌・函館: 相対馬格で評価（牡牝バイアスなし）
            if is_dirt:
                if 10.0 <= rel_bw <= 45.0:
                    body_sex_mult *= 1.015
                elif rel_bw > 45.0:
                    body_sex_mult *= 1.005
                elif rel_bw <= -45.0:
                    body_sex_mult *= 0.965
                elif rel_bw <= -25.0:
                    body_sex_mult *= 0.982
            else:
                if rel_bw <= -40.0:
                    body_sex_mult *= 0.980
                elif dist_int >= 2000 and rel_bw >= 45.0:
                    body_sex_mult *= 0.985  # 芝長距離の超大型はスタミナ面で微割引
                elif rel_bw >= 50.0:
                    body_sex_mult *= 0.990

    # 2) 性別 × 季節（混合戦のみ・穏やかな補正）
    if (not is_mare_only) and sex:
        if is_summer:
            if sex == "牝":
                body_sex_mult *= 1.015
            elif is_summer_heat and sex in ("牡", "セ", "騙") and (rel_bw is None or rel_bw >= 0.0):
                body_sex_mult *= 0.990
        elif is_late_summer and sex == "牝":
            body_sex_mult *= 1.010
        elif is_winter and sex == "牝":
            body_sex_mult *= 0.990

    # 3) 馬体重増減(kg & %) × レース間隔 × 年齢 クロス（新馬戦は初出走のためスキップ）
    bw_diff = h.get("body_weight_diff")
    if (not is_shinba) and (bw_diff is not None):
        try:
            d_kg = int(bw_diff)
            d_pct = (float(d_kg) / float(bw) * 100.0) if (bw is not None and bw > 0) else (float(d_kg) / base_bw * 100.0)

            if is_mid_layoff and (d_kg <= -4 or d_pct <= -0.9):
                # 最危険パターン「休んだのに減（6〜12週空け × -4kg以上）」
                if d_kg <= -10 or d_pct <= -2.1:
                    body_sex_mult *= 0.915 if is_summer else 0.950
                else:
                    body_sex_mult *= 0.945 if is_summer else 0.975
            elif is_tight_5w and (4 <= d_kg <= 8):
                # 最上位加点パターン「使い詰めなのに+4〜+8kg増」
                body_sex_mult *= 1.030 if is_summer else 1.022
            elif is_tight_5w and (-8 <= d_kg <= -4):
                # 使い詰め微減は通常馬格では中立、ダート小柄馬×中1〜2週のみ軽微警戒
                if is_dirt and is_small_frame and (rw is not None and rw <= 2):
                    body_sex_mult *= 0.972
            elif d_kg <= -14 or d_pct <= -3.0:
                body_sex_mult *= 0.915 if is_small_frame else 0.930
            elif d_kg <= -10 or d_pct <= -2.1:
                body_sex_mult *= 0.950 if is_small_frame else 0.965
            elif d_kg <= -4:
                if is_summer and (rw is not None and rw >= 13):
                    body_sex_mult *= 0.960
                elif is_small_frame:
                    body_sex_mult *= 0.980
                else:
                    body_sex_mult *= 0.992
            elif is_young:
                if is_layoff_return:
                    if 6 <= d_kg <= 18:
                        body_sex_mult *= 1.030 if not is_summer_heat else 1.012
                    elif 2 <= d_kg <= 5:
                        body_sex_mult *= 1.012
                    elif d_kg >= 22:
                        body_sex_mult *= 0.970
                else:
                    if 9 <= d_kg <= 12:
                        body_sex_mult *= 1.012 if not is_summer else 0.990
                    elif 13 <= d_kg <= 18:
                        body_sex_mult *= 1.005 if not is_summer else 0.975
                    elif d_kg >= 20:
                        body_sex_mult *= 0.960
                    elif -2 <= d_kg <= 3 and not is_small_frame:
                        body_sex_mult *= 1.005
            else:
                # 古馬(4歳以上): +10kg以上は季節問わず太め残り割引
                if d_kg >= 18 or d_pct >= 3.8:
                    body_sex_mult *= 0.935 if (rw is not None and rw >= 12) else 0.905
                elif d_kg >= 10 or d_pct >= 2.2:
                    body_sex_mult *= 0.968 if (rw is not None and rw >= 12) else 0.952
                elif 4 <= d_kg <= 8:
                    body_sex_mult *= 1.010
                elif -2 <= d_kg <= 3:
                    body_sex_mult *= 1.005
        except (TypeError, ValueError):
            pass

    # 馬体・性別・間隔ブロックの安全クリップ [0.86, 1.10]
    mult = max(0.86, min(1.10, body_sex_mult))

    # 4) 斤量変化
    jw_diff = h.get("jockey_weight_diff")
    if jw_diff is not None:
        try:
            jd = float(jw_diff)
            if jd <= -2.0:
                mult *= 1.025
            elif jd >= 2.0:
                mult *= 0.975
        except (TypeError, ValueError):
            pass

    # 5) ローテーション（芝⇔ダート替わり・長期休養/連闘・昇級初戦） ※騎手・種牡馬評価は完全不使用
    prev_surf = str(h.get("prev_surface") or "")
    if prev_surf and cur_surface:
        if ("ダ" in prev_surf) != ("ダ" in cur_surface) and (not is_shinba):
            mult *= 0.97

    if rw is not None:
        if rw >= 20:
            mult *= 0.95
        elif rw == 1:
            mult *= 0.97

    prev_cls = str(h.get("prev_class") or "")
    if prev_cls and (cur_class or race_name):
        pr, cr = _class_rank(prev_cls), _class_rank(f"{race_name} {cur_class}")
        if pr > 0 and cr > 0 and (cr - pr) >= 1:
            mult *= 0.975

    # 6) 前走ペース補正
    p_pace = str(h.get("prev_pace_tag") or "").upper()
    if p_pace in ("H", "S"):
        r12, _ = calc_front7_pos_ratio(h)
        if r12 is not None and r12 <= FRONT7_RATIO_TH:
            if p_pace == "H":
                mult *= 1.025
            elif p_pace == "S":
                mult *= 0.985

    return round(max(0.80, min(1.14, mult)), 4)


def classify_horse_character_bonus(
    h: Dict,
    surface: str,
    distance: int,
    spd_lvl: int,
    geo_type: str,
    geo_toughness: str,
) -> float:
    if "ダ" in (surface or ""):
        return 0.0
    delta = 0.0

    ag_list = [float(x) for x in (h.get("prev_agari_list") or []) if x and 30.0 <= float(x) <= 42.0]
    if not ag_list and h.get("prev_agari_3f"):
        v = float(h["prev_agari_3f"])
        if 30.0 <= v <= 42.0:
            ag_list = [v]

    char_tag = "不明"
    if ag_list:
        best_ag = min(ag_list)
        if best_ag <= 34.4:
            char_tag = "軽瞬発"
            if spd_lvl >= 1 or geo_type == "瞬発":
                delta += 0.030
            elif spd_lvl <= -2 or geo_toughness == "タフ":
                delta -= 0.020
        elif best_ag >= 34.8:
            char_tag = "重持続"
            if spd_lvl <= -1 or geo_toughness == "タフ":
                delta += 0.030
            elif spd_lvl >= 2 and geo_type == "瞬発":
                delta -= 0.025

    pt = float(h.get("prev_time_sec") or 0.0)
    pv = str(h.get("prev_venue") or "")
    pd = int(h.get("prev_dist") or 0)
    pf = int(h.get("prev_finish") or 0)
    pcls = str(h.get("prev_class") or "")
    psurf = str(h.get("prev_surface") or "芝")
    if pt > 40.0 and pv and pd > 0:
        bench = get_class_benchmark_time(pv, pd, pcls, pcls, surface=psurf)
        if bench is not None:
            tdiff = pt - bench
            good_fin = (1 <= pf <= 4)
            if tdiff <= -0.3 and good_fin:
                delta += 0.025 if spd_lvl >= 1 else 0.005
            elif tdiff >= 0.4 and good_fin:
                delta += 0.025 if spd_lvl <= -1 else -0.015

    if pd > 0 and distance > 0:
        dd = int(distance) - pd
        if dd >= 200 and char_tag == "軽瞬発":
            delta -= 0.015
        elif dd <= -200 and char_tag == "重持続":
            lt = str(h.get("leg_type") or "")
            if lt in ("逃げ", "先行"):
                delta += 0.015
            elif lt == "追込":
                delta -= 0.020
    return delta


def calc_environment_waku_adjustment(
    race: Dict,
    horses: List[Dict],
    env: RaceEnvironment,
    strength: float = V14_TRACK_CHAR_STRENGTH,
) -> Dict[str, float]:
    if not horses:
        return {str(w): 1.0 for w in range(1, 9)}

    rname = str(race.get("race_name") or "") + " " + str(race.get("race_class") or "")
    rclass = str(race.get("race_class") or "")
    is_upper = any(k in rname for k in ("2勝", "２勝", "3勝", "３勝", "オープン", "OP", "G1", "G2", "G3", "特別", "賞", "S"))

    by_w: Dict[int, List[Tuple[float, float]]] = {}
    for h in horses:
        w = int(h.get("waku") or 0)
        if not (1 <= w <= 8):
            continue
        st6 = classify_leg_type_6(h)
        wo = float(h.get("win_odds") or 0.0)
        sup = (1.0 / (max(1.2, wo) ** 0.85)) if wo >= 1.0 else 0.15
        delta = 0.0

        if st6 == "自在":
            delta += 0.035
        elif st6 == "マクリ":
            if env.toughness == "タフ" or env.distance >= 2000 or env.is_dirt:
                delta += 0.025
            elif env.course_type == "瞬発" and env.speed_level >= 1:
                delta -= 0.020

        if not env.is_dirt:
            if env.straight_m >= 400.0 and env.pace_mode == "S":
                if st6 in ("逃げ", "先行"):
                    delta += 0.025
                elif st6 == "追込":
                    delta -= 0.020
            elif env.straight_m <= 360.0 and env.pace_mode == "H":
                if st6 in ("差し", "自在", "マクリ"):
                    delta += 0.025
                elif st6 == "逃げ":
                    delta -= 0.025

            if env.speed_level >= 2:
                if st6 in ("先行", "自在"):
                    delta += 0.015
                elif st6 == "追込" and env.straight_m < 400.0:
                    delta -= 0.020
            elif env.speed_level <= -2:
                if st6 in ("逃げ", "先行", "自在"):
                    delta += 0.025
                elif st6 == "追込":
                    delta -= 0.025

            if env.day_num >= 5:
                if "京都" in env.venue:
                    if w <= 3: delta += 0.035
                    elif w >= 7: delta -= 0.020
                elif "中山" in env.venue:
                    if w <= 3: delta += 0.025
                    if st6 in ("逃げ", "先行"): delta += 0.025
                    elif st6 == "追込": delta -= 0.025
                elif "福島" in env.venue:
                    if w <= 3: delta += 0.030
                    if st6 in ("逃げ", "先行"): delta += 0.020
                elif "東京" in env.venue:
                    if w >= 6: delta += 0.025
                    elif w <= 2: delta -= 0.025
                elif "新潟" in env.venue and env.course_dir == "外":
                    if w >= 6: delta += 0.020
                elif "札幌" in env.venue or "函館" in env.venue:
                    if st6 in ("逃げ", "先行"): delta += 0.020
                    elif st6 == "追込": delta -= 0.020
                elif "中京" in env.venue:
                    if st6 in ("差し", "自在"): delta += 0.020
            elif 1 <= env.day_num <= 4:
                if "東京" in env.venue:
                    if w >= 6: delta += 0.020
                    elif w <= 2: delta -= 0.015
                elif "阪神" in env.venue and env.course_dir == "内":
                    if w <= 3 and st6 in ("逃げ", "先行"): delta += 0.025
                elif "福島" in env.venue:
                    if is_upper and st6 in ("差し", "自在"): delta += 0.020
                    elif (not is_upper) and st6 in ("逃げ", "先行"): delta += 0.020

            if env.cushion_value is not None:
                if env.cushion_value >= 10.0:
                    if st6 in ("逃げ", "先行"): delta += 0.025
                    elif st6 == "追込": delta -= 0.020
                elif env.cushion_value <= 7.5:
                    if st6 in ("差し", "自在"): delta += 0.025
                    elif st6 == "逃げ": delta -= 0.020

            if env.live_waku_tilt != 0.0:
                pos_norm = (w - 4.5) / 3.5
                delta += LIVE_TURF_WAKU_GAIN * env.live_waku_tilt * pos_norm

        delta += classify_horse_character_bonus(h, env.surface, env.distance, env.speed_level, env.course_type, env.toughness)
        cond_mult = evaluate_horse_condition_mult(h, race_name=rname, cur_surface=env.surface, cur_class=rclass, venue=env.venue, month=env.month, distance=env.distance)
        delta = max(-0.18, min(0.18, delta))
        combined = (1.0 + delta * strength) * cond_mult
        by_w.setdefault(w, []).append((combined, sup))

    raw_m: Dict[int, float] = {}
    for w in range(1, 9):
        items = by_w.get(w)
        if items:
            ts = sum(s for _, s in items)
            raw_m[w] = sum(v * s for v, s in items) / ts if ts > 0 else 1.0
        else:
            raw_m[w] = 1.0

    act = [w for w in range(1, 9) if w in by_w]
    ms = (sum(raw_m[w] for w in act) / float(len(act))) if act else 1.0
    return {str(w): (raw_m[w] / ms) for w in range(1, 9)}


def get_leg_weight(
    leg_type: str,
    surface: str = "ダート",
    track_condition: str = "良",
    venue: str = "",
    distance: int = 0,
    prev_c4: Optional[int] = None,
    prev_field_size: int = 14,
    prev_c1: Optional[int] = None,
    prev_c2: Optional[int] = None,
    pace_mode: str = "M",
    race_name: str = "",
) -> float:
    is_dirt = ("ダ" in (surface or "")) or ("ダート" in (surface or ""))
    surf_key = "ダート" if is_dirt else "芝"
    lt = str(leg_type or "不明")
    cond = track_condition or "良"
    dist = int(distance or 0)

    v_fit = None
    for (vk, sk), tbl in COURSE_LEG_FIT.items():
        if vk in (venue or "") and sk == surf_key:
            v_fit = tbl
            break
    if v_fit is None:
        v_fit = (
            {"逃げ": 1.09, "先行": 1.09, "差し": 0.96, "追込": 0.85, "不明": 0.98}
            if is_dirt
            else {"逃げ": 1.04, "先行": 1.08, "差し": 1.01, "追込": 0.90, "不明": 0.99}
        )
    w = float(v_fit.get(lt, 0.99))

    if not any(vk in (venue or "") for vk, _ in COURSE_LEG_FIT.keys()):
        for v_key, traits in VENUE_STYLE_TRAITS.items():
            if v_key in (venue or ""):
                w += traits.get(lt, 0.0)
                break

    r12, r4 = calc_front7_pos_ratio({
        "prev_c1": prev_c1, "prev_c2": prev_c2, "prev_c4": prev_c4, "prev_field_size": prev_field_size
    })
    if r12 is not None:
        if r12 <= (4.0 / 18.0):
            w += 0.065 if is_dirt else 0.055
        elif r12 <= FRONT7_RATIO_TH:
            w += 0.045 if is_dirt else 0.040
        elif r12 <= (10.0 / 18.0):
            w += 0.005 if (not is_dirt) else -0.005
        elif r12 <= (13.0 / 18.0):
            w -= 0.045 if is_dirt else 0.035
        else:
            w -= 0.075 if is_dirt else 0.055

        if r4 is not None:
            if r4 <= (5.0 / 18.0):
                w += 0.045
            elif (r4 - r12 >= 0.22) and (r12 <= FRONT7_RATIO_TH):
                w -= 0.075
            elif (r12 > FRONT7_RATIO_TH) and (r4 <= (6.0 / 18.0)):
                w += 0.035
    elif prev_c4 and int(prev_c4) > 0:
        n = max(int(prev_field_size or 14), int(prev_c4), 6)
        c4_ratio = float(prev_c4) / float(n)
        if c4_ratio <= (5.0 / 18.0):
            w += 0.05
        elif c4_ratio >= 0.75:
            w -= 0.05

    if dist > 0:
        if dist <= 1200:
            w += {"逃げ": 0.04, "先行": 0.03, "差し": -0.02, "追込": -0.04}.get(lt, 0.0)
            if ("新潟" in (venue or "")) and (not is_dirt) and dist == 1000:
                w += {"逃げ": 0.03, "先行": 0.03, "追込": -0.04}.get(lt, 0.0)
        elif dist <= 1400:
            w += {"逃げ": 0.02, "先行": 0.02, "差し": 0.00, "追込": -0.02}.get(lt, 0.0)
        elif dist >= 2000:
            w += {"逃げ": -0.03, "先行": 0.01, "差し": 0.03, "追込": 0.01}.get(lt, 0.0)

    if not is_dirt:
        if cond == "良":
            w += {"逃げ": 0.02, "先行": 0.02, "差し": 0.00, "追込": -0.01}.get(lt, 0.0)
        elif cond in ("重", "不良"):
            w += {"逃げ": -0.04, "先行": 0.01, "差し": 0.04, "追込": 0.02}.get(lt, 0.0)
    else:
        if cond in ("稍重", "稍"):
            w += {"逃げ": 0.03, "先行": 0.02, "差し": -0.01, "追込": -0.02}.get(lt, 0.0)
        elif cond in ("重", "不良"):
            w += {"逃げ": 0.06, "先行": 0.04, "差し": -0.03, "追込": -0.05}.get(lt, 0.0)

    if pace_mode == "H":
        w += {"逃げ": -0.06, "先行": -0.02, "差し": 0.05, "追込": 0.03}.get(lt, 0.0)
    elif pace_mode == "S":
        w += {"逃げ": 0.06, "先行": 0.04, "差し": -0.03, "追込": -0.05}.get(lt, 0.0)

    if ("未勝利" in race_name) or ("1勝" in race_name) or ("１勝" in race_name):
        if r12 is not None and r12 <= FRONT7_RATIO_TH:
            w += 0.015

    return round(max(0.76, min(1.28, w)), 3)
