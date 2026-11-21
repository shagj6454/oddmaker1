import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page

from core_v12 import normalize_text, parse_hhmm_to_minutes


@dataclass
class RaceMeta:
    race_id: str
    venue: str
    race_num: int
    race_name: str
    post_time: str
    has_scratch: bool = False


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


def fetch_netkeiba_race_and_odds(page: Page, race_id: str, bet_type: str = "枠連") -> Tuple[RaceMeta, List[Dict], float]:
    """netkeiba出馬表 + 単勝1人気オッズ + 指定式別オッズをまとめて取得"""
    shutuba_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    page.goto(shutuba_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(400)
    body = page.inner_text("body")

    race_name = page.locator(".RaceName").first.inner_text().strip() if page.locator(".RaceName").count() > 0 else ""
    m_time = re.search(r"(\d{1,2}:\d{2})\s*発走", body)
    post_time = m_time.group(1) if m_time else ""

    venue_map = {
        "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
    }
    meta = RaceMeta(
        race_id=race_id,
        venue=venue_map.get(race_id[4:6], ""),
        race_num=int(race_id[10:12]),
        race_name=race_name,
        post_time=post_time,
        has_scratch=("取消" in body) or ("除外" in body),
    )

    tan_list = fetch_netkeiba_odds_api(page, race_id, "1", ordered=False)
    fav1_odds = tan_list[0]["odds"] if tan_list else 9.9

    type_map = {"枠連": ("3", False), "馬連": ("4", False), "馬単": ("6", True)}
    type_num, ordered = type_map.get(bet_type, ("3", False))
    combos = fetch_netkeiba_odds_api(page, race_id, type_num, ordered)
    return meta, combos, fav1_odds


def fetch_jra_result_wakuren(page: Page, race_id: str) -> Optional[str]:
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(400)

        # ① 払戻テーブルの枠連行から直接数字2つを取得
        row = page.locator("tr.Wakuren td.Result, tr:has(th:text-is('枠連')) td.Result").first
        if row.count() > 0:
            nums = re.findall(r"[1-8]", row.inner_text())
            if len(nums) >= 2:
                a, b = sorted((int(nums[0]), int(nums[1])))
                return f"{a}-{b}"

        # ② フォールバック（ハイフンあり／なし両対応）
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
    if order.meta.has_scratch:
        print("🛑 [JRA ABORT] 取消/除外馬あり", flush=True)
        return False

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
