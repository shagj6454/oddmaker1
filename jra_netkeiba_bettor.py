import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page

from core_v12 import (
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


def _parse_past_c1_from_cell(cell_text: str) -> Tuple[Optional[int], int]:
    """前走セル文字列から1コーナー通過順と前走出走頭数を抽出（例: '16頭 ... 2-2-2-1'）"""
    m_head = re.search(r"(\d{1,2})頭", cell_text)
    prev_n = int(m_head.group(1)) if m_head else 14
    # 通過順パターン (例: 1-1-1-1, 3-4-2, 12-11)
    m_pass = re.search(r"\b(\d{1,2})(?:-\d{1,2}){1,3}\b", cell_text)
    if m_pass:
        c1 = int(m_pass.group(1))
        if 1 <= c1 <= 18:
            return c1, prev_n
    return None, prev_n


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
    # 馬場状態 (良 / 稍重 / 重 / 不良)
    m_cond = re.search(r"馬場[:：]?\s*(良|稍重|稍|重|不良)", header_info)
    track_cond = m_cond.group(1) if m_cond else "良"
    if track_cond == "稍":
        track_cond = "稍重"

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

    # 全出走馬の枠番・馬番・前走1コーナー位置取りを取得（rowspan省略行は直前の枠を引き継ぎ）
    horses_info: List[Dict] = []
    horse_map: Dict[int, Dict] = {}
    rows = page.locator("tr.HorseList")
    last_waku = 0
    for i in range(rows.count()):
        r = rows.nth(i)
        u_loc = r.locator("td[class*='Umaban']").first
        if u_loc.count() == 0:
            continue
        um = re.search(r"\d{1,2}", u_loc.inner_text())
        if not um:
            continue
        umaban = int(um.group(0))

        waku = 0
        w_loc = r.locator("td[class*='Waku']").first
        if w_loc.count() > 0:
            wm = re.search(r"[1-8]", w_loc.inner_text())
            if wm:
                waku = int(wm.group(0))
            else:
                cls = w_loc.get_attribute("class") or ""
                cm = re.search(r"Waku([1-8])", cls)
                if cm:
                    waku = int(cm.group(1))
        if waku > 0:
            last_waku = waku
        elif last_waku > 0:
            waku = last_waku

        # 前走（1番左の過去走セル td.Past）から1コーナー通過順を取得
        prev_c1, prev_n = None, 14
        past_cells = r.locator("td.Past")
        for p_idx in range(min(past_cells.count(), 2)):
            c1, pn = _parse_past_c1_from_cell(past_cells.nth(p_idx).inner_text())
            if c1 is not None:
                prev_c1, prev_n = c1, pn
                break
        leg_type = classify_leg_type(prev_c1, prev_n)
        info = {"umaban": umaban, "waku": waku, "prev_c1": prev_c1, "prev_field_size": prev_n, "leg_type": leg_type}
        horses_info.append(info)
        horse_map[umaban] = info

    tan_list = fetch_netkeiba_odds_api(page, race_id, "1", ordered=False)
    horse_count = len(horses_info) or len(tan_list)
    if horse_count == 0:
        m_hc = re.search(r"(\d{1,2})頭", r_data2)
        if m_hc:
            horse_count = int(m_hc.group(1))

    # 万一枠番が0の馬が残っていれば頭数・馬番から100%補完
    for h in horses_info:
        if h["waku"] <= 0 and horse_count > 0:
            h["waku"] = calc_waku_from_umaban(h["umaban"], horse_count)

    fav1_odds = tan_list[0]["odds"] if tan_list else 9.9
    fav1_horse = tan_list[0]["horse"] if tan_list else 0
    fav1_info = horse_map.get(fav1_horse, {})
    fav1_waku = int(fav1_info.get("waku") or 0)
    if fav1_waku <= 0 and fav1_horse > 0 and horse_count > 0:
        fav1_waku = calc_waku_from_umaban(fav1_horse, horse_count)
    fav1_leg_type = str(fav1_info.get("leg_type") or "不明")

    waku_leg_scores = calc_waku_leg_scores(horses_info, surface=surface, track_condition=track_cond, venue=venue, distance=distance)

    meta = RaceMeta(
        race_id=race_id, venue=venue, race_num=race_num, race_name=race_name,
        post_time=post_time, has_scratch=has_scratch, horse_count=horse_count,
        race_class=race_class, surface=surface, distance=distance, track_condition=track_cond,
        fav1_win_odds=fav1_odds, fav1_waku=fav1_waku, fav1_leg_type=fav1_leg_type,
        horses_info=horses_info, waku_leg_scores=waku_leg_scores,
    )

    type_map = {"枠連": ("3", False), "馬連": ("4", False), "馬単": ("6", True)}
    type_num, ordered = type_map.get(bet_type, ("3", False))
    combos = fetch_netkeiba_odds_api(page, race_id, type_num, ordered)
    return meta, combos


def fetch_jra_result_wakuren(page: Page, race_id: str) -> Optional[str]:
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(400)

        # ① 払戻テーブルの枠連行から取得
        row = page.locator("tr.Wakuren td.Result, tr:has(th:text-is('枠連')) td.Result").first
        if row.count() > 0:
            nums = re.findall(r"[1-8]", row.inner_text())
            if len(nums) >= 2:
                a, b = sorted((int(nums[0]), int(nums[1])))
                return f"{a}-{b}"

        # ② 着順テーブルの1着・2着の枠番から直接算出（最も確実）
        waku_cells = page.locator("#All_Result_Table tbody tr td.Num.Waku, table.RaceTable01 tbody tr td.Waku")
        if waku_cells.count() >= 2:
            w1_m = re.search(r"[1-8]", waku_cells.nth(0).inner_text())
            w2_m = re.search(r"[1-8]", waku_cells.nth(1).inner_text())
            if w1_m and w2_m:
                a, b = sorted((int(w1_m.group(0)), int(w2_m.group(0))))
                return f"{a}-{b}"

        # ③ テキスト検索フォールバック
        txt = page.inner_text("body")
        m = re.search(r"枠連\s+([1-8])\s*[-－\s]+\s*([1-8])\b", txt)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            return f"{a}-{b}"
    except Exception:
        pass
    return None


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
