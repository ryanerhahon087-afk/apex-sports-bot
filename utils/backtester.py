"""
APEX/SPORTS BOT — Backtester
Fetches settled Kalshi markets from the past N days, simulates what the bot
would have picked using the same AI pipeline, and measures historical accuracy.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Series to pull settled markets from
BACKTEST_SERIES = [
    "KXNBAGAME",
    "KXNBATOTAL",
    "KXMLBGAME",
    "KXMLBTOTAL",
    "KXNHLGAME",
    "KXNHLTOTAL",
]

# Injected into every research prompt to prevent hindsight bias
PRE_GAME_INSTRUCTION = """IMPORTANT — BACKTEST MODE: This game has already been played.
You must analyse it as if it has NOT happened yet. Use ONLY information that would have
been available BEFORE game time: pre-game injury reports, recent team form, historical
head-to-head records, pre-game betting lines, home/away and rest factors.
Do NOT mention the final score, winner, or any post-game news."""


class BacktestEngine:
    """
    Runs the full research → evaluate → assemble pipeline against settled markets
    and compares predicted picks to actual outcomes.
    """

    def __init__(self, kalshi_client, intelligence, database, config):
        self._kalshi = kalshi_client
        self._intel = intelligence
        self._db = database
        self._config = config
        self._ticker_result_cache: dict = {}   # ticker → "yes"/"no"

    # ── PUBLIC ENTRY POINT ────────────────────────────────────────────────────

    async def run(self, days: int = 7) -> dict:
        """
        Full backtest over the past `days` days.
        Returns a structured report dict (also saved to DB).
        """
        days = min(max(days, 1), 14)
        logger.info(f"[BACKTEST] ═══ Starting {days}-day backtest ═══")

        # 1. Pull settled markets
        settled = await self._fetch_settled_markets()
        if not settled:
            return {"error": "No settled markets found — check API connectivity"}

        # 2. Group by date; keep only the target window
        daily_games = self._group_by_date(settled, days)
        if not daily_games:
            return {"error": f"No completed games in the past {days} days"}

        logger.info(
            f"[BACKTEST] Data across {len(daily_games)} days: "
            + ", ".join(sorted(daily_games.keys()))
        )

        # 3. Simulate each day
        balance = 100.0
        daily_results = []
        total_legs_all = winning_legs_all = 0
        total_slips = winning_slips = 0

        for day_idx, date_str in enumerate(sorted(daily_games.keys())):
            games = daily_games[date_str][:5]   # cap at 5 games / day
            logger.info(
                f"[BACKTEST] ── Day {day_idx+1}/{len(daily_games)}: "
                f"{date_str} | {len(games)} game(s)"
            )

            # Research + evaluate
            candidates = await self._research_and_evaluate(
                games, day_idx, len(daily_games)
            )

            if len(candidates) < self._config.DAILY_MIN_LEGS:
                logger.warning(
                    f"[BACKTEST] {date_str}: only {len(candidates)} candidates "
                    f"(need ≥{self._config.DAILY_MIN_LEGS}), skipping"
                )
                continue

            # Assemble slip + check results
            day_result = await self._assemble_and_check(candidates, balance)
            if not day_result:
                continue

            day_result["date"] = date_str
            day_result["balance_before"] = round(balance, 2)

            pnl = (
                day_result["stake"] * (day_result["combined_odds"] - 1)
                if day_result["won"]
                else -day_result["stake"]
            )
            balance = round(balance + pnl, 2)
            day_result["pnl"] = round(pnl, 2)
            day_result["balance_after"] = balance

            daily_results.append(day_result)
            total_slips += 1
            winning_slips += int(day_result["won"])
            total_legs_all += day_result["leg_count"]
            winning_legs_all += day_result["winning_legs"]

        # 4. Build report
        total_pnl = round(balance - 100.0, 2)
        report = {
            "period": f"Last {days} days",
            "run_at": datetime.now(timezone.utc).isoformat(),
            "period_days": days,
            "total_slips": total_slips,
            "winning_slips": winning_slips,
            "slip_win_rate": (
                round(winning_slips / total_slips * 100, 1) if total_slips else 0
            ),
            "total_legs": total_legs_all,
            "winning_legs": winning_legs_all,
            "leg_win_rate": (
                round(winning_legs_all / total_legs_all * 100, 1) if total_legs_all else 0
            ),
            "total_pnl": total_pnl,
            "starting_balance": 100.0,
            "ending_balance": balance,
            "daily_breakdown": daily_results,
        }

        logger.info(
            f"[BACKTEST] ═══ Complete ═══ | "
            f"{winning_slips}/{total_slips} slips | "
            f"{winning_legs_all}/{total_legs_all} legs | "
            f"P&L ${total_pnl:+.2f}"
        )

        self._db.save_backtest_result(days, report)
        return report

    # ── DATA FETCHING ─────────────────────────────────────────────────────────

    async def _fetch_settled_markets(self) -> dict:
        results = {}
        for series in BACKTEST_SERIES:
            markets = await self._kalshi.fetch_series_markets(
                series, status="settled", limit=200
            )
            if markets:
                results[series] = markets
            logger.info(f"[BACKTEST] {series}: {len(markets)} settled markets")
            await asyncio.sleep(0.5)

        total = sum(len(v) for v in results.values())
        logger.info(f"[BACKTEST] Fetched {total} settled markets across {len(results)} series")
        return results

    def _group_by_date(self, all_markets: dict, days: int) -> dict:
        """Group settled markets by date, restricting to the past `days` days."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        daily: dict = {}

        for series, markets in all_markets.items():
            sport = self._series_to_sport(series)
            events: dict = {}

            for market in markets:
                event_ticker = market.get("event_ticker", "")
                if not event_ticker:
                    continue

                time_str = (
                    market.get("occurrence_datetime") or
                    market.get("close_time", "")
                )
                if not time_str:
                    continue

                try:
                    game_time = datetime.fromisoformat(
                        time_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    continue

                # Only completed games within the window
                if game_time <= cutoff or game_time >= now:
                    continue

                date_str = game_time.strftime("%Y-%m-%d")

                if event_ticker not in events:
                    events[event_ticker] = {
                        "event_ticker": event_ticker,
                        "title": market.get("title", ""),
                        "sport": sport,
                        "game_start": time_str,
                        "date": date_str,
                        "markets": [],
                    }

                yes_ask = float(market.get("yes_ask_dollars") or 0)
                result  = market.get("result", "")

                events[event_ticker]["markets"].append({
                    "ticker":      market.get("ticker", ""),
                    "title":       market.get("title", ""),
                    "series":      series,
                    "market_type": self._infer_market_type(series),
                    "yes_ask":     yes_ask,
                    "yes_bid":     float(market.get("yes_bid_dollars") or 0),
                    "result":      result,
                    "subtitle":    market.get("yes_sub_title", ""),
                })

                # Pre-cache actual result for later comparison
                if market.get("ticker") and result:
                    self._ticker_result_cache[market["ticker"]] = result

            # Bucket by date, de-duping by event_ticker
            for game in events.values():
                d = game["date"]
                if d not in daily:
                    daily[d] = []
                seen = {g["event_ticker"] for g in daily[d]}
                if game["event_ticker"] not in seen:
                    daily[d].append(game)

        return daily

    # ── RESEARCH & EVALUATION ─────────────────────────────────────────────────

    async def _research_and_evaluate(
        self, games: list, day_idx: int, total_days: int
    ) -> list:
        """Run research + pick evaluation for all markets in a day's games."""
        candidates = []

        for g_idx, game in enumerate(games):
            title = game.get("title", "")[:55]
            logger.info(
                f"[BACKTEST] Day {day_idx+1}/{total_days} "
                f"game {g_idx+1}/{len(games)}: {title}"
            )

            research = await self._intel.research_game(
                game, extra_instruction=PRE_GAME_INSTRUCTION
            )
            await asyncio.sleep(3)

            if research.get("research_quality", 0) == 0:
                logger.warning(f"[BACKTEST] Research poor/failed: {title}")
                continue

            for market in game.get("markets", []):
                yes_ask = market.get("yes_ask", 0)
                if yes_ask <= 0.05 or yes_ask >= 0.95:
                    continue  # skip near-certain / nearly-impossible lines

                pick_dict = {
                    "game":         game.get("title", ""),
                    "market_type":  market.get("market_type", "GAME_WINNER"),
                    "pick":         market.get("subtitle") or market.get("title", ""),
                    "current_odds": yes_ask,
                }

                evaluation = await self._intel.evaluate_pick(
                    pick_dict, research, "DAILY"
                )
                await asyncio.sleep(3)

                if not evaluation.get("include_in_slip"):
                    continue
                conf = float(evaluation.get("confidence") or 0)
                if conf < self._config.DAILY_MIN_CONFIDENCE:
                    continue

                individual_odds = round(1.0 / yes_ask, 4) if yes_ask > 0 else 1.0

                candidates.append({
                    "game":          game.get("title", ""),
                    "sport":         game.get("sport", "UNKNOWN"),
                    "market_type":   market.get("market_type", "GAME_WINNER"),
                    "pick":          evaluation.get("calibrated_pick") or pick_dict["pick"],
                    "kalshi_ticker": market.get("ticker", ""),
                    "individual_odds": individual_odds,
                    "confidence":    conf,
                    "yes_ask":       yes_ask,
                    "game_start":    game.get("game_start", ""),
                    "ai_reasoning":  evaluation.get("reasoning", ""),
                    "win_probability": float(evaluation.get("win_probability") or 0),
                })

        logger.info(
            f"[BACKTEST] Day {day_idx+1}: {len(candidates)} candidates "
            f"from {len(games)} games"
        )
        return candidates

    # ── SLIP ASSEMBLY & RESULT CHECK ──────────────────────────────────────────

    async def _assemble_and_check(
        self, candidates: list, balance: float
    ) -> Optional[dict]:
        """Assemble a simulated slip, then compare each leg to actual results."""
        slip_data = await self._intel.assemble_slip(
            candidates=candidates,
            slip_type="DAILY",
            target_odds=self._config.DAILY_TARGET_COMBINED,
            min_legs=self._config.DAILY_MIN_LEGS,
            max_legs=self._config.DAILY_MAX_LEGS,
        )
        await asyncio.sleep(3)

        if not slip_data or not slip_data.get("selected_legs"):
            logger.warning("[BACKTEST] Slip assembly returned no legs")
            return None

        legs = slip_data["selected_legs"]

        # Recalculate combined_odds if AI returned 0
        combined_odds = float(slip_data.get("combined_odds") or 0)
        if combined_odds <= 0:
            combined_odds = 1.0
            for leg in legs:
                lo = float(leg.get("individual_odds") or 0)
                if lo > 0:
                    combined_odds *= lo
            combined_odds = round(combined_odds, 4)

        stake = round(
            min(balance * self._config.DAILY_STAKE_PCT, self._config.DAILY_HARD_CAP),
            2,
        )

        # Score each leg against real Kalshi result
        legs_detail = []
        winning_legs = 0

        for leg in legs:
            ticker = leg.get("kalshi_ticker") or leg.get("ticker", "")
            actual = self._ticker_result_cache.get(ticker, "unknown")
            leg_won = actual == "yes"   # we always pick the YES side
            if leg_won:
                winning_legs += 1

            legs_detail.append({
                "game":          leg.get("game", ""),
                "pick":          leg.get("pick", ""),
                "ticker":        ticker,
                "odds":          round(float(leg.get("individual_odds") or 0), 3),
                "confidence":    round(float(leg.get("confidence") or 0), 1),
                "actual_result": actual,
                "won":           leg_won,
            })

        all_won = len(legs_detail) > 0 and winning_legs == len(legs_detail)

        return {
            "legs":            legs_detail,
            "leg_count":       len(legs_detail),
            "winning_legs":    winning_legs,
            "combined_odds":   combined_odds,
            "stake":           stake,
            "potential_payout": round(stake * combined_odds, 2),
            "won":             all_won,
            "result":          "WIN" if all_won else "LOSS",
        }

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _series_to_sport(self, series: str) -> str:
        if "NBA" in series: return "NBA"
        if "NFL" in series: return "NFL"
        if "MLB" in series: return "MLB"
        if "NHL" in series: return "NHL"
        return "OTHER"

    def _infer_market_type(self, series: str) -> str:
        if "GAME" in series:  return "GAME_WINNER"
        if "TOTAL" in series: return "TOTAL"
        return "OTHER"
