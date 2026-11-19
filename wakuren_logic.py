import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "wakuren_state_v11_9.json"

INITIAL_BANKROLL = 52000
EXCLUDED_VENUES = {"金沢", "笠松", "帯広", "帯広ば"}

STAGES = [
    {"name": "1戦目", "default_prev_loss": 0, "target": 1350},
    {"name": "2戦目", "default_prev_loss": 500, "target": 2400},
    {"name": "3戦目", "default_prev_loss": 1400, "target": 4050},
    {"name": "4戦目", "default_prev_loss": 2900, "target": 7000},
    {"name": "5戦目", "default_prev_loss": 5500, "target": 11900},
    {"name": "6戦目", "default_prev_loss": 9900, "target": 20250},
    {"name": "7戦目", "default_prev_loss": 17400, "target": 34500},
    {"name": "8戦目", "default_prev_loss": 30200, "target": 58800},
]

class WakurenRuleEngine:
    def __init__(self, logger):
        self.logger = logger
        self.state = self.load_state()

    def load_state(self) -> Dict:
        today_str = datetime.now().strftime("%Y-%m-%d")
        default_state = {
            "stage_idx": 0,
            "balance": INITIAL_BANKROLL,
            "set_loss": 0,
            "daily_stats": {"date": today_str, "total_bet": 0, "total_payout": 0},
            "active_ticket": None,
        }
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if data.get("daily_stats", {}).get("date") != today_str:
                    data["daily_stats"] = {"date": today_str, "total_bet": 0, "total_payout": 0}
                return data
            except Exception as e:
                self.logger.warning(f"状態ファイルの読み込みに失敗したため初期化します: {e}")
        return default_state

    def save_state(self):
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def check_defensive_filters(
        self,
        venue: str,
        is_obstacle: bool,
        num_horses: int,
        fav1_win_odds: float,
        has_scratch: bool,
    ) -> Tuple[bool, List[str], bool]:
        '''
        戻り値: (参加可否, 完全見送り理由リスト, 1着固定軸モードフラグ)
        v11.9:
          - 障害・ばんえい・金沢/笠松・出走取消あり -> 完全除外
          - 単勝1倍台 (< 2.0倍) または 少頭数 (<= 11頭) -> 【枠単優先（枠単なし時は馬単）1着固定流し】へ自動切替
        '''
        reasons = []
        use_axis_mode = False

        if any(ex in venue for ex in EXCLUDED_VENUES):
            reasons.append(f"除外対象競馬場 ({venue}: 小規模プール・オッズ急落防止)")
        if is_obstacle:
            reasons.append("障害戦 / ばんえい競馬")
        if has_scratch:
            reasons.append("出走取消・競走除外馬あり (オッズ歪み防止)")

        if num_horses <= 11 or fav1_win_odds < 2.0:
            use_axis_mode = True

        return (len(reasons) == 0), reasons, use_axis_mode

    @staticmethod
    def calc_synthetic_odds(combos: List[Dict]) -> float:
        if not combos:
            return 0.0
        inv_sum = sum(1.0 / c["odds"] for c in combos if c["odds"] > 0)
        return (1.0 / inv_sum) if inv_sum > 0 else 0.0

    def build_ticket(
        self,
        race_num: int,
        wakuren_combos: List[Dict],
        wakutan_combos: Optional[List[Dict]] = None,
        umatan_combos: Optional[List[Dict]] = None,
        use_axis_mode: bool = False,
        fav1_frame_no: Optional[int] = None,
        fav1_horse_no: Optional[int] = None,
    ) -> Tuple[Optional[Dict], str]:
        is_order_bet = False

        if use_axis_mode:
            threshold = 1.80
            is_order_bet = True
            w_tan_list = wakutan_combos or []
            u_tan_list = umatan_combos or []

            # ① 枠単が発売されているレースは【枠単】を優先採用
            if len(w_tan_list) >= 3:
                sorted_wt = sorted(w_tan_list, key=lambda x: x["odds"])
                axis_f = fav1_frame_no if fav1_frame_no else sorted_wt[0]["w1"]
                axis_targets = [c for c in sorted_wt if c["w1"] == axis_f][:5]
                if len(axis_targets) < 3:
                    return None, f"枠単1枠軸モード：1番人気({axis_f}枠)1着固定の買い目が不足のため見送り"
                synth = self.calc_synthetic_odds(axis_targets)
                if synth < threshold:
                    return None, f"枠単1枠軸流し：合成オッズ ({synth:.2f}倍 < {threshold:.2f}倍) のため見送り"

                selected = axis_targets
                bet_type = "枠単"
                mode_label = "wakutan_1fav_axis"
                self.logger.info(
                    f"⚡ [v11.9 枠単優先モード発動] 1着固定:{axis_f}枠 -> 相手上位{len(selected)}枠 / 合成オッズ:{synth:.2f}倍 >= 1.80倍"
                )
            # ② 枠単発売なし（JRA・園田等）のレースは【馬単】へ自動フォールバック
            else:
                if not u_tan_list:
                    return None, "馬単1頭軸モード：枠単なし・馬単オッズデータ未取得のため見送り"
                sorted_u = sorted(u_tan_list, key=lambda x: x["odds"])
                axis_h = fav1_horse_no if fav1_horse_no else sorted_u[0]["w1"]
                axis_targets = [c for c in sorted_u if c["w1"] == axis_h][:5]
                if len(axis_targets) < 5:
                    return None, f"馬単1頭軸モード：1番人気({axis_h}番)1着固定の買い目が5点未満のため見送り"
                synth = self.calc_synthetic_odds(axis_targets)
                if synth < threshold:
                    return None, f"馬単1頭軸5点流し：合成オッズ ({synth:.2f}倍 < {threshold:.2f}倍) のため見送り"

                selected = axis_targets
                bet_type = "馬単"
                mode_label = "umatan_1fav_axis_5pt"
                self.logger.info(
                    f"⚡ [v11.9 馬単フォールバック発動(枠単なし)] 1着固定:{axis_h}番 -> 相手2〜6番人気(5点) / 合成オッズ:{synth:.2f}倍 >= 1.80倍"
                )
        else:
            sorted_combos = sorted(wakuren_combos, key=lambda x: (x["odds"], x["w1"], x["w2"]))
            for idx, c in enumerate(sorted_combos):
                c["pop"] = c.get("pop") or (idx + 1)
                c["is_zorome"] = (c["w1"] == c["w2"])

            is_early = (race_num <= 8)
            threshold = 1.80 if is_early else 2.50
            bet_type = "枠連/枠複"
            mode_label = "early_1_8R" if is_early else "late_9_12R"

            if is_early:
                selected = [c for c in sorted_combos if 2 <= c["pop"] <= 5 and not c["is_zorome"]]
                synth = self.calc_synthetic_odds(selected)
                if synth < threshold:
                    self.logger.info(f"前半戦(2〜5人気)合成オッズ {synth:.2f}倍 < 1.80倍 のため 3〜6人気へスライド判定")
                    selected = [c for c in sorted_combos if 3 <= c["pop"] <= 6 and not c["is_zorome"]]
                    synth = self.calc_synthetic_odds(selected)
                    if synth < threshold:
                        return None, f"前半戦スライド後も合成オッズ基準未満 ({synth:.2f}倍 < {threshold:.2f}倍)"
            else:
                selected = [c for c in sorted_combos if 7 <= c["pop"] <= 12 and not c["is_zorome"]]
                synth = self.calc_synthetic_odds(selected)
                if synth < threshold:
                    return None, f"後半戦(7〜12人気)合成オッズ基準未満 ({synth:.2f}倍 < {threshold:.2f}倍)"

        if not selected:
            return None, "有効な買い目が存在しません"

        stage_idx = self.state["stage_idx"]
        prev_loss = self.state["set_loss"]
        target = max(STAGES[stage_idx]["target"], prev_loss + 850)

        total_bet = 0
        items = []
        for idx, c in enumerate(selected):
            bet = max(100, int(round((target / c["odds"]) / 100.0)) * 100)
            total_bet += bet
            w1 = c["w1"] if is_order_bet else min(c["w1"], c["w2"])
            w2 = c["w2"] if is_order_bet else max(c["w1"], c["w2"])
            items.append({
                "w1": w1,
                "w2": w2,
                "odds": c["odds"],
                "pop": c.get("pop", idx + 1),
                "bet": bet,
            })

        required = prev_loss + total_bet + 100
        for _ in range(60):
            changed = False
            for x in items:
                if round(x["bet"] * x["odds"]) < required:
                    x["bet"] += 100
                    total_bet += 100
                    required = prev_loss + total_bet + 100
                    changed = True
            if not changed:
                break

        min_payout = min(int(round(x["bet"] * x["odds"])) for x in items)
        for x in items:
            x["est_payout"] = int(round(x["bet"] * x["odds"]))

        ticket = {
            "stage_idx": stage_idx,
            "stage_name": STAGES[stage_idx]["name"],
            "race_num": race_num,
            "bet_type": bet_type,
            "is_order_bet": is_order_bet,
            "mode": mode_label,
            "synthetic_odds": round(synth, 2),
            "total_bet": total_bet,
            "min_payout": min_payout,
            "items": items,
        }
        return ticket, "OK"

    def record_result(self, winning_w1: int, winning_w2: int, dividend_yen: int) -> Dict:
        ticket = self.state.get("active_ticket")
        if not ticket:
            raise ValueError("照合対象のアクティブな馬券データがありません。")

        is_order = ticket.get("is_order_bet", False)
        lo = winning_w1 if is_order else min(winning_w1, winning_w2)
        hi = winning_w2 if is_order else max(winning_w1, winning_w2)
        sep = "->" if is_order else "-"
        total_bet = ticket["total_bet"]
        self.state["daily_stats"]["total_bet"] += total_bet

        matched = next((x for x in ticket["items"] if x["w1"] == lo and x["w2"] == hi), None)
        if matched:
            payout = int(round((matched["bet"] / 100.0) * dividend_yen))
            self.state["daily_stats"]["total_payout"] += payout
            net_set_profit = payout - (self.state["set_loss"] + total_bet)
            self.state["balance"] = self.state["balance"] - total_bet + payout
            self.state["set_loss"] = 0
            self.state["stage_idx"] = 0
            status = "HIT"
            self.logger.info(
                f"🎯 的中! [{ticket.get('bet_type', '枠連')}] {lo}{sep}{hi} ({dividend_yen}円) / 払戻 {payout}円 / セット純利益 +{net_set_profit}円 -> 1戦目へリセット"
            )
        else:
            payout = 0
            new_loss = self.state["set_loss"] + total_bet
            self.state["balance"] -= total_bet
            if self.state["stage_idx"] >= 7:
                self.logger.warning("⚠️ 8戦目不的中のため損切りルール発動 -> 次回1戦目へリセット")
                self.state["set_loss"] = 0
                self.state["stage_idx"] = 0
            else:
                self.state["set_loss"] = new_loss
                self.state["stage_idx"] += 1
            status = "MISS"
            self.logger.info(
                f"❌ 不的中 (結果: {lo}{sep}{hi}) / 投資 -{total_bet}円 / 次回: {STAGES[self.state['stage_idx']]['name']} (累計負債 {self.state['set_loss']}円)"
            )

        self.state["active_ticket"] = None
        self.save_state()
        return {"status": status, "payout": payout, "state": self.state}
