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
    楽天競馬 (keiba.rakuten.co.jp / bet.keiba.rakuten.co.jp)
    自動ログイン・実残高確認・オッズ再確認・2分前投票確定クラス (v11.9)
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

    def ensure_logged_in(self, page) -> bool:
        """ログイン状態を確認し、未ログインなら楽天ID・パスワードでログイン実行"""
        page.goto("https://keiba.rakuten.co.jp/", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(1200)
        body_txt = page.inner_text("body")

        if "ログアウト" in body_txt or ("精算" in body_txt and "ログイン" not in body_txt[:400]):
            self.logger.info("✅ 楽天競馬へすでにログイン済みです。")
            return True

        if not self.user_id or not self.password:
            self.logger.error("❌ RAKUTEN_USER_ID または RAKUTEN_PASSWORD がSecretsに設定されていません。")
            return False

        self.logger.info("🔐 楽天競馬へログインを実行します...")
        login_btn = page.locator('a:has-text("ログイン"), button:has-text("ログイン")').first
        if login_btn.count() > 0:
            login_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)

        user_box = page.locator('input[name="u"], input[id*="user"], input[type="email"], input[type="text"]').first
        user_box.fill(self.user_id)

        if page.locator('input[type="password"]').count() == 0:
            next_btn = page.locator('button:has-text("次へ"), #cta001, button[type="submit"]').first
            if next_btn.count() > 0:
                next_btn.click()
                page.wait_for_timeout(1500)

        pass_box = page.locator('input[name="p"], input[type="password"]').first
        pass_box.fill(self.password)
        submit_btn = page.locator('button:has-text("ログイン"), #cta011, input[type="submit"], button[type="submit"]').first
        submit_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        if "ログアウト" in page.inner_text("body"):
            self.logger.info("✅ 楽天競馬ログイン成功！")
            return True
        else:
            self.logger.error("❌ 楽天競馬ログインに失敗しました（パスキー設定やパスワードを確認してください）。")
            self._save_screenshot(page, "login_failed")
            return False

    def get_real_balance(self, page) -> Optional[int]:
        """
        楽天競馬の投票画面またはマイページから現在の『購入可能残高（投票可能額）』を円単位で取得する。
        取得できない場合は None を返す。
        """
        try:
            # まず現在のページテキストから残高表記を探す
            txt = page.inner_text("body")
            m = re.search(r"(?:投票可能額|購入可能額|購入限度額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", txt)
            if m:
                return int(m.group(1).replace(",", ""))

            # 見つからない場合は投票トップ画面を開いて購入可能額を確認
            page.goto("https://bet.keiba.rakuten.co.jp/", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            bet_txt = page.inner_text("body")
            m2 = re.search(r"(?:投票可能額|購入可能額|購入限度額|現在残高|残高)\s*[:：]?\s*([\d,]+)\s*円", bet_txt)
            if m2:
                return int(m2.group(1).replace(",", ""))
        except Exception as e:
            self.logger.warning(f"⚠️ 実残高の取得中に例外が発生しました: {e}")
        return None

    def vote_on_open_page(self, page, race_data: Dict, ticket: Dict) -> bool:
        """
        上部緑色ナビバー『投票』ボタンから投票画面を開き、
        会場・レース番号・式別（枠複 / 枠単 / 馬単）・組番・金額を入力して確定投票を実行する
        """
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

        if not self.vote_pin:
            self.logger.error("❌ RAKUTEN_VOTE_PIN（投票用4桁暗証番号）がSecretsに設定されていません。")
            return False

        try:
            vote_nav = page.locator('a:has-text("投票")').first
            if vote_nav.count() > 0:
                vote_nav.click()
                page.wait_for_timeout(1500)
            else:
                page.goto("https://bet.keiba.rakuten.co.jp/", timeout=30000)
                page.wait_for_timeout(1500)

            target_page = page.context.pages[-1]
            target_page.wait_for_load_state("domcontentloaded", timeout=15000)
            body_txt = target_page.inner_text("body")

            if "VOTI0003" in body_txt or "COMW0014" in body_txt or "サービス時間外" in body_txt or "投票受付は終了" in body_txt:
                self.logger.warning("⚠️ 楽天競馬の投票受付時間外、または締切済みのため投票を中断します。")
                self._save_screenshot(target_page, "vote_closed")
                return False

            target_page.locator(f'a:has-text("{venue}"), button:has-text("{venue}"), td:has-text("{venue}")').first.click()
            target_page.wait_for_timeout(1000)
            target_page.locator(f'a:has-text("{race_num}R"), button:has-text("{race_num}R"), td:has-text("{race_num}R")').first.click()
            target_page.wait_for_timeout(1000)

            target_page.locator(f'a:has-text("{btype}"), button:has-text("{btype}"), label:has-text("{btype}")').first.click()
            target_page.wait_for_timeout(800)

            for item in ticket["items"]:
                n1 = str(item["w1"])
                n2 = str(item["w2"])
                units = str(item["bet"] // 100)

                target_page.locator(
                    f'input[name*="first"][value="{n1}"], td[data-first="{n1}"], label[for*="first_{n1}"], label:has-text("{n1}")'
                ).first.click()
                target_page.wait_for_timeout(300)
                target_page.locator(
                    f'input[name*="second"][value="{n2}"], td[data-second="{n2}"], label[for*="second_{n2}"], label:has-text("{n2}")'
                ).last.click()
                target_page.wait_for_timeout(300)

                amt_box = target_page.locator(
                    'input[name*="amount"], input[id*="amount"], input[type="tel"], input[type="number"]'
                ).first
                amt_box.fill(units)
                target_page.locator('button:has-text("追加"), button:has-text("セット"), a:has-text("追加")').first.click()
                target_page.wait_for_timeout(400)

            target_page.locator(
                'button:has-text("投票内容確認"), a:has-text("投票内容確認"), button:has-text("確認画面へ")'
            ).first.click()
            target_page.wait_for_load_state("domcontentloaded", timeout=15000)
            target_page.wait_for_timeout(1000)

            total_box = target_page.locator('input[name*="total"], input[placeholder*="合計"]').first
            if total_box.count() > 0:
                total_box.fill(str(ticket["total_bet"]))

            pin_box = target_page.locator('input[type="password"], input[name*="pin"], input[placeholder*="暗証番号"]').first
            pin_box.fill(self.vote_pin)

            target_page.locator('button:has-text("投票する"), input[value*="投票する"], button:has-text("投票確定")').first.click()
            target_page.wait_for_load_state("domcontentloaded", timeout=15000)
            target_page.wait_for_timeout(1500)

            done_text = target_page.inner_text("body")
            if "投票を受け付けました" in done_text or "受付番号" in done_text or "投票完了" in done_text:
                self.logger.info(
                    f"🎉 [楽天競馬 投票確定完了] {venue}{race_num}R ({btype}) 合計{ticket['total_bet']}円の購入が完了しました！"
                )
                self._save_screenshot(target_page, "vote_success")
                return True
            else:
                self.logger.warning("⚠️ 投票完了画面の受付番号が確認できませんでした。")
                self._save_screenshot(target_page, "vote_unconfirmed")
                return False

        except Exception as e:
            self.logger.exception(f"❌ 楽天競馬 投票操作中にエラーが発生しました: {e}")
            self._save_screenshot(page, "vote_exception")
            return False
