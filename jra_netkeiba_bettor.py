import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page

from core_v12 import (
    calc_median_nearest,
    calc_waku_from_umaban,
    calc_waku_leg_scores,
    classify_leg_type,
    normalize_text,
    parse_hhmm_to_minutes,
)


@dataclass
class RaceMeta:
    race_id: str
    venue: str
    race_num: int
    race_name: str
    post_time: str
    has_scratch: bool = False
    horse_count: int = 0
    race_class: str = ""
    surface: str = "芝"
    distance: int = 0
    track_condition: str = "良"
    fav1_win_odds: float = 9.9
    fav2_win_odds: float = 0.0
    fav1_umaban: int = 0
    fav1_waku: int = 0
    fav1_leg_type: str = "不明"
    horses_info: List[Dict] = field(default_factory=list)
    waku_leg_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class BetOrder:
    meta: RaceMeta
    bet_type: str                     # "枠連" / "馬連" / "馬単"
    tickets: List[Tuple[str, int]]
    total_amount: int = field(init=False)

    def __post_init__(self):
        self.total_amount = sum(amt for _, amt in self.tickets)


def fetch_today_jra_race_ids(page: Page, ymd: str) -> List[str]:
    url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={ymd}"
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(500)
    return sorted(set(re.findall(r"race_id=(\d{12})", page.content())))


def fetch_netkeiba_odds_api(page: Page, race_id: str, type_num: str, ordered: bool) -> List[Dict]:
    """
    netkeiba 公式内部JSON API から直接オッズを取得
      type_num: "1"=単勝, "3"=枠連, "4"=馬連, "6"=馬単
    """
    api_url = (
        f"https://race.netkeiba.com/api/api_get_jra_odds.html"
        f"?pid=api_get_jra_odds&input=UTF-8&output=json"
        f"&race_id={race_id}&type={type_num}&action=init&sort=odds&compress=0"
    )
    try:
        res = page.request.get(
            api_url,
            headers={
                "Referer": f"https://race.netkeiba.com/odds/index.html?race_id={race_id}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            timeout=20000,
        )
        if not res.ok:
            return []
        payload = json.loads(res.text())
        odds_dict = ((payload.get("data") or {}).get("odds") or {}).get(type_num) or {}
        if not isinstance(odds_dict, dict):
            return []

        combos: List[Dict] = []
        for k, v in odds_dict.items():
            if not isinstance(v, list) or len(v) < 3:
                continue
            try:
                odds_val = float(str(v[0]).replace(",", ""))
                pop_val = int(str(v[2]))
            except (ValueError, TypeError):
                continue
            if odds_val < 1.0 or pop_val < 1:
                continue

            if type_num == "1" and re.fullmatch(r"\d{2}", str(k)):
                combos.append({"pop": pop_val, "horse": int(k), "odds": odds_val})
            elif re.fullmatch(r"\d{4}", str(k)):
                a, b = int(k[:2]), int(k[2:])
                if not ordered:
                    a, b = min(a, b), max(a, b)
                combos.append({"pop": pop_val, "pair": f"{a}-{b}", "w1": a, "w2": b, "odds": odds_val})

        combos.sort(key=lambda x: x["pop"])
        return combos
    except Exception:
        return []


def _normalize_cond(raw: str) -> str:
    if raw in ("稍", "稍重"):
        return "稍重"
    if raw in ("不", "不良"):
        return "不良"
    return raw


def extract_track_condition_from_text(text: str, surface: str = "") -> Optional[str]:
    """
    netkeibaのあらゆる馬場表記（'馬場:不' / '馬場:稍' / '芝:重' / 'ダ:不良' / 'Item04: 重' 等）に対応。
    1文字表記（稍・不）も確実に正規化する。
    """
    if not text:
        return None
    cleaned = re.sub(r"[\s\u3000]+", " ", text)
    is_dirt = ("ダ" in (surface or ""))
    # ① 対象コース種別が明記されている場合を最優先
    if is_dirt:
        m_sp = re.search(r"(?:ダート|ダ)\s*[:：\s]*[(（]?(不良|稍重|良|稍|重|不)(?:[)）\s/]|$)", cleaned)
        if m_sp:
            return _normalize_cond(m_sp.group(1))
    elif "芝" in (surface or ""):
        m_sp = re.search(r"芝\s*[:：\s]*[(（]?(不良|稍重|良|稍|重|不)(?:[)）\s/]|$)", cleaned)
        if m_sp:
            return _normalize_cond(m_sp.group(1))
    # ② '馬場:重' / '馬場:稍' / '馬場:不' / '馬場状態:不良'
    m = re.search(r"馬場(?:状態)?\s*[:：\s]*[(（]?(不良|稍重|良|稍|重|不)(?:[)）\s/]|$)", cleaned)
    if m:
        return _normalize_cond(m.group(1))
    # ③ '芝:重' / 'ダ:重' 汎用
    m2 = re.search(r"(?:芝|ダート|ダ)\s*[:：]\s*(不良|稍重|良|稍|重|不)(?:[\s/]|$)", cleaned)
    if m2:
        return _normalize_cond(m2.group(1))
    return None


def extract_track_condition_from_page(page: Page, surface: str = "") -> Optional[str]:
    """DOM要素(.Item04 / .RaceData01 / .RaceList_Item02 等)と本文の両方から馬場状態を抽出"""
    for sel in (".RaceData01 .Item04", ".RaceData01 span.Item04", ".RaceData01", ".RaceList_Item02"):
        loc = page.locator(sel).first
        if loc.count() > 0:
            txt = loc.inner_text().strip()
            cond = extract_track_condition_from_text(txt, surface)
            if cond:
                return cond
            # Item04 が '重' / '稍' / '不' / '良' のみの場合
            m_only = re.search(r"(不良|稍重|良|稍|重|不)", txt)
            if m_only and ("Item04" in sel):
                return _normalize_cond(m_only.group(1))
    return extract_track_condition_from_text(page.inner_text("body")[:2000], surface)


def _parse_past_c1_c4_from_cell(cell_text: str) -> Tuple[Optional[int], Optional[int], int]:
    """前走セル文字列から(1コーナー通過順, 4コーナー通過順, 前走出走頭数)を抽出（例: '16頭 ... 2-2-3-1' -> 2, 1, 16）"""
    m_head = re.search(r"(\d{1,2})頭", cell_text)
    prev_n = int(m_head.group(1)) if m_head else 14
    m_pass = re.search(r"\b(\d{1,2}(?:-\d{1,2}){1,3})\b", cell_text)
    if m_pass:
        parts = [int(x) for x in m_pass.group(1).split("-")]
        c1, c4 = parts[0], parts[-1]
        if 1 <= c1 <= 18 and 1 <= c4 <= 18:
            return c1, c4, prev_n
    return None, None, prev_n


def fetch_netkeiba_race_and_odds(page: Page, race_id: str, bet_type: str = "枠連") -> Tuple[RaceMeta, List[Dict]]:
    """
    netkeiba 馬柱(shutuba_past.html) + 単勝/枠連APIから以下を一括取得:
      - 場名, R, レース名, クラス(ハンデ/G3/2勝等), 発走時刻, 取消有無
      - 当日馬場: 芝/ダート(surface), 馬場状態(良/稍重/重/不良)
      - 全馬の枠番・馬番・前走1コーナー位置取り・脚質(逃げ/先行/差し/追込)
      - 出走頭数(horse_count), 単勝1人気オッズ・枠番・脚質
    """
    past_url = f"https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}"
    page.goto(past_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(450)
    body = page.inner_text("body")

    # 万一 shutuba_past が空の場合は通常 shutuba.html にフォールバック
    if page.locator("tr.HorseList").count() == 0:
        page.goto(f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}", wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(400)
        body = page.inner_text("body")

    race_name = page.locator(".RaceName").first.inner_text().strip() if page.locator(".RaceName").count() > 0 else ""
    r_data1 = page.locator(".RaceData01").first.inner_text().strip() if page.locator(".RaceData01").count() > 0 else ""
    r_data2 = page.locator(".RaceData02").first.inner_text().strip() if page.locator(".RaceData02").count() > 0 else ""
    header_info = f"{race_name} {r_data1} {r_data2}"

    m_time = re.search(r"(\d{1,2}:\d{2})\s*発走", header_info) or re.search(r"(\d{1,2}:\d{2})\s*発走", body)
    post_time = m_time.group(1) if m_time else ""

    # 芝 / ダート / 障害
    surface = "障害" if ("障" in r_data1 or "障害" in header_info) else ("ダート" if "ダ" in r_data1 else "芝")
    m_dist = re.search(r"[芝ダ障](\d{3,4})m", r_data1) or re.search(r"(\d{4})m", header_info)
    distance = int(m_dist.group(1)) if m_dist else 0
    # 馬場状態 (馬場:重 / 芝:稍重 / ダ:不良 等すべての表記に対応)
    track_cond = extract_track_condition_from_page(page, surface) or extract_track_condition_from_text(header_info, surface) or ""

    # レースクラス（G1/G2/G3/ハンデ/新馬/未勝利/1勝/2勝/3勝/オープン等）
    race_class = re.sub(r"\s+", " ", r_data2).strip()
    if page.locator(".Icon_GradeType1").count() > 0:
        race_class += " G1"
    elif page.locator(".Icon_GradeType2").count() > 0:
        race_class += " G2"
    elif page.locator(".Icon_GradeType3").count() > 0:
        race_class += " G3"

    venue_map = {
        "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
    }
    venue = venue_map.get(race_id[4:6], "")
    race_num = int(race_id[10:12])
    has_scratch = ("取消" in body) or ("除外" in body)

    # 全出走馬の枠番・馬番・前走1コーナー位置取りを取得
    # ※ shutuba_past.html では td.Waku5 のテキストが「馬番(例:8)」で、本当の枠番はCSSクラス(Waku5)にあるため
    #    必ず CSSクラス名(Waku1〜Waku8)を最優先で取得し、さらに max_umaban からの数学的算出で完全検証する。
    horses_info: List[Dict] = []
    horse_map: Dict[int, Dict] = {}
    rows = page.locator("tr.HorseList")
    for i in range(rows.count()):
        r = rows.nth(i)
        u_loc = r.locator("td[class*='Umaban'], td.Waku").first
        if u_loc.count() == 0:
            continue
        um = re.search(r"\d{1,2}", u_loc.inner_text())
        if not um:
            continue
        umaban = int(um.group(0))

        # ① 最優先：CSSクラス名 Waku1〜Waku8（枠色そのもの）から枠番を取得
        waku = 0
        for loc_cand in (r.locator("td[class*='Waku']").first, u_loc, r):
            if loc_cand.count() > 0:
                cls = loc_cand.get_attribute("class") or ""
                cm = re.search(r"Waku([1-8])\b", cls)
                if cm:
                    waku = int(cm.group(1))
                    break

        # 直近最大3走の過去走セルから1コーナー＆4コーナー通過順を収集し、中央値(nearest)で安定判定
        c1_list: List[int] = []
        c4_list: List[int] = []
        n_list: List[int] = []
        past_cells = r.locator("td.Past, td[class*='Past']")
        for p_idx in range(min(past_cells.count(), 3)):
            c1, c4, pn = _parse_past_c1_c4_from_cell(past_cells.nth(p_idx).inner_text())
            if c1 is not None:
                c1_list.append(c1)
                if c4 is not None:
                    c4_list.append(c4)
                n_list.append(pn)
        prev_c1 = calc_median_nearest(c1_list)
        prev_c4 = calc_median_nearest(c4_list)
        prev_n = calc_median_nearest(n_list) or 14
        leg_type = classify_leg_type(prev_c1, prev_n)
        info = {
            "umaban": umaban, "waku": waku, "prev_c1": prev_c1, "prev_c4": prev_c4,
            "prev_c1_list": c1_list, "prev_c4_list": c4_list, "prev_field_size": prev_n, "leg_type": leg_type,
        }
        horses_info.append(info)
        horse_map[umaban] = info

    tan_list = fetch_netkeiba_odds_api(page, race_id, "1", ordered=False)
    tan_by_horse = {int(t["horse"]): float(t["odds"]) for t in tan_list}
    for h in horses_info:
        h["win_odds"] = tan_by_horse.get(h["umaban"], 0.0)

    # JRA枠割りルールは「最大馬番(取消馬含む枠確定時の頭数)」と「馬番」から100%一意に決まる
    max_uma_candidates = [h["umaban"] for h in horses_info] + [int(t["horse"]) for t in tan_list]
    m_hc = re.search(r"(\d{1,2})頭", r_data2)
    if m_hc:
        max_uma_candidates.append(int(m_hc.group(1)))
    max_umaban = max(max_uma_candidates) if max_uma_candidates else 0
    horse_count = len(horses_info) or len(tan_list) or max_umaban

    # ② 数学的算出(calc_waku_from_umaban)で未取得・誤認を100%補正
    for h in horses_info:
        calc_w = calc_waku_from_umaban(h["umaban"], max_umaban) if max_umaban > 0 else 0
        if h["waku"] <= 0 and calc_w > 0:
            h["waku"] = calc_w
        elif calc_w > 0 and h["waku"] != calc_w:
            # CSSクラスと数学的枠番が食い違う場合も、JRA確定枠ルール(calc_w)を優先
            h["waku"] = calc_w

    fav1_odds = tan_list[0]["odds"] if tan_list else 9.9
    fav2_odds = tan_list[1]["odds"] if len(tan_list) >= 2 else 0.0
    fav1_horse = tan_list[0]["horse"] if tan_list else 0
    fav1_info = horse_map.get(fav1_horse, {})
    fav1_waku = calc_waku_from_umaban(fav1_horse, max_umaban) if (fav1_horse > 0 and max_umaban > 0) else int(fav1_info.get("waku") or 0)
    fav1_leg_type = str(fav1_info.get("leg_type") or "不明")

    track_cond = track_cond or "良"
    waku_leg_scores = calc_waku_leg_scores(horses_info, surface=surface, track_condition=track_cond, venue=venue, distance=distance)

    meta = RaceMeta(
        race_id=race_id, venue=venue, race_num=race_num, race_name=race_name,
        post_time=post_time, has_scratch=has_scratch, horse_count=horse_count,
        race_class=race_class, surface=surface, distance=distance, track_condition=track_cond,
        fav1_win_odds=fav1_odds, fav2_win_odds=fav2_odds, fav1_umaban=fav1_horse, fav1_waku=fav1_waku, fav1_leg_type=fav1_leg_type,
        horses_info=horses_info, waku_leg_scores=waku_leg_scores,
    )

    type_map = {"枠連": ("3", False), "馬連": ("4", False), "馬単": ("6", True)}
    type_num, ordered = type_map.get(bet_type, ("3", False))
    combos = fetch_netkeiba_odds_api(page, race_id, type_num, ordered)
    return meta, combos


def fetch_jra_result_info(page: Page, race_id: str, surface: str = "") -> Tuple[Optional[str], Optional[str]]:
    """result.html 1アクセスで (確定枠連組番, 確定馬場状態) を同時取得"""
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
    wf_pair: Optional[str] = None
    cond: Optional[str] = None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(400)

        cond = extract_track_condition_from_page(page, surface)

        # ① 払戻テーブルの枠連行から取得
        row = page.locator("tr.Wakuren td.Result, tr:has(th:text-is('枠連')) td.Result").first
        if row.count() > 0:
            nums = re.findall(r"[1-8]", row.inner_text())
            if len(nums) >= 2:
                a, b = sorted((int(nums[0]), int(nums[1])))
                wf_pair = f"{a}-{b}"

        # ② 着順テーブルの1着・2着の枠番から直接算出
        if not wf_pair:
            waku_cells = page.locator("#All_Result_Table tbody tr td.Num.Waku, table.RaceTable01 tbody tr td.Waku")
            if waku_cells.count() >= 2:
                w1_m = re.search(r"[1-8]", waku_cells.nth(0).inner_text())
                w2_m = re.search(r"[1-8]", waku_cells.nth(1).inner_text())
                if w1_m and w2_m:
                    a, b = sorted((int(w1_m.group(0)), int(w2_m.group(0))))
                    wf_pair = f"{a}-{b}"

        # ③ テキスト検索フォールバック
        txt = page.inner_text("body")
        if not cond:
            cond = extract_track_condition_from_text(txt[:1500], surface)
        if not wf_pair:
            m = re.search(r"枠連\s+([1-8])\s*[-－\s]+\s*([1-8])\b", txt)
            if m:
                a, b = sorted((int(m.group(1)), int(m.group(2))))
                wf_pair = f"{a}-{b}"
    except Exception:
        pass
    return wf_pair, cond


def fetch_jra_result_wakuren(page: Page, race_id: str) -> Optional[str]:
    wf_pair, _ = fetch_jra_result_info(page, race_id)
    return wf_pair


def verify_ipat_race_match(
    meta: RaceMeta, ipat_venue: str, ipat_rnum: str, ipat_rname: str, ipat_close: str
) -> Tuple[bool, str]:
    if meta.venue not in ipat_venue:
        return False, f"場名不一致: netkeiba={meta.venue} vs 即PAT={ipat_venue}"
    m_r = re.search(r"(\d{1,2})\s*R", ipat_rnum)
    if not m_r or int(m_r.group(1)) != meta.race_num:
        return False, f"R番号不一致: netkeiba={meta.race_num}R vs 即PAT={ipat_rnum}"
    nk_name, ip_name = normalize_text(meta.race_name), normalize_text(ipat_rname)
    if nk_name and ip_name and (nk_name not in ip_name) and (ip_name not in nk_name):
        return False, f"レース名不一致: netkeiba={meta.race_name} vs 即PAT={ipat_rname}"
    nk_min, ip_min = parse_hhmm_to_minutes(meta.post_time), parse_hhmm_to_minutes(ipat_close)
    if nk_min is None or ip_min is None:
        return False, f"時刻取得失敗: netkeiba={meta.post_time} vs 即PAT={ipat_close}"
    diff = nk_min - ip_min
    if not (0 <= diff <= 5):
        return False, f"時刻ズレ検知: 発走={meta.post_time} / 締切={ipat_close} (差{diff}分)"
    return True, f"照合OK ({meta.venue}{meta.race_num}R {meta.race_name} / 発走{meta.post_time}・締切{ipat_close})"


def login_ipat(page: Page) -> None:
    page.goto("https://www.ipat.jra.go.jp/", wait_until="domcontentloaded", timeout=25000)
    page.fill('input[name="inetid"]', os.environ["JRA_INET_ID"])
    btn1 = page.locator('a[title*="ログイン"], button:has-text("ログイン"), a:has-text("ログイン"), input[value*="ログイン"], input[type="submit"]').first
    if btn1.count() > 0:
        btn1.click()
    else:
        page.press('input[name="inetid"]', "Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(800)

    page.fill('input[name="i"]', os.environ["JRA_SUBSCRIBER_NO"])
    page.fill('input[name="p"]', os.environ["JRA_PIN"])
    page.fill('input[name="r"]', os.environ["JRA_PARS_NO"])
    btn2 = page.locator('a[title*="ネット投票"], button:has-text("ネット投票"), a:has-text("ネット投票"), input[value*="ネット投票"], input[type="submit"]').first
    if btn2.count() > 0:
        btn2.click()
    else:
        page.press('input[name="r"]', "Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)


def execute_ipat_bet(page: Page, order: BetOrder, dry_run: bool = True) -> bool:
    page.locator('button:has-text("オッズ投票"), a:has-text("オッズ投票")').first.click()
    page.wait_for_timeout(800)
    page.locator(f'button:has-text("{order.meta.venue}")').first.click()
    page.wait_for_timeout(500)

    race_btn = page.locator(f'button:has-text("{order.meta.race_num}R")').first
    btn_txt = race_btn.inner_text()
    ok, msg = verify_ipat_race_match(order.meta, order.meta.venue, btn_txt, btn_txt, btn_txt)
    if not ok:
        print(f"🛑 [JRA FAIL-SAFE / 選択前] {msg}", flush=True)
        return False
    print(f"✅ [JRA 事前照合OK] {msg}", flush=True)
    race_btn.click()
    page.wait_for_timeout(800)

    page.locator("select").nth(2).select_option(label=order.bet_type)
    page.wait_for_timeout(500)
    page.locator('button:has-text("オッズ投票（人気順）"), a:has-text("オッズ投票（人気順）")').first.click()
    page.wait_for_timeout(600)

    header_txt = page.inner_text("body")[:800]
    ok2, msg2 = verify_ipat_race_match(order.meta, header_txt, header_txt, order.meta.race_name, header_txt)
    if not ok2:
        print(f"🛑 [JRA FAIL-SAFE / 投票画面] {msg2}", flush=True)
        return False

    for pair_str, amount in order.tickets:
        if amount < 100 or amount % 100 != 0:
            print(f"🛑 [JRA FAIL-SAFE] 不正金額: {pair_str} -> {amount}円", flush=True)
            return False
        row_loc = page.locator(f'tr:has(td:text-is("{pair_str}"))').first
        if row_loc.count() == 0:
            print(f"🛑 [JRA FAIL-SAFE] 組番 {pair_str} 未検出", flush=True)
            return False
        row_loc.click()
        page.wait_for_timeout(200)
        page.locator('input[placeholder*="金額"], input[type="tel"]').last.fill(str(amount // 100))
        page.locator('button:has-text("セット")').last.click()
        page.wait_for_timeout(300)

    page.locator('button:has-text("入力終了"), button:has-text("購入予定リスト")').first.click()
    page.wait_for_timeout(800)

    cart_txt = page.inner_text("body")
    expected_total_str = f"{order.total_amount:,}円"
    if expected_total_str not in cart_txt:
        print(f"🛑 [JRA FAIL-SAFE / カート金額不一致] 期待={expected_total_str}", flush=True)
        return False
    for pair_str, _ in order.tickets:
        if pair_str not in cart_txt:
            print(f"🛑 [JRA FAIL-SAFE / カート組番欠落] {pair_str}", flush=True)
            return False

    if dry_run:
        print(f"🧪 [JRA DRY-RUN完了] 全フェイルセーフ通過 ({order.meta.venue}{order.meta.race_num}R {order.bet_type} {expected_total_str})", flush=True)
        return True

    page.locator('input[name*="total"], input[placeholder*="合計"]').last.fill(str(order.total_amount))
    page.locator('button:has-text("購入する")').last.click()
    page.wait_for_timeout(500)
    page.locator('button:has-text("OK")').first.click()
    print(f"🎯 [JRA 実購入完了] {order.meta.venue}{order.meta.race_num}R {order.bet_type} {expected_total_str}", flush=True)
    return True
