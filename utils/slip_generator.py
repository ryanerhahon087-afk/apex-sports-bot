"""
APEX/SPORTS BOT — Slip Generator
Orchestrates the full pipeline: market scan → research → evaluate → assemble → save.
Runs once per day at the configured generation window.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class SlipGenerator:
    """
    Generates all three slip types daily.
    Pipeline: scan markets → group by game → research → evaluate → assemble → save
    """

    def __init__(self, kalshi_client, intelligence, database, config):
        self._kalshi = kalshi_client
        self._intel = intelligence
        self._db = database
        self._config = config

    # ── MAIN ENTRY POINT ──────────────────────────────────────────────────────

    async def generate_all_slips(self) -> dict:
        """
        Full daily generation cycle.
        Returns dict with generated slip IDs.
        """
        logger.info("[GEN] Starting daily slip generation")
        results = {"daily": None, "rollover": None, "lotto": None}

        try:
            # Step 1: Fetch all available markets
            all_markets = await self._kalshi.fetch_all_sports_markets(
                self._config.ACTIVE_SERIES
            )
            if not all_markets:
                logger.warning("[GEN] No markets available — skipping generation")
                return results

            # Step 2: Group markets by game and filter for today's games
            games = self._group_markets_by_game(all_markets)
            logger.info(f"[GEN] Found {len(games)} games to analyze")

            if not games:
                logger.warning("[GEN] No eligible games found")
                return results

            # Step 3: Research all games (parallel for speed)
            researched_games = await self._research_all_games(games)

            # Step 4: Evaluate all picks
            all_candidates = await self._evaluate_all_picks(researched_games)
            logger.info(f"[GEN] {len(all_candidates)} total candidates evaluated")

            # Step 5: Generate each slip type
            balance = self._db.get_balance()

            # Daily slip
            daily_slip = await self._generate_daily_slip(all_candidates, balance)
            if daily_slip:
                results["daily"] = daily_slip
                logger.info(f"[GEN] Daily slip generated: "
                           f"{daily_slip['combined_odds']:.2f}x odds")

            # Rollover slip (only if active rollover exists)
            active_rollover = self._db.get_active_rollover()
            if active_rollover:
                rollover_slip = await self._generate_rollover_slip(
                    all_candidates, balance, active_rollover
                )
                if rollover_slip:
                    results["rollover"] = rollover_slip

            # Check if it's time for a new lotto slip (weekly)
            if self._should_generate_lotto():
                lotto_slip = await self._generate_lotto_slip(
                    all_candidates, balance
                )
                if lotto_slip:
                    results["lotto"] = lotto_slip

            logger.info(f"[GEN] Generation complete: {results}")
            return results

        except Exception as e:
            logger.error(f"[GEN] Generation failed: {e}", exc_info=True)
            return results

    # ── MARKET GROUPING ───────────────────────────────────────────────────────

    def _group_markets_by_game(self, all_markets: dict) -> list:
        """
        Group markets by game event and filter for upcoming games.
        Returns list of game dicts with their available markets.
        """
        games_dict = {}
        now = datetime.now(timezone.utc)
        min_start = now + timedelta(hours=self._config.MIN_HOURS_BEFORE_GAME)

        for series, markets in all_markets.items():
            sport = self._series_to_sport(series)

            for market in markets:
                event_ticker = market.get("event_ticker", "")
                if not event_ticker:
                    continue

                # Parse game close time
                close_time_str = market.get("close_time", "")
                if not close_time_str:
                    continue

                try:
                    close_time = datetime.fromisoformat(
                        close_time_str.replace("Z", "+00:00")
                    )
                except Exception:
                    continue

                # Only games that haven't started yet
                if close_time <= min_start:
                    continue

                # Skip markets with no real liquidity data
                yes_ask = float(market.get("yes_ask_dollars") or 0)
                yes_bid = float(market.get("yes_bid_dollars") or 0)

                if event_ticker not in games_dict:
                    games_dict[event_ticker] = {
                        "event_ticker": event_ticker,
                        "title": market.get("title", ""),
                        "sport": sport,
                        "close_time": close_time_str,
                        "markets": [],
                    }

                games_dict[event_ticker]["markets"].append({
                    "ticker": market.get("ticker"),
                    "title": market.get("title", ""),
                    "series": series,
                    "market_type": self._infer_market_type(series, market),
                    "yes_ask": yes_ask,
                    "yes_bid": yes_bid,
                    "subtitle": market.get("yes_sub_title", ""),
                })

        games = list(games_dict.values())
        # Sort by sport priority
        priority = {s: i for i, s in enumerate(self._config.SPORT_PRIORITY)}
        games.sort(key=lambda g: priority.get(g["sport"], 99))
        return games

    def _series_to_sport(self, series: str) -> str:
        if series.startswith("KXNBA"):
            return "NBA"
        elif series.startswith("KXMLB"):
            return "MLB"
        elif series.startswith("KXNHL"):
            return "NHL"
        elif series.startswith("KXMLS") or series.startswith("KXSOCCER"):
            return "SOCCER"
        return "OTHER"

    def _infer_market_type(self, series: str, market: dict) -> str:
        if "GAME" in series:
            return "GAME_WINNER"
        elif "TOTAL" in series:
            return "TOTAL"
        elif "PLAYER" in series:
            return "PLAYER_PROP"
        elif "SPREAD" in series:
            return "SPREAD"
        return "OTHER"

    # ── RESEARCH ──────────────────────────────────────────────────────────────

    async def _research_all_games(self, games: list) -> list:
        """
        Research all games sequentially to stay within Anthropic rate limits.
        Web-search research uses ~3k tokens per call; 20 parallel = 429 errors.
        """
        researched = []
        for i, game in enumerate(games):
            try:
                research = await self._intel.research_game(game)
            except Exception as e:
                logger.error(f"[GEN] Research failed for {game['title']}: {e}")
                research = {"research_quality": 0, "error": str(e)}
            game["research"] = research
            researched.append(game)
            # Small delay between calls to avoid rate limits
            if i < len(games) - 1:
                await asyncio.sleep(2)

        return researched

    # ── EVALUATION ────────────────────────────────────────────────────────────

    async def _evaluate_all_picks(self, researched_games: list) -> list:
        """
        For each game, evaluate the available market picks.
        Returns flat list of evaluated candidates.
        """
        all_candidates = []

        for game in researched_games:
            research = game.get("research", {})
            if research.get("research_quality", 0) < 3:
                logger.debug(f"[GEN] Skipping {game['title']} — "
                            f"low research quality")
                continue

            for market in game.get("markets", []):
                # Build pick dict
                pick = {
                    "game": game["title"],
                    "sport": game["sport"],
                    "event_ticker": game["event_ticker"],
                    "market_type": market["market_type"],
                    "pick": market["subtitle"] or market["title"],
                    "kalshi_ticker": market["ticker"],
                    "current_odds": self._calc_odds(market["yes_ask"]),
                    "yes_ask": market["yes_ask"],
                    "yes_bid": market["yes_bid"],
                    "game_start": game.get("close_time"),
                    "research": research,
                }

                # Skip if no real price
                if market["yes_ask"] <= 0:
                    continue

                # Skip if odds below minimum (1.2x)
                if pick["current_odds"] < 1.20:
                    continue

                # Evaluate with AI — small delay between calls to respect rate limits
                for slip_type in ["DAILY", "ROLLOVER", "LOTTO"]:
                    evaluation = await self._intel.evaluate_pick(
                        pick, research, slip_type
                    )
                    pick[f"eval_{slip_type.lower()}"] = evaluation
                    await asyncio.sleep(1)

                all_candidates.append(pick)

        return all_candidates

    def _calc_odds(self, yes_ask: float) -> float:
        """Convert yes_ask price to decimal odds."""
        if yes_ask <= 0 or yes_ask >= 1:
            return 0
        return round(1 / yes_ask, 4)

    # ── DAILY SLIP ────────────────────────────────────────────────────────────

    async def _generate_daily_slip(self, candidates: list,
                                    balance: float) -> Optional[dict]:
        """Generate the daily picks slip."""
        # FIX 1: Lower threshold to 7.0 and log per-sport breakdown
        DAILY_THRESHOLD = 7.0  # was DAILY_MIN_CONFIDENCE (7.5)

        # Log per-sport breakdown before filtering
        by_sport: dict = {}
        for c in candidates:
            s = c.get("sport", "?")
            conf = c.get("eval_daily", {}).get("confidence", 0)
            include = c.get("eval_daily", {}).get("include_in_slip", False)
            by_sport.setdefault(s, {"total": 0, "conf_pass": 0, "include_pass": 0})
            by_sport[s]["total"] += 1
            if conf >= DAILY_THRESHOLD:
                by_sport[s]["conf_pass"] += 1
            if conf >= DAILY_THRESHOLD and include:
                by_sport[s]["include_pass"] += 1

        for sport, counts in by_sport.items():
            logger.info(
                f"[GEN] Daily candidates {sport}: "
                f"{counts['include_pass']}/{counts['total']} eligible "
                f"(conf≥{DAILY_THRESHOLD}: {counts['conf_pass']})"
            )

        eligible = [
            c for c in candidates
            if c.get("eval_daily", {}).get("confidence", 0) >= DAILY_THRESHOLD
            and c.get("eval_daily", {}).get("include_in_slip", False)
        ]

        if not eligible:
            logger.warning("[GEN] No eligible candidates for daily slip")
            return None

        logger.info(f"[GEN] Daily: {len(eligible)} eligible candidates")

        # FIX 2: Use yes_ask directly for individual_odds; skip zero-odds candidates
        daily_candidates = []
        for c in eligible:
            individual_odds = self._calc_odds(c["yes_ask"])
            if individual_odds <= 0:
                logger.debug(f"[GEN] Skipping {c['kalshi_ticker']} — zero odds")
                continue
            daily_candidates.append({
                "game": c["game"],
                "sport": c["sport"],
                "market_type": c["market_type"],
                "pick": c["eval_daily"].get("calibrated_pick", c["pick"]),
                "calibrated_line": c["eval_daily"].get("calibrated_line"),
                "original_line": c.get("pick"),
                "kalshi_ticker": c["kalshi_ticker"],
                "individual_odds": individual_odds,
                "confidence": c["eval_daily"]["confidence"],
                "ai_reasoning": c["eval_daily"].get("reasoning", ""),
            })

        if not daily_candidates:
            logger.warning("[GEN] All eligible candidates had zero odds — skipping")
            return None

        # Assemble slip
        slip_data = await self._intel.assemble_slip(
            candidates=daily_candidates,
            slip_type="DAILY",
            target_odds=self._config.DAILY_TARGET_COMBINED,
            min_legs=self._config.DAILY_MIN_LEGS,
            max_legs=self._config.DAILY_MAX_LEGS,
        )

        if not slip_data.get("selected_legs"):
            return None

        # Validate combo
        is_valid, reason = self._intel.validate_combo(
            slip_data["selected_legs"]
        )
        if not is_valid:
            logger.warning(f"[GEN] Daily slip invalid: {reason}")
            # Try to fix by removing problematic leg
            slip_data = await self._fix_invalid_combo(
                slip_data, reason, "DAILY", eligible
            )
            if not slip_data:
                return None

        # FIX 4: Enrich AI-returned selected_legs with game_start from candidates
        ticker_to_game_start = {
            c["kalshi_ticker"]: c.get("game_start", "") for c in eligible
        }
        for leg in slip_data.get("selected_legs", []):
            if not leg.get("game_start"):
                leg["game_start"] = ticker_to_game_start.get(
                    leg.get("kalshi_ticker", ""), ""
                )

        projected_finish = max(
            (l.get("game_start", "") for l in slip_data["selected_legs"]),
            default=""
        )

        # Calculate stake
        stake = min(
            balance * self._config.DAILY_STAKE_PCT,
            self._config.DAILY_HARD_CAP
        )
        stake = round(stake, 2)
        combined_odds = slip_data["combined_odds"]
        potential_payout = round(stake * combined_odds, 2)

        logger.info(
            f"[GEN] Saving daily slip | legs={len(slip_data['selected_legs'])} | "
            f"odds={combined_odds:.2f}x | conf={slip_data.get('overall_confidence', 0):.1f} | "
            f"stake=${stake:.2f} | finish={projected_finish}"
        )

        # Save to database
        slip_id = self._db.save_slip(
            slip={
                "slip_type": "DAILY",
                "sport_mix": ",".join(set(
                    l["sport"] for l in slip_data["selected_legs"]
                )),
                "combined_odds": combined_odds,
                "stake": stake,
                "potential_payout": potential_payout,
                "confidence": slip_data["overall_confidence"],
                "projected_finish": projected_finish,
            },
            legs=slip_data["selected_legs"],
        )

        return {
            "slip_id": slip_id,
            "combined_odds": combined_odds,
            "stake": stake,
            "potential_payout": potential_payout,
            "legs": slip_data["selected_legs"],
        }

    # ── ROLLOVER SLIP ─────────────────────────────────────────────────────────

    async def _generate_rollover_slip(self, candidates: list,
                                       balance: float,
                                       active_rollover: dict) -> Optional[dict]:
        """Generate today's rollover slip."""
        eligible = [
            c for c in candidates
            if c.get("eval_rollover", {}).get("confidence", 0)
            >= self._config.ROLLOVER_MIN_CONFIDENCE
            and c.get("eval_rollover", {}).get("include_in_slip", False)
        ]

        if not eligible:
            logger.warning("[GEN] No eligible candidates for rollover slip")
            return None

        slip_data = await self._intel.assemble_slip(
            candidates=[{
                "game": c["game"],
                "sport": c["sport"],
                "market_type": c["market_type"],
                "pick": c["eval_rollover"].get("calibrated_pick", c["pick"]),
                "calibrated_line": c["eval_rollover"].get("calibrated_line"),
                "original_line": c.get("pick"),
                "kalshi_ticker": c["kalshi_ticker"],
                "individual_odds": self._calc_odds(c["yes_ask"]),
                "confidence": c["eval_rollover"]["confidence"],
                "ai_reasoning": c["eval_rollover"].get("reasoning", ""),
            } for c in eligible],
            slip_type="ROLLOVER",
            target_odds=active_rollover["target_odds"],
            min_legs=self._config.ROLLOVER_MIN_LEGS,
            max_legs=self._config.ROLLOVER_MAX_LEGS,
        )

        if not slip_data.get("selected_legs"):
            return None

        # Use current compounded stake from rollover session
        stake = round(active_rollover["current_stake"], 2)
        combined_odds = slip_data["combined_odds"]
        potential_payout = round(stake * combined_odds, 2)

        slip_id = self._db.save_slip(
            slip={
                "slip_type": "ROLLOVER",
                "sport_mix": ",".join(set(
                    l["sport"] for l in slip_data["selected_legs"]
                )),
                "combined_odds": combined_odds,
                "stake": stake,
                "potential_payout": potential_payout,
                "confidence": slip_data["overall_confidence"],
                "rollover_id": active_rollover["id"],
                "rollover_day": active_rollover["current_day"],
            },
            legs=slip_data["selected_legs"],
        )

        return {
            "slip_id": slip_id,
            "combined_odds": combined_odds,
            "stake": stake,
            "potential_payout": potential_payout,
            "rollover_day": active_rollover["current_day"],
            "legs": slip_data["selected_legs"],
        }

    # ── LOTTO SLIP ────────────────────────────────────────────────────────────

    async def _generate_lotto_slip(self, candidates: list,
                                    balance: float) -> Optional[dict]:
        """Generate the weekly lotto slip."""
        # More permissive filtering for lotto
        eligible = [
            c for c in candidates
            if c.get("eval_lotto", {}).get("confidence", 0)
            >= self._config.LOTTO_MIN_CONFIDENCE
        ]

        if len(eligible) < self._config.LOTTO_MIN_LEGS:
            logger.warning(f"[GEN] Not enough candidates for lotto slip "
                          f"({len(eligible)} < {self._config.LOTTO_MIN_LEGS})")
            return None

        slip_data = await self._intel.assemble_slip(
            candidates=[{
                "game": c["game"],
                "sport": c["sport"],
                "market_type": c["market_type"],
                "pick": c["eval_lotto"].get("calibrated_pick", c["pick"]),
                "kalshi_ticker": c["kalshi_ticker"],
                "individual_odds": self._calc_odds(c["yes_ask"]),
                "confidence": c["eval_lotto"]["confidence"],
                "ai_reasoning": c["eval_lotto"].get("reasoning", ""),
            } for c in eligible],
            slip_type="LOTTO",
            target_odds=self._config.LOTTO_MIN_ODDS,
            min_legs=self._config.LOTTO_MIN_LEGS,
            max_legs=self._config.LOTTO_MAX_LEGS,
        )

        if not slip_data.get("selected_legs"):
            return None

        combined_odds = slip_data["combined_odds"]
        if (combined_odds < self._config.LOTTO_MIN_ODDS or
                combined_odds > self._config.LOTTO_MAX_ODDS):
            logger.warning(f"[GEN] Lotto odds {combined_odds}x outside "
                          f"target range {self._config.LOTTO_MIN_ODDS}-"
                          f"{self._config.LOTTO_MAX_ODDS}")
            return None

        stake = min(
            balance * self._config.LOTTO_STAKE_PCT,
            self._config.LOTTO_HARD_CAP
        )
        stake = round(max(1.0, stake), 2)
        potential_payout = round(stake * combined_odds, 2)

        # Record lotto generation date
        self._db.set_state("last_lotto_date",
                           datetime.now(timezone.utc).date().isoformat())

        slip_id = self._db.save_slip(
            slip={
                "slip_type": "LOTTO",
                "sport_mix": ",".join(set(
                    l["sport"] for l in slip_data["selected_legs"]
                )),
                "combined_odds": combined_odds,
                "stake": stake,
                "potential_payout": potential_payout,
                "confidence": slip_data["overall_confidence"],
            },
            legs=slip_data["selected_legs"],
        )

        return {
            "slip_id": slip_id,
            "combined_odds": combined_odds,
            "stake": stake,
            "potential_payout": potential_payout,
            "legs": slip_data["selected_legs"],
        }

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _should_generate_lotto(self) -> bool:
        """Check if it's time to generate a new lotto slip (weekly)."""
        last_date_str = self._db.get_state("last_lotto_date")
        if not last_date_str:
            return True
        try:
            last_date = datetime.fromisoformat(last_date_str).date()
            days_since = (datetime.now(timezone.utc).date() - last_date).days
            return days_since >= self._config.LOTTO_FREQUENCY_DAYS
        except Exception:
            return True

    async def _fix_invalid_combo(self, slip_data: dict, reason: str,
                                   slip_type: str, eligible: list):
        """
        When a combo is invalid, try to fix it by removing problematic legs.
        """
        logger.info(f"[GEN] Attempting to fix invalid combo: {reason}")
        legs = slip_data.get("selected_legs", [])

        if len(legs) <= 2:
            logger.warning("[GEN] Cannot fix — too few legs")
            return None

        # Remove the last leg and try validation again
        fixed_legs = legs[:-1]
        is_valid, new_reason = self._intel.validate_combo(fixed_legs)
        if is_valid:
            slip_data["selected_legs"] = fixed_legs
            # Recalculate combined odds
            combined = 1.0
            for leg in fixed_legs:
                combined *= leg.get("individual_odds", 1.0)
            slip_data["combined_odds"] = round(combined, 4)
            logger.info(f"[GEN] Fixed combo by removing last leg — "
                       f"now {len(fixed_legs)} legs")
            return slip_data

        logger.warning(f"[GEN] Could not fix combo: {new_reason}")
        return None

    # ── RESULT MONITORING ─────────────────────────────────────────────────────

    async def check_pending_slips(self):
        """
        Check all pending slips and resolve any that have finished.
        Should be called every few minutes.
        """
        open_slips = self._db.get_open_slips()
        if not open_slips:
            return

        for slip in open_slips:
            slip_id = slip["id"]
            legs = self._db.get_slip_legs(slip_id)

            all_resolved = True
            all_won = True

            for leg in legs:
                ticker = leg.get("kalshi_ticker")
                if not ticker or leg.get("status") != "PENDING":
                    if leg.get("status") == "PENDING":
                        all_resolved = False
                    continue

                # Check market status
                status = await self._kalshi.get_market_status(ticker)
                if not status.get("resolved"):
                    all_resolved = False
                    continue

                result = status.get("result", "").lower()
                # Determine if this leg won
                # YES side: result="yes" → YES won
                # If we bet YES and result is yes → WIN
                if "yes" in ticker.lower() or "_yes" in ticker.lower():
                    leg_won = result == "yes"
                else:
                    # NO side bet (we bought NO)
                    leg_won = result == "no"

                leg_status = "WON" if leg_won else "LOST"
                self._db.resolve_leg(leg["id"], leg_status)

                if not leg_won:
                    all_won = False

            if all_resolved:
                # Resolve the slip
                stake = slip["stake"]
                combined_odds = slip["combined_odds"]

                if all_won:
                    net_pnl = round(stake * combined_odds - stake, 2)
                    self._db.resolve_slip(slip_id, "WON", net_pnl)
                    new_balance = self._db.get_balance() + net_pnl + stake
                    self._db.set_balance(round(new_balance, 2))

                    logger.info(f"[SLIP] #{slip_id} WON | "
                               f"pnl=+${net_pnl} | "
                               f"new_balance=${new_balance:.2f}")

                    # Handle rollover advancement
                    if slip.get("rollover_id"):
                        payout = stake * combined_odds
                        self._db.advance_rollover(
                            slip["rollover_id"],
                            won=True,
                            payout=round(payout, 2),
                            next_stake=round(payout, 2),
                        )
                else:
                    net_pnl = -stake
                    self._db.resolve_slip(slip_id, "LOST", net_pnl)
                    new_balance = self._db.get_balance() + net_pnl
                    self._db.set_balance(round(new_balance, 2))

                    logger.info(f"[SLIP] #{slip_id} LOST | "
                               f"pnl=-${stake} | "
                               f"new_balance=${new_balance:.2f}")

                    # Fail rollover
                    if slip.get("rollover_id"):
                        self._db.advance_rollover(
                            slip["rollover_id"],
                            won=False,
                            payout=0,
                            next_stake=0,
                        )
