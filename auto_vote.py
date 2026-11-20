import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from env_loader import load_env_file

load_env_file()

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class RakutenKeibaVoter:
    """
    楽天競馬 投票画面 (https://bet.keiba.rakuten.co.jp/bet/normal および /sp/menu) 対応
    動画（sp/menu -> オッズ投票 -> チェック選択 -> セット -> 金額入力へ -> sp/kaime -> 投票内容を確認する）
    に準拠した自動投票クラス (v11.9)
    """

    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.user_id = os.getenv("RAKUTEN_USER_ID", "").strip()
        self.password = os.getenv("RAKUTEN_PASSWORD", "").strip()
        self.vote_pin = os.getenv("RAKUTEN_VOTE_PIN", "").strip()

    def _save_screenshot(self, page, label: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = LOG_DIR / f"{label}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.info(f"📸 画面キャプチャ保存: {path}")
        except Exception:
            pass

    def _click_visible(self, page, selectors, timeout_ms: int = 4000) -> bool:
        for sel in selectors:
            loc = page.locator(f"{sel}:visible")
            for idx in range(loc.count()):
                item = loc.nth(idx)
                cls = item.get_attribute("class") or ""
                if "disableItem" in cls or "disabled" in cls:
                    continue
                try:
                    item.click(timeout=timeout_ms)
                    return True
                except Exception:
                    continue
        return False

    def _is_logged_in(self, page) -> bool:
        url = page.url
        txt = page.inner_text("body")
        if "bet.keiba.rakuten.co.jp/bet/" in url or "bet.keiba.rakuten.co.jp/sp/" in url:
            if any(k in txt for k in ("馬券カゴ", "購入限度額", "購入可能額", "投票内容を確認する", "オッズ投票", "ログアウト")):
                return True
        return False

    def ensure_logged_in(self, page) -> bool:
        page.goto("https://bet.keiba.rakuten.co.jp/bet/normal", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        if self._is_logged_in(page):
            self.logger.info("✅ 楽天競馬へすでにログイン済みです。")
            return True

        if not self.user_id or not self.password:
            self.logger.error("❌ RAKUTEN_USER_ID または RAKUTEN_PASSWORD がSecretsに設定されていません。")
            return False

        self.logger.info(f"🔐 楽天競馬へログインを実行します... (URL: {page.url})")
        try:
            user_box = page.locator(
                'input[name="u"], input[id="user_id"], input[id*="user"], input[type="email"], input[type="text"]'
            ).first
            user_box.wait_for(state="visible", timeout=20000)
            user_box.fill(self.user_id)
            page.wait_for_timeout(500)

            if page.locator('input[type="password"]').count() == 0:
                next_btn = page.locator(
                    '#cta001, button:has-text("次へ"), input[type="submit"], button[type="submit"]'
                ).first
                next_btn.click()
                page.wait_for_timeout(2000)

            if page.locator('input[type="password"]').count() == 0:
                alt_pw = page.locator('a:has-text("パスワード"), button:has-text("パスワード")').first
                if alt_pw.count() > 0:
                    alt_pw.click()
                    page.wait_for_timeout(1500)

            pass_box = page.locator('input[name="p"], input[type="password"]').first
            pass_box.wait_for(state="visible", timeout=20000)
            pass_box.fill(self.password)
            page.wait_for_timeout(500)

            submit_btn = page.locator(
                '#cta011, button:has-text("ログイン"), button:has-text("次へ"), input[type="submit"], button[type="submit"]'
            ).first
            submit_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)

            if "bet.keiba.rakuten.co.jp" not in page.url:
                page.goto("https://bet.keiba.rakuten.co.jp/bet/normal", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)

            if self._is_logged_in(page):
                self.logger.info(f"✅ 楽天競馬ログイン成功！ (URL: {page.url})")
                return True
            else:
                self.logger.error(f"❌ 楽天競馬ログインに失敗しました (現在URL: {page.url})")
                self._save_screenshot(page, "login_failed")
                return False
        except Exception as e:
            self.logger.exception(f"❌ 楽天競馬ログイン処理中にエラーが発生しました: {e}")
            self._save_screenshot(page, "login_exception")
            return False

    def get_real_balance(self, page) -> Optional[int]:
        try:
            txt = page.inner_text("body")
            m = re.search(r"(?:購入限度額|購入可能額|投票可能額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", txt)
            if m:
                return int(m.group(1).replace(",", ""))

            page.goto("https://bet.keiba.rakuten.co.jp/bet/normal", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            bet_txt = page.inner_text("body")
            m2 = re.search(r"(?:購入限度額|購入可能額|投票可能額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", bet_txt)
            if m2:
                return int(m2.group(1).replace(",", ""))
        except Exception as e:
            self.logger.warning(f"⚠️ 楽天競馬 実残高の取得中に例外が発生しました: {e}")
        return None

    def vote_on_open_page(self, page, race_data: Dict, ticket: Dict) -> bool:
        btype = ticket.get("bet_type", "枠複")
        if btype in ("枠連/枠複", "枠連"):
            btype = "枠複"
        sep = "->" if ticket.get("is_order_bet") else "-"
        venue = race_data["venue"]
        race_num = int(race_data["race_num"])

        self.logger.info(
            f"[楽天競馬 {btype}投票実行] {venue}{race_num}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍 / 最低払戻:{ticket['min_payout']}円"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (テスト確認モード)] 実際の投票送信はスキップし、成功として記録します。")
            return True

        try:
            page.goto("https://bet.keiba.rakuten.co.jp/sp/menu", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            body_txt = page.inner_text("body")

            if "VOTI0003" in body_txt or "COMW0014" in body_txt or "サービス時間外" in body_txt:
                self.logger.warning("⚠️ 楽天競馬の投票受付時間外のため投票を中断します。")
                self._save_screenshot(page, "vote_closed")
                return False

            # 1. 競馬場（大井 / 笠松 / 園田 等）を選択
            self._click_visible(page, [f'button:has-text("{venue}")', f'a:has-text("{venue}")', f'li:has-text("{venue}")'])
            page.wait_for_timeout(1000)

            # 2. 対象レース行の「オッズ投票」をクリック
            odds_btns = page.locator('a:has-text("オッズ投票"):visible, button:has-text("オッズ投票"):visible')
            if odds_btns.count() >= race_num:
                odds_btns.nth(race_num - 1).click(timeout=5000)
            elif odds_btns.count() > 0:
                odds_btns.first.click(timeout=5000)
            else:
                page.goto("https://bet.keiba.rakuten.co.jp/bet/normal", timeout=30000)
                page.wait_for_timeout(1200)
                venue_row = page.locator(f'table tr:has-text("{venue}")').first
                if venue_row.count() > 0 and venue_row.locator('a:has-text("オッズ")').count() >= race_num:
                    venue_row.locator('a:has-text("オッズ")').nth(race_num - 1).click(timeout=5000)

            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 3. 式別（枠複 / 枠単 / 馬単）を選択
            self._click_visible(
                page,
                [
                    f'button:has-text("{btype}")',
                    f'a:has-text("{btype}")',
                    f'li:has-text("{btype}")',
                    f'td:has-text("{btype}")',
                ],
            )
            page.wait_for_timeout(1000)

            # 4. オッズ一覧から対象組番（w1, w2）のチェックボックスをONにする
            checked_count = 0
            rows = page.locator("table tr")
            for item in ticket["items"]:
                n1, n2 = str(item["w1"]), str(item["w2"])
                for r_idx in range(rows.count()):
                    row = rows.nth(r_idx)
                    r_txt = re.sub(r"\s+", " ", row.inner_text()).strip()
                    m_pair = re.search(r"\b(\d{1,2})\s*[-－→]\s*(\d{1,2})\b", r_txt)
                    if m_pair:
                        a_str, b_str = m_pair.group(1), m_pair.group(2)
                        matched = (a_str == n1 and b_str == n2) or (
                            not ticket.get("is_order_bet") and a_str == n2 and b_str == n1
                        )
                        if matched:
                            cb = row.locator('input[type="checkbox"]').first
                            if cb.count() > 0:
                                if not cb.is_checked():
                                    cb.check(force=True)
                                checked_count += 1
                                page.wait_for_timeout(200)
                                break

            if checked_count == 0:
                self.logger.error("❌ オッズ一覧画面で対象買い目のチェックボックスを選択できませんでした。")
                self._save_screenshot(page, "odds_check_failed")
                return False

            self.logger.info(f"☑️ オッズ一覧で {checked_count}/{len(ticket['items'])} 点の買い目を選択しました。")

            # 5. 「セット」→「金額入力へ」をクリック
            if self._click_visible(page, ['button:has-text("セット")', 'a:has-text("セット")'], timeout_ms=3000):
                page.wait_for_timeout(800)
            self._click_visible(page, ['button:has-text("金額入力へ")', 'a:has-text("金額入力へ")'], timeout_ms=5000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 6. 馬券カゴ（sp/kaime）で各買い目の100円単位口数を入力
            kaime_rows = page.locator("table tr, .kaime-row, li")
            filled_count = 0
            for item in ticket["items"]:
                n1, n2 = str(item["w1"]), str(item["w2"])
                units = str(max(1, int(item["bet"]) // 100))
                pair_pattern = rf"\b{n1}\s*[-－→]\s*{n2}\b"
                rev_pattern = rf"\b{n2}\s*[-－→]\s*{n1}\b"

                for k_idx in range(kaime_rows.count()):
                    k_row = kaime_rows.nth(k_idx)
                    k_txt = k_row.inner_text()
                    is_match = bool(re.search(pair_pattern, k_txt)) or (
                        not ticket.get("is_order_bet") and bool(re.search(rev_pattern, k_txt))
                    )
                    if is_match:
                        inp = k_row.locator('input[type="tel"], input[type="number"], input[type="text"]').first
                        if inp.count() > 0:
                            inp.fill(units)
                            filled_count += 1
                            page.wait_for_timeout(200)
                            break

            if filled_count == 0:
                self.logger.error("❌ 馬券カゴで金額を1件も入力できませんでした。")
                self._save_screenshot(page, "kaime_fill_failed")
                return False

            # 7. 「投票内容を確認する」をクリック -> 確認画面へ遷移
            self._click_visible(
                page,
                ['button:has-text("投票内容を確認する")', 'a:has-text("投票内容を確認する")'],
                timeout_ms=5000,
            )
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            # 8. 確認画面で総投票金額・暗証番号（表示時のみ）を入力し「投票する」をクリック
            total_box = page.locator('input[type="tel"]:visible, input[type="number"]:visible, input[name*="total"]:visible').first
            if total_box.count() > 0:
                total_box.fill(str(ticket["total_bet"]))
                page.wait_for_timeout(300)

            pin_box = page.locator('input[type="password"]:visible, input[name*="pin"]:visible')
            if pin_box.count() > 0 and self.vote_pin:
                pin_box.first.fill(self.vote_pin)
                page.wait_for_timeout(300)

            self._click_visible(page, ['button:has-text("投票する")', 'input[value*="投票する"]'], timeout_ms=5000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

            done_text = page.inner_text("body")
            if "投票を受け付けました" in done_text or "受付番号" in done_text or "投票完了" in done_text:
                self.logger.info(
                    f"🎉 [楽天競馬 投票確定完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円の購入が完了しました！"
                )
                self._save_screenshot(page, "vote_success")
                return True
            else:
                self.logger.warning("⚠️ 投票完了画面の受付番号が確認できませんでした。")
                self._save_screenshot(page, "vote_unconfirmed")
                return False

        except Exception as e:
            self.logger.exception(f"❌ 楽天競馬 投票操作中にエラーが発生しました: {e}")
            self._save_screenshot(page, "vote_exception")
            return False


class JraIpatVoter:
    """
    JRA 即PAT (https://www.ipat.jra.go.jp/) 自動ログイン・残高確認・投票確定クラス（土日開催用）
    """

    def __init__(self, logger, headless: bool = True, dry_run: bool = False):
        self.logger = logger
        self.headless = headless
        self.dry_run = dry_run
        self.inet_id = os.getenv("JRA_INET_ID", "").strip()
        self.subscriber_no = os.getenv("JRA_SUBSCRIBER_NO", "").strip()
        self.pin = os.getenv("JRA_PIN", "").strip()
        self.pars_no = os.getenv("JRA_PARS_NO", "").strip()

    def _save_screenshot(self, page, label: str):
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = LOG_DIR / f"jra_{label}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            self.logger.info(f"📸 JRA画面キャプチャ保存: {path}")
        except Exception:
            pass

    def ensure_logged_in(self, page) -> bool:
        if not all([self.inet_id, self.subscriber_no, self.pin, self.pars_no]):
            self.logger.error("❌ JRA即PATの認証情報 (JRA_INET_ID / SUBSCRIBER_NO / PIN / PARS_NO) が不足しています。")
            return False

        page.goto("https://www.ipat.jra.go.jp/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)

        if "通常投票" in page.inner_text("body") or "購入限度額" in page.inner_text("body"):
            self.logger.info("✅ JRA即PATへすでにログイン済みです。")
            return True

        self.logger.info("🔐 JRA即PATへログインを実行します...")
        try:
            inet_box = page.locator('input[name="inetid"]').first
            if inet_box.count() > 0:
                inet_box.fill(self.inet_id)
                page.locator('a:has-text("ログイン"), input[type="submit"], button[type="submit"]').first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1200)

            page.locator('input[name="i"]').first.fill(self.subscriber_no)
            page.locator('input[name="p"]').first.fill(self.pin)
            page.locator('input[name="r"]').first.fill(self.pars_no)
            page.locator('a:has-text("ネット投票メニューへ"), input[type="submit"], button[type="submit"]').first.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

            if "通常投票" in page.inner_text("body") or "購入限度額" in page.inner_text("body"):
                self.logger.info("✅ JRA即PATログイン成功！")
                return True
            self.logger.error("❌ JRA即PATログインに失敗しました（受付時間外または認証情報エラー）。")
            self._save_screenshot(page, "login_failed")
            return False
        except Exception as e:
            self.logger.exception(f"❌ JRA即PATログイン処理中にエラーが発生しました: {e}")
            self._save_screenshot(page, "login_exception")
            return False

    def get_real_balance(self, page) -> Optional[int]:
        try:
            txt = page.inner_text("body")
            m = re.search(r"(?:購入限度額|購入可能額)\s*[:：]?\s*([\d,]+)\s*円", txt)
            if m:
                return int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return None

    def vote_on_open_page(self, page, race_data: Dict, ticket: Dict) -> bool:
        btype = "馬単" if ticket.get("bet_type") == "馬単" else "枠連"
        sep = "->" if ticket.get("is_order_bet") else "-"
        venue = race_data["venue"]
        race_num = int(race_data["race_num"])

        self.logger.info(
            f"[JRA即PAT {btype}投票実行] {venue}{race_num}R ({ticket['stage_name']}) "
            f"合計:{ticket['total_bet']}円 / 合成オッズ:{ticket['synthetic_odds']}倍"
        )
        for item in ticket["items"]:
            self.logger.info(
                f"  -> 買い目: {btype} {item['w1']}{sep}{item['w2']} ({item['odds']}倍 / {item['pop']}人気) : {item['bet']}円"
            )

        if self.dry_run:
            self.logger.info("[DRY-RUN (JRAテストモード)] 実際の投票送信はスキップし、成功として記録します。")
            return True

        try:
            page.locator('button:has-text("通常投票"), a:has-text("通常投票")').first.click()
            page.wait_for_timeout(1200)
            page.locator(f'button:has-text("{venue}"), a:has-text("{venue}")').first.click()
            page.wait_for_timeout(800)
            page.locator(f'button:has-text("{race_num}R"), a:has-text("{race_num}R")').first.click()
            page.wait_for_timeout(800)
            page.locator(f'select, button:has-text("{btype}"), a:has-text("{btype}")').first.click()
            page.wait_for_timeout(600)

            for item in ticket["items"]:
                n1, n2 = str(item["w1"]), str(item["w2"])
                units = str(item["bet"] // 100)
                page.locator(f'label:has-text("{n1}"), button:has-text("{n1}")').first.click()
                page.wait_for_timeout(200)
                page.locator(f'label:has-text("{n2}"), button:has-text("{n2}")').last.click()
                page.wait_for_timeout(200)
                page.locator('input[type="tel"], input[type="number"]').first.fill(units)
                page.locator('button:has-text("セット")').first.click()
                page.wait_for_timeout(300)

            page.locator('button:has-text("入力終了")').first.click()
            page.wait_for_timeout(1000)
            page.locator('input[type="tel"], input[type="number"]').first.fill(str(ticket["total_bet"]))
            page.locator('button:has-text("投票")').first.click()
            page.wait_for_timeout(1500)

            if "投票を受け付けました" in page.inner_text("body") or "受付番号" in page.inner_text("body"):
                self.logger.info(f"🎉 [JRA即PAT 投票確定完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円")
                self._save_screenshot(page, "vote_success")
                return True
            self._save_screenshot(page, "vote_unconfirmed")
            return False
        except Exception as e:
            self.logger.exception(f"❌ JRA即PAT 投票操作中にエラーが発生しました: {e}")
            self._save_screenshot(page, "vote_exception")
            return False
