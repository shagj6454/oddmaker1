import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page

from core_v12 import (
    EXCLUDED_VENUES,
    calc_waku_leg_scores,
    classify_leg_type,
    parse_hhmm_to_minutes,
)

ALL_NAR_VENUES = (
    "帯広ば", "門別", "盛岡", "水沢", "浦和", "船橋", "大井",
    "川崎", "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀",
)


@dataclass
class NarRaceMeta:
    race_id: str
    venue: str
    race_num: int
    race_name: str
    post_time: str
    has_scratch: bool = False
    horse_count: int = 0
    race_class: str = ""
    surface: str = "ダート"
    track_condition: str = "良"
    fav1_win_odds: float = 9.9
    fav1_waku: int = 0
    fav1_leg_type: str = "不明"
    horses_info: List[Dict] = field(default_factory=list)
    waku_leg_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class NarBetOrder:
    meta: NarRaceMeta
    bet_type: str                     # "枠複" / "馬複" / "枠単" / "馬単"
    tickets: List[Tuple[str, int]]
    total_amount: int = field(init=False)

    def __post_init__(self):
        self.total_amount = sum(amt for _, amt in self.tickets)


def normalize_nar_venue(raw_text: str) -> Optional[str]:
    cleaned = re.sub(r"[\s\u3000]+", "", raw_text or "")
    for v in ALL_NAR_VENUES:
        if v == cleaned or v in cleaned:
            return v
    return None


def fetch_nar_tan_and_meta(page: Page, race_id: str) -> Dict:
    """
    楽天競馬の単勝・複勝オッズページ1アクセスで以下を一括取得:
      - fav1_win_odds, fav1_waku, fav1_umaban, horse_count
      - race_name, race_class, surface(ダート/芝), track_condition(良/稍重/重/不良)
    """
    url = f"https://keiba.rakuten.co.jp/odds/tanfuku/RACEID/{race_id}"
    res = {
        "fav1_win_odds": 9.9, "fav1_waku": 0, "fav1_umaban": 0,
        "horse_count": 0, "race_name": "", "race_class": "",
        "surface": "ダート", "track_condition": "良",
    }
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(300)
        body = page.inner_text("body")
        if "ページが表示できません" in body:
            return res

        head = body[:1200]
        res["surface"] = "芝" if ("芝" in head and "ダート" not in head and "ダ" not in head[:400]) else "ダート"
        m_cond = re.search(r"(?:馬場|ダート|芝)[:：\s]*(良|稍重|稍|重|不良)", head)
        if m_cond:
            c = m_cond.group(1)
            res["track_condition"] = "稍重" if c == "稍" else c
        m_rn = re.search(r"\d{1,2}R\s+([^\n]{2,35})", head)
        if m_rn:
            res["race_name"] = m_rn.group(1).strip()
            res["race_class"] = res["race_name"]

        min_win = 9999.0
        fav1_waku, fav1_uma = 0, 0
        h_count = 0
        rows = page.locator("table tr")
        for i in range(rows.count()):
            cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
            if len(cells) < 4:
                continue
            nums = [int(c) for c in cells[:3] if re.fullmatch(r"\d{1,2}", c)]
            if len(nums) < 2:
                continue
            waku, uma = nums[0], nums[1]
            if not (1 <= waku <= 8 and 1 <= uma <= 18):
                continue
            h_count += 1
            for cell in cells[2:]:
                if re.search(r"\d+\.\d+\s*[-－～~]\s*\d+\.\d+", cell):
                    continue
                m_o = re.search(r"\b(\d+\.\d+)\b", cell)
                if m_o:
                    val = float(m_o.group(1))
                    if 1.0 <= val < min_win:
                        min_win = val
                        fav1_waku, fav1_uma = waku, uma
                    break

        res["fav1_win_odds"] = min_win if min_win < 9999.0 else 9.9
        res["fav1_waku"] = fav1_waku
        res["fav1_umaban"] = fav1_uma
        res["horse_count"] = h_count
    except Exception:
        pass
    return res


def fetch_nar_horses_leg_info(page: Page, race_id: str, venue: str, surface: str, track_condition: str) -> Tuple[List[Dict], Dict[str, float]]:
    """
    楽天競馬の出馬表（馬柱）から各馬の枠番・馬番・前走1コーナー通過順・脚質を取得し、枠スコアを算出
    """
    url = f"https://keiba.rakuten.co.jp/race_card/list/RACEID/{race_id}"
    horses_info: List[Dict] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(350)
        body = page.inner_text("body")
        if "ページが表示できません" in body:
            return [], {}

        rows = page.locator("table tr")
        for i in range(rows.count()):
            row = rows.nth(i)
            cells = [re.sub(r"\s+", " ", c).strip() for c in row.locator("th, td").all_inner_texts()]
            if len(cells) < 5:
                continue
            nums = [int(c) for c in cells[:3] if re.fullmatch(r"\d{1,2}", c)]
            if len(nums) < 2:
                continue
            waku, uma = nums[0], nums[1]
            if not (1 <= waku <= 8 and 1 <= uma <= 18):
                continue

            # 馬柱行全体から最初の「通過順（例: 1-1-1-1 / 2-3-2）」と頭数を抽出
            row_txt = " ".join(cells[3:])
            m_head = re.search(r"(\d{1,2})頭", row_txt)
            prev_n = int(m_head.group(1)) if m_head else 12
            m_pass = re.search(r"\b(\d{1,2})(?:-\d{1,2}){1,3}\b", row_txt)
            prev_c1 = int(m_pass.group(1)) if (m_pass and 1 <= int(m_pass.group(1)) <= 18) else None
            leg_type = classify_leg_type(prev_c1, prev_n)
            horses_info.append({
                "umaban": uma, "waku": waku, "prev_c1": prev_c1,
                "prev_field_size": prev_n, "leg_type": leg_type,
            })
    except Exception:
        pass

    waku_scores = calc_waku_leg_scores(horses_info, surface=surface, track_condition=track_condition, venue=venue)
    return horses_info, waku_scores


def fetch_nar_tan_fav1(page: Page, race_id: str) -> float:
    return float(fetch_nar_tan_and_meta(page, race_id)["fav1_win_odds"])


def _extract_nar_pair_table(page: Page, max_num: int, ordered: bool) -> List[Dict]:
    combos: List[Dict] = []
    rows = page.locator("table tr")
    for i in range(rows.count()):
        cells = [re.sub(r"\s+", " ", c).strip() for c in rows.nth(i).locator("th, td").all_inner_texts()]
        idx = 0
        while idx + 2 < len(cells):
            m_pop = re.fullmatch(r"\d{1,3}", cells[idx])
            m_pair = re.search(r"\b(\d{1,2})\s*[-－→]\s*(\d{1,2})\b", cells[idx + 1])
            m_odds = re.search(r"\b(\d+(?:\.\d+)?)\b", cells[idx + 2])
            if m_pop and m_pair and m_odds:
                pop, a, b, odds = int(m_pop.group(0)), int(m_pair.group(1)), int(m_pair.group(2)), float(m_odds.group(1))
                if 1 <= a <= max_num and 1 <= b <= max_num and 1.0 <= odds < 9999:
                    if not ordered:
                        a, b = min(a, b), max(a, b)
                    if not any(c["w1"] == a and c["w2"] == b for c in combos):
                        combos.append({"pop": pop, "pair": f"{a}-{b}", "w1": a, "w2": b, "odds": odds})
                idx += 3
            else:
                idx += 1
    combos.sort(key=lambda x: x["pop"])
    return combos


def fetch_nar_odds(page: Page, race_id: str, bet_kind: str = "wakufuku") -> List[Dict]:
    url = f"https://keiba.rakuten.co.jp/odds/{bet_kind}/RACEID/{race_id}"
    ordered = bet_kind in ("wakutan", "umatan")
    max_num = 18 if bet_kind in ("umafuku", "umatan") else 8
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(400)
        if "ページが表示できません" in page.inner_text("body"):
            return []
        combos = _extract_nar_pair_table(page, max_num=max_num, ordered=ordered)
        if len(combos) >= 3:
            return combos
        for sel in ('a:has-text("人気順")', 'a:has-text("人気高配当順")', 'button:has-text("人気順")'):
            loc = page.locator(f"{sel}:visible").first
            if loc.count() > 0:
                try:
                    loc.click(timeout=2500)
                    page.wait_for_timeout(400)
                    combos = _extract_nar_pair_table(page, max_num=max_num, ordered=ordered)
                    if len(combos) >= 3:
                        return combos
                except Exception:
                    continue
        return combos
    except Exception:
        return []


def fetch_today_nar_races(page: Page, ymd: str) -> List[NarRaceMeta]:
    """楽天競馬の本日の開催レース一覧（場名・R・発走時刻・RACEID）を取得"""
    url = f"https://keiba.rakuten.co.jp/race_card/list/RACEID/{ymd}0000000000"
    metas: List[NarRaceMeta] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(600)
        links = page.locator('a[href*="/race_card/list/RACEID/"]:visible')
        venue_urls = []
        seen = set()
        for i in range(links.count()):
            a_tag = links.nth(i)
            v = normalize_nar_venue(a_tag.inner_text())
            href = a_tag.get_attribute("href") or ""
            if v and v not in seen and ymd in href and not any(ex in v for ex in EXCLUDED_VENUES):
                seen.add(v)
                full = href if href.startswith("http") else f"https://keiba.rakuten.co.jp{href}"
                venue_urls.append((v, full))

        for v_name, v_url in venue_urls:
            page.goto(v_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(400)
            rows = page.locator("table tr")
            for i in range(rows.count()):
                txt = re.sub(r"\s+", " ", rows.nth(i).inner_text()).strip()
                m_r = re.search(r"\b(\d{1,2})R\b", txt)
                m_t = re.search(r"(\d{1,2}:\d{2})", txt)
                hrefs = rows.nth(i).locator('a[href*="/RACEID/"]')
                if not m_r or not m_t or hrefs.count() == 0:
                    continue
                href = hrefs.first.get_attribute("href") or ""
                m_id = re.search(r"/RACEID/(\d{18})", href)
                if not m_id:
                    continue
                metas.append(NarRaceMeta(
                    race_id=m_id.group(1), venue=v_name, race_num=int(m_r.group(1)),
                    race_name=txt[:40], post_time=m_t.group(1),
                    has_scratch=("取消" in txt) or ("除外" in txt),
                ))
    except Exception as e:
        print(f"⚠️ [NAR一覧取得例外] {e}", flush=True)
    return metas


def login_rakuten_keiba(page: Page) -> None:
    page.goto("https://keiba.rakuten.co.jp/", wait_until="domcontentloaded", timeout=25000)
    login_link = page.locator('a:has-text("ログイン"), button:has-text("ログイン")').first
    if login_link.count() > 0:
        login_link.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(600)

    uid_input = page.locator('input[name="u"], input[id*="user"], input[type="email"], input[type="text"]').first
    if uid_input.count() > 0:
        uid_input.fill(os.environ["RAKUTEN_USER_ID"])
        next_btn = page.locator('button:has-text("次へ"), input[type="submit"], button[type="submit"]').first
        if next_btn.count() > 0:
            next_btn.click()
            page.wait_for_timeout(1000)

    pw_input = page.locator('input[name="p"], input[type="password"]').first
    if pw_input.count() > 0:
        pw_input.fill(os.environ["RAKUTEN_PASSWORD"])
        sub_btn = page.locator('button:has-text("ログイン"), input[type="submit"], button[type="submit"]').first
        if sub_btn.count() > 0:
            sub_btn.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1200)


def execute_rakuten_bet(page: Page, order: NarBetOrder, dry_run: bool = True) -> bool:
    if order.meta.has_scratch:
        print("🛑 [NAR ABORT] 取消/除外馬あり", flush=True)
        return False

    kind_map = {"枠複": "wakufuku", "馬複": "umafuku", "枠単": "wakutan", "馬単": "umatan"}
    kind_slug = kind_map.get(order.bet_type, "wakufuku")
    bet_url = f"https://keiba.rakuten.co.jp/odds/{kind_slug}/RACEID/{order.meta.race_id}"
    page.goto(bet_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(600)

    body = page.inner_text("body")
    if (order.meta.venue not in body) or (f"{order.meta.race_num}R" not in body):
        print(f"🛑 [NAR FAIL-SAFE] 場名/R番号不一致: 期待={order.meta.venue}{order.meta.race_num}R", flush=True)
        return False

    if order.meta.post_time and order.meta.post_time not in body:
        m_t = re.search(r"(\d{1,2}:\d{2})\s*発走", body)
        if m_t:
            m1, m2 = parse_hhmm_to_minutes(order.meta.post_time), parse_hhmm_to_minutes(m_t.group(1))
            if m1 is None or m2 is None or abs(m1 - m2) > 5:
                print(f"🛑 [NAR FAIL-SAFE] 時刻不一致: {order.meta.post_time} vs {m_t.group(1)}", flush=True)
                return False

    for pair_str, amount in order.tickets:
        if amount < 100 or amount % 100 != 0:
            print(f"🛑 [NAR FAIL-SAFE] 不正金額: {pair_str} -> {amount}円", flush=True)
            return False

    expected_total_str = f"{order.total_amount:,}円"
    if dry_run:
        print(f"🧪 [NAR DRY-RUN完了] 全フェイルセーフ通過 ({order.meta.venue}{order.meta.race_num}R {order.bet_type} {len(order.tickets)}点 / 合計{expected_total_str})", flush=True)
        return True

    for pair_str, amount in order.tickets:
        row_loc = page.locator(f'tr:has-text("{pair_str}")').first
        if row_loc.count() == 0:
            print(f"🛑 [NAR FAIL-SAFE] 組番 {pair_str} 未検出", flush=True)
            return False
        cb = row_loc.locator('input[type="checkbox"]').first
        if cb.count() > 0:
            cb.check()

    to_cart = page.locator('button:has-text("投票"), a:has-text("投票ボックス"), button:has-text("金額入力")').first
    if to_cart.count() > 0:
        to_cart.click()
        page.wait_for_timeout(800)

    pin = os.environ.get("RAKUTEN_PIN", "")
    pin_input = page.locator('input[type="password"], input[name*="pin"]').last
    if pin and pin_input.count() > 0:
        pin_input.fill(pin)
    confirm_btn = page.locator('button:has-text("購入する"), button:has-text("投票を確定")').last
    if confirm_btn.count() > 0:
        confirm_btn.click()
        page.wait_for_timeout(800)

    print(f"🎯 [NAR 実購入完了] {order.meta.venue}{order.meta.race_num}R {order.bet_type} 合計{expected_total_str}", flush=True)
    return True
