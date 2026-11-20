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
    楽天競馬 PC版投票画面 (https://bet.keiba.rakuten.co.jp/bet/normal) 対応（平日・毎日）
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
        if btype == "枠連/枠複":
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
            page.goto("https://bet.keiba.rakuten.co.jp/bet/normal", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            body_txt = page.inner_text("body")

            if "VOTI0003" in body_txt or "COMW0014" in body_txt or "サービス時間外" in body_txt:
                self.logger.warning("⚠️ 楽天競馬の投票受付時間外のため投票を中断します。")
                self._save_screenshot(page, "vote_closed")
                return False

            venue_row = page.locator(f'table tr:has-text("{venue}")').first
            if venue_row.count() > 0:
                odds_links = venue_row.locator('a:has-text("オッズ")')
                if odds_links.count() >= race_num:
                    odds_links.nth(race_num - 1).click()
                else:
                    page.locator(f'a:has-text("{venue}"), button:has-text("{venue}")').first.click()
                    page.wait_for_timeout(1000)
                    page.locator(f'a:has-text("{race_num}R"), button:has-text("{race_num}R")').first.click()
            else:
                page.locator('a:has-text("オッズ")').first.click()
                page.wait_for_timeout(1000)
                page.locator(f'a:has-text("{venue}")').first.click()
                page.wait_for_timeout(800)
                page.locator(f'a:has-text("{race_num}R")').first.click()

            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            page.locator(
                f'a:has-text("{btype}"), button:has-text("{btype}"), li:has-text("{btype}"), label:has-text("{btype}")'
            ).first.click()
            page.wait_for_timeout(1200)

            checked_count = 0
            rows = page.locator("table tr")
            for item in ticket["items"]:
                n1, n2 = str(item["w1"]), str(item["w2"])
                for r_idx in range(rows.count()):
                    row = rows.nth(r_idx)
                    nums = re.findall(r"\b\d{1,2}\b", row.inner_text().strip())
                    if len(nums) >= 2 and nums[0] == n1 and nums[1] == n2:
                        cb = row.locator('input[type="checkbox"]').first
                        if cb.count() > 0:
                            if not cb.is_checked():
                                cb.check(force=True)
                            checked_count += 1
                            page.wait_for_timeout(200)
                            break

            if checked_count == 0:
                self.logger.error("❌ オッズ一覧で対象買い目のチェックボックスを選択できませんでした。")
                self._save_screenshot(page, "odds_check_failed")
                return False

            add_btn = page.locator(
                'button:has-text("セット"), a:has-text("セット"), button:has-text("馬券カゴ"), '
                'button:has-text("金額入力へ"), a:has-text("金額入力へ"), input[value*="セット"]'
            ).first
            if add_btn.count() > 0:
                add_btn.click()
                page.wait_for_timeout(1200)

            kaime_rows = page.locator("table tr, .kaime-row, li")
            filled_count = 0
            for item in ticket["items"]:
                n1, n2 = str(item["w1"]), str(item["w2"])
                units = str(item["bet"] // 100)
                pair_pattern = rf"{n1}\s*[-－→]\s*{n2}"
                for k_idx in range(kaime_rows.count()):
                    k_row = kaime_rows.nth(k_idx)
                    if re.search(pair_pattern, k_row.inner_text()):
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

            page.locator(
                'button:has-text("投票内容を確認する"), a:has-text("投票内容を確認する"), input[value*="投票内容を確認"]'
            ).first.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)

            total_box = page.locator('input[type="tel"], input[type="number"], input[name*="total"], input[type="text"]').first
            if total_box.count() > 0:
                total_box.fill(str(ticket["total_bet"]))
                page.wait_for_timeout(300)

            pin_box = page.locator('input[type="password"], input[name*="pin"]')
            if pin_box.count() > 0 and self.vote_pin:
                pin_box.first.fill(self.vote_pin)
                page.wait_for_timeout(300)

            page.locator('button:has-text("投票する"), input[value*="投票する"]').first.click()
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
