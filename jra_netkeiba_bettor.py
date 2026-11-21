import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from playwright.sync_api import Page


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


def normalize_text(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    return s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def parse_hhmm_to_minutes(hhmm: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _fetch_netkeiba_odds_api(page: Page, race_id: str, type_num: str, ordered: bool) -> List[Dict]:
    """
    netkeiba の内部オッズJSON APIから直接オッズ・人気順を取得する。
      type_num: "3"=枠連, "4"=馬連, "6"=馬単
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
            print(f"⚠️ [API HTTP {res.status}] race_id={race_id} type={type_num}", flush=True)
            return []

        raw_txt = res.text()
        payload = json.loads(raw_txt)
        odds_root = (payload.get("data") or {}).get("odds") or {}
        odds_dict = odds_root.get(type_num) or {}
        if not isinstance(odds_dict, dict):
            return []

        combos: List[Dict] = []
        for k, v in odds_dict.items():
            # キーは4桁 (例: "0508" -> 5-8)
            if not re.fullmatch(r"\d{4}", str(k)) or not isinstance(v, list) or len(v) < 3:
                continue
            a, b = int(k[:2]), int(k[2:])
            if not ordered:
                a, b = min(a, b), max(a, b)
            try:
                odds_val = float(str(v[0]).replace(",", ""))
                pop_val = int(str(v[2]))
            except (ValueError, TypeError):
                continue
            if odds_val >= 1.0 and pop_val >= 1:
                combos.append({
                    "pop": pop_val,
                    "pair": f"{a}-{b}",
                    "w1": a,
                    "w2": b,
                    "odds": odds_val,
                })

        combos.sort(key=lambda x: x["pop"])
        return combos
    except Exception as e:
        print(f"⚠️ [API取得例外] race_id={race_id} type={type_num}: {e}", flush=True)
        return []


# ==============================================================================
# 1. netkeiba からレース情報・オッズを取得
# ==============================================================================
def fetch_netkeiba_race_and_odds(
    page: Page,
    race_id: str,
    bet_type: str = "枠連",
) -> Tuple[RaceMeta, List[Dict]]:
    shutuba_url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    page.goto(shutuba_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(400)

    body = page.inner_text("body")
    race_name = ""
    if page.locator(".RaceName").count() > 0:
        race_name = page.locator(".RaceName").first.inner_text().strip()

    m_time = re.search(r"(\d{1,2}:\d{2})\s*発走", body)
    post_time = m_time.group(1) if m_time else ""

    venue_map = {
        "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
        "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
    }
    venue = venue_map.get(race_id[4:6], "")
    race_num = int(race_id[10:12])
    has_scratch = ("取消" in body) or ("除外" in body)

    meta = RaceMeta(
        race_id=race_id,
        venue=venue,
        race_num=race_num,
        race_name=race_name,
        post_time=post_time,
        has_scratch=has_scratch,
    )

    type_map = {"枠連": ("3", False), "馬連": ("4", False), "馬単": ("6", True)}
    type_num, ordered = type_map.get(bet_type, ("3", False))
    combos = _fetch_netkeiba_odds_api(page, race_id, type_num, ordered)
    return meta, combos


# ==============================================================================
# 2. フェイルセーフ照合ロジック
# ==============================================================================
def verify_ipat_race_match(
    meta: RaceMeta,
    ipat_venue_text: str,
    ipat_race_num_text: str,
    ipat_race_name_text: str,
    ipat_close_time_text: str,
) -> Tuple[bool, str]:
    if meta.venue not in ipat_venue_text:
        return False, f"場名不一致: netkeiba={meta.venue} vs 即PAT={ipat_venue_text}"

    m_r = re.search(r"(\d{1,2})\s*R", ipat_race_num_text)
    if not m_r or int(m_r.group(1)) != meta.race_num:
        return False, f"R番号不一致: netkeiba={meta.race_num}R vs 即PAT={ipat_race_num_text}"

    nk_name = normalize_text(meta.race_name)
    ip_name = normalize_text(ipat_race_name_text)
    if nk_name and ip_name and (nk_name not in ip_name) and (ip_name not in nk_name):
        return False, f"レース名不一致: netkeiba={meta.race_name} vs 即PAT={ipat_race_name_text}"

    nk_min = parse_hhmm_to_minutes(meta.post_time)
    ip_min = parse_hhmm_to_minutes(ipat_close_time_text)
    if nk_min is None or ip_min is None:
        return False, f"時刻取得失敗: netkeiba={meta.post_time} vs 即PAT={ipat_close_time_text}"

    diff = nk_min - ip_min
    if not (0 <= diff <= 5):
        return False, f"時刻ズレ検知: 発走={meta.post_time} / 即PAT締切={ipat_close_time_text} (差{diff}分)"

    return True, f"照合OK ({meta.venue}{meta.race_num}R {meta.race_name} / 発走{meta.post_time}・締切{ipat_close_time_text})"


# ==============================================================================
# 3. JRA 即PAT ログイン〜フェイルセーフ照合〜投票実行
# ==============================================================================
def login_ipat(page: Page) -> None:
    page.goto("https://www.ipat.jra.go.jp/", wait_until="domcontentloaded", timeout=25000)
    page.fill('input[name="inetid"]', os.environ["JRA_INET_ID"])
    page.click('a:has-text("ログイン"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")

    page.fill('input[name="i"]', os.environ["JRA_SUBSCRIBER_NO"])
    page.fill('input[name="p"]', os.environ["JRA_PIN"])
    page.fill('input[name="r"]', os.environ["JRA_PARS_NO"])
    page.click('a:has-text("ネット投票メニューへ"), input[type="submit"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)


def execute_ipat_bet(page: Page, order: BetOrder, dry_run: bool = True) -> bool:
    if order.meta.has_scratch:
        print("🛑 [ABORT] 取消/除外馬ありのため購入中止", flush=True)
        return False

    page.locator('button:has-text("オッズ投票"), a:has-text("オッズ投票")').first.click()
    page.wait_for_timeout(800)

    page.locator(f'button:has-text("{order.meta.venue}")').first.click()
    page.wait_for_timeout(500)

    race_btn = page.locator(f'button:has-text("{order.meta.race_num}R")').first
    btn_text = race_btn.inner_text()
    ok, msg = verify_ipat_race_match(order.meta, order.meta.venue, btn_text, btn_text, btn_text)
    if not ok:
        print(f"🛑 [FAIL-SAFE ABORT / レース選択前] {msg}", flush=True)
        return False

    print(f"✅ [事前照合通過] {msg}", flush=True)
    race_btn.click()
    page.wait_for_timeout(800)

    page.locator("select").nth(2).select_option(label=order.bet_type)
    page.wait_for_timeout(500)
    page.locator('button:has-text("オッズ投票（人気順）"), a:has-text("オッズ投票（人気順）")').first.click()
    page.wait_for_timeout(600)

    header_txt = page.inner_text("body")[:800]
    ok2, msg2 = verify_ipat_race_match(order.meta, header_txt, header_txt, order.meta.race_name, header_txt)
    if not ok2:
        print(f"🛑 [FAIL-SAFE ABORT / 投票画面] {msg2}", flush=True)
        return False

    for pair_str, amount in order.tickets:
        if amount < 100 or amount % 100 != 0:
            print(f"🛑 [FAIL-SAFE ABORT] 不正な金額指定: {pair_str} -> {amount}円", flush=True)
            return False
        row_loc = page.locator(f'tr:has(td:text-is("{pair_str}"))').first
        if row_loc.count() == 0:
            print(f"🛑 [FAIL-SAFE ABORT] 即PAT画面に組番 {pair_str} が見つかりません", flush=True)
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
        print(f"🛑 [FAIL-SAFE ABORT / カート金額不一致] 期待={expected_total_str}", flush=True)
        return False

    for pair_str, _ in order.tickets:
        if pair_str not in cart_txt:
            print(f"🛑 [FAIL-SAFE ABORT / カート組番欠落] {pair_str} が購入予定リストにありません", flush=True)
            return False

    if dry_run:
        print(f"🧪 [DRY-RUN完了] 全フェイルセーフ通過（{order.meta.venue}{order.meta.race_num}R / {order.bet_type} / 合計{expected_total_str}）", flush=True)
        return True

    page.locator('input[name*="total"], input[placeholder*="合計"]').last.fill(str(order.total_amount))
    page.locator('button:has-text("購入する")').last.click()
    page.wait_for_timeout(500)
    page.locator('button:has-text("OK")').first.click()
    print(f"🎯 [実購入完了] {order.meta.venue}{order.meta.race_num}R {order.bet_type} 合計{expected_total_str}", flush=True)
    return True
