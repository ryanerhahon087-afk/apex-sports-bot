"""
APEX/SPORTS BOT — Synthetic Game Simulator
Generates realistic fictional games, runs AI picks against them,
and reveals results only after picks are locked.
"""
import asyncio
import logging
import math
import random

logger = logging.getLogger(__name__)


# ── PART 1: GAME GENERATOR ───────────────────────────────────────────────────

class SyntheticGameGenerator:

    NBA_TEAMS = [
        {"name": "Los Angeles Lakers",      "abbr": "LAL",
         "avg_points": 113.2, "avg_allowed": 114.8,
         "pace": 98.5,  "home_boost": 3.2},
        {"name": "Boston Celtics",          "abbr": "BOS",
         "avg_points": 117.8, "avg_allowed": 108.2,
         "pace": 97.2,  "home_boost": 3.8},
        {"name": "Oklahoma City Thunder",   "abbr": "OKC",
         "avg_points": 118.5, "avg_allowed": 109.3,
         "pace": 100.2, "home_boost": 3.5},
        {"name": "Cleveland Cavaliers",     "abbr": "CLE",
         "avg_points": 114.7, "avg_allowed": 107.8,
         "pace": 96.8,  "home_boost": 3.1},
        {"name": "Denver Nuggets",          "abbr": "DEN",
         "avg_points": 116.2, "avg_allowed": 111.5,
         "pace": 98.1,  "home_boost": 4.2},
        {"name": "Minnesota Timberwolves",  "abbr": "MIN",
         "avg_points": 112.3, "avg_allowed": 106.9,
         "pace": 97.5,  "home_boost": 2.9},
        {"name": "Golden State Warriors",   "abbr": "GSW",
         "avg_points": 115.6, "avg_allowed": 113.2,
         "pace": 100.8, "home_boost": 3.6},
        {"name": "Miami Heat",              "abbr": "MIA",
         "avg_points": 110.8, "avg_allowed": 108.9,
         "pace": 95.3,  "home_boost": 3.3},
        {"name": "New York Knicks",         "abbr": "NYK",
         "avg_points": 113.5, "avg_allowed": 109.7,
         "pace": 96.1,  "home_boost": 4.5},
        {"name": "Philadelphia 76ers",      "abbr": "PHI",
         "avg_points": 111.2, "avg_allowed": 110.8,
         "pace": 95.8,  "home_boost": 3.0},
    ]

    MLB_TEAMS = [
        {"name": "New York Yankees",     "abbr": "NYY",
         "avg_runs_scored": 4.8, "avg_runs_allowed": 3.9, "home_boost": 0.3},
        {"name": "Los Angeles Dodgers", "abbr": "LAD",
         "avg_runs_scored": 5.1, "avg_runs_allowed": 3.7, "home_boost": 0.4},
        {"name": "Houston Astros",      "abbr": "HOU",
         "avg_runs_scored": 4.5, "avg_runs_allowed": 3.8, "home_boost": 0.3},
        {"name": "Atlanta Braves",      "abbr": "ATL",
         "avg_runs_scored": 5.3, "avg_runs_allowed": 4.1, "home_boost": 0.4},
        {"name": "Baltimore Orioles",   "abbr": "BAL",
         "avg_runs_scored": 4.7, "avg_runs_allowed": 4.2, "home_boost": 0.3},
        {"name": "Tampa Bay Rays",      "abbr": "TB",
         "avg_runs_scored": 4.2, "avg_runs_allowed": 3.6, "home_boost": 0.2},
        {"name": "Seattle Mariners",    "abbr": "SEA",
         "avg_runs_scored": 4.0, "avg_runs_allowed": 3.5, "home_boost": 0.3},
        {"name": "San Diego Padres",    "abbr": "SD",
         "avg_runs_scored": 4.4, "avg_runs_allowed": 3.9, "home_boost": 0.3},
    ]

    # ── Public API ─────────────────────────────────────────────────────────────

    def generate_game(self, sport: str, difficulty: str = "mixed",
                      game_date: str = None) -> dict:
        """
        Generate a single realistic fictional game.
        Returns game data WITHOUT the true result (stored under _true_result).
        """
        if sport == "NBA":
            return self._generate_nba_game(difficulty, game_date)
        elif sport == "MLB":
            return self._generate_mlb_game(difficulty, game_date)
        raise ValueError(f"Unknown sport: {sport}")

    def generate_day(self, date_str: str, n_nba: int = 3,
                     n_mlb: int = 2, difficulty: str = "mixed") -> list:
        """Generate a full day of games."""
        games = []
        for _ in range(n_nba):
            games.append(self.generate_game("NBA", difficulty, date_str))
        for _ in range(n_mlb):
            games.append(self.generate_game("MLB", difficulty, date_str))
        return games

    def reveal_results(self, games: list) -> dict:
        """
        After picks are locked, reveal actual results.
        Returns dict of ticker -> True (YES won) / False (NO won).
        """
        results = {}
        for game in games:
            true_result = game.get("_true_result", {})
            results.update(true_result.get("over_results", {}))
            event_ticker = game.get("event_ticker", "")
            if event_ticker:
                home_wins = true_result.get("home_wins", False)
                results[f"{event_ticker}-HOME"] = home_wins
                results[f"{event_ticker}-AWAY"] = not home_wins
        return results

    # ── NBA ────────────────────────────────────────────────────────────────────

    def _generate_nba_game(self, difficulty: str, game_date: str) -> dict:
        teams = random.sample(self.NBA_TEAMS, 2)
        home, away = teams[0], teams[1]

        pace_factor = (home["pace"] + away["pace"]) / 2 / 100
        base_total = (
            (home["avg_points"] + away["avg_points"] +
             home["avg_allowed"] + away["avg_allowed"]) / 2
        ) * pace_factor
        base_total += home["home_boost"] * 0.5

        scenario = self._generate_scenario(difficulty)
        true_total = base_total + scenario["total_modifier"]
        true_total += random.gauss(0, 8)
        true_total = round(true_total)

        # Market line centred on true total with some noise
        market_line = true_total + random.gauss(0, 3)
        market_line = round(market_line / 0.5) * 0.5

        lines = sorted({
            market_line - 4, market_line - 2, market_line,
            market_line + 2, market_line + 4,
        })

        line_markets = []
        for line in lines:
            z = (line - true_total) / 12
            true_prob_over = 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))
            market_prob = true_prob_over + random.uniform(-0.05, 0.05)
            market_prob = max(0.05, min(0.95, market_prob))
            yes_ask = round(market_prob, 2)
            yes_bid = round(market_prob - random.uniform(0.01, 0.03), 2)
            line_markets.append({
                "ticker":          f"KXNBATOTAL-SIM-{home['abbr']}{away['abbr']}-{int(line * 2)}",
                "title":           f"{away['name']} at {home['name']}: Total Points",
                "yes_sub_title":   f"Over {line} points scored",
                "yes_ask_dollars": str(yes_ask),
                "yes_bid_dollars": str(yes_bid),
                "market_type":     "TOTAL",
                "sport":           "NBA",
            })

        # Winner market
        home_edge = (
            (home["avg_points"] - home["avg_allowed"]) -
            (away["avg_points"] - away["avg_allowed"])
        ) / 40 + 0.05
        home_win_prob = max(0.2, min(0.8,
            0.5 + home_edge + scenario["home_win_modifier"]))
        home_wins = random.random() < home_win_prob

        # Actual scores
        if home_wins:
            home_score = int(true_total * (home_win_prob + 0.1))
            away_score = true_total - home_score
        else:
            away_score = int(true_total * (1 - home_win_prob + 0.1))
            home_score = true_total - away_score

        home_score  = max(85, int(home_score))
        away_score  = max(85, int(away_score))
        actual_total = home_score + away_score

        winner_prob_ask = round(home_win_prob + random.uniform(-0.04, 0.04), 2)
        winner_markets = [
            {
                "ticker":          f"KXNBAGAME-SIM-{home['abbr']}{away['abbr']}-HOME",
                "title":           f"{away['name']} at {home['name']} Winner",
                "yes_sub_title":   home["name"],
                "yes_ask_dollars": str(max(0.1, min(0.9, winner_prob_ask))),
                "yes_bid_dollars": str(max(0.08, min(0.88, winner_prob_ask - 0.02))),
                "market_type":     "GAME_WINNER",
                "sport":           "NBA",
            },
            {
                "ticker":          f"KXNBAGAME-SIM-{home['abbr']}{away['abbr']}-AWAY",
                "title":           f"{away['name']} at {home['name']} Winner",
                "yes_sub_title":   away["name"],
                "yes_ask_dollars": str(max(0.1, min(0.9, round(1 - winner_prob_ask, 2)))),
                "yes_bid_dollars": str(max(0.08, min(0.88, round(1 - winner_prob_ask - 0.02, 2)))),
                "market_type":     "GAME_WINNER",
                "sport":           "NBA",
            },
        ]

        over_results = {
            line_markets[i]["ticker"]: actual_total > lines[i]
            for i in range(len(lines))
        }
        over_results[f"KXNBAGAME-SIM-{home['abbr']}{away['abbr']}-HOME"] = home_wins
        over_results[f"KXNBAGAME-SIM-{home['abbr']}{away['abbr']}-AWAY"] = not home_wins

        return {
            "event_ticker":        f"KXNBA-SIM-{home['abbr']}{away['abbr']}",
            "title":               f"{away['name']} at {home['name']}",
            "sport":               "NBA",
            "game_date":           game_date,
            "home_team":           home["name"],
            "away_team":           away["name"],
            "home_avg_points":     home["avg_points"],
            "away_avg_points":     away["avg_points"],
            "home_avg_allowed":    home["avg_allowed"],
            "away_avg_allowed":    away["avg_allowed"],
            "home_pace":           home["pace"],
            "away_pace":           away["pace"],
            "scenario_description": scenario["description"],
            "markets":             line_markets + winner_markets,
            "is_synthetic":        True,
            "_true_result": {
                "home_score":   home_score,
                "away_score":   away_score,
                "actual_total": actual_total,
                "home_wins":    home_wins,
                "over_results": over_results,
            },
        }

    # ── MLB ────────────────────────────────────────────────────────────────────

    def _generate_mlb_game(self, difficulty: str, game_date: str) -> dict:
        teams = random.sample(self.MLB_TEAMS, 2)
        home, away = teams[0], teams[1]

        scenario = self._generate_scenario(difficulty)

        expected_home = (home["avg_runs_scored"] * 0.5 +
                         away["avg_runs_allowed"] * 0.5 +
                         home["home_boost"] +
                         scenario["total_modifier"] * 0.3)
        expected_away = (away["avg_runs_scored"] * 0.5 +
                         home["avg_runs_allowed"] * 0.5 +
                         scenario["total_modifier"] * 0.3)

        expected_home = max(1.0, expected_home)
        expected_away = max(1.0, expected_away)

        home_runs   = self._poisson(expected_home)
        away_runs   = self._poisson(expected_away)
        actual_total = home_runs + away_runs

        expected_total = expected_home + expected_away
        market_line = round(expected_total * 2) / 2

        lines = [market_line - 1.5, market_line - 0.5,
                 market_line + 0.5, market_line + 1.5]

        line_markets = []
        for line in lines:
            z = (line - expected_total) / 2.5
            true_prob_over = 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))
            market_prob = max(0.05, min(0.95,
                true_prob_over + random.uniform(-0.04, 0.04)))
            line_markets.append({
                "ticker":          f"KXMLBTOTAL-SIM-{home['abbr']}{away['abbr']}-{int(line * 2)}",
                "title":           f"{away['name']} vs {home['name']} Total Runs",
                "yes_sub_title":   f"Over {line} runs scored",
                "yes_ask_dollars": str(round(market_prob, 2)),
                "yes_bid_dollars": str(round(market_prob - 0.02, 2)),
                "market_type":     "TOTAL",
                "sport":           "MLB",
            })

        return {
            "event_ticker":         f"KXMLB-SIM-{home['abbr']}{away['abbr']}",
            "title":                f"{away['name']} at {home['name']}",
            "sport":                "MLB",
            "game_date":            game_date,
            "home_team":            home["name"],
            "away_team":            away["name"],
            "home_avg_runs":        home["avg_runs_scored"],
            "away_avg_runs":        away["avg_runs_scored"],
            "scenario_description": scenario["description"],
            "markets":              line_markets,
            "is_synthetic":         True,
            "_true_result": {
                "home_runs":    home_runs,
                "away_runs":    away_runs,
                "actual_total": actual_total,
                "home_wins":    home_runs > away_runs,
                "over_results": {
                    m["ticker"]: actual_total > lines[i]
                    for i, m in enumerate(line_markets)
                },
            },
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _generate_scenario(self, difficulty: str) -> dict:
        scenarios = {
            "easy": [
                {"description": "Heavy favourite at home, opponent missing star player",
                 "total_modifier": -5,  "home_win_modifier":  0.20},
                {"description": "High-pace matchup, both teams top-10 offence",
                 "total_modifier":  8,  "home_win_modifier":  0.05},
                {"description": "Low defensive intensity, back-to-back for away team",
                 "total_modifier":  6,  "home_win_modifier":  0.15},
            ],
            "medium": [
                {"description": "Competitive matchup, both teams healthy",
                 "total_modifier":  0,  "home_win_modifier":  0.00},
                {"description": "Defensive game expected, playoff intensity",
                 "total_modifier": -3,  "home_win_modifier":  0.05},
                {"description": "Slight favourite at home, evenly matched",
                 "total_modifier":  2,  "home_win_modifier":  0.08},
            ],
            "hard": [
                {"description": "Coin-flip game, no clear edge",
                 "total_modifier":  0,  "home_win_modifier":  0.00},
                {"description": "Trap game — strong team in letdown spot",
                 "total_modifier": -8,  "home_win_modifier": -0.10},
                {"description": "High variance — both teams streaky",
                 "total_modifier": random.gauss(0, 10),
                 "home_win_modifier": random.uniform(-0.1, 0.1)},
            ],
        }

        if difficulty == "mixed":
            level = random.choices(
                ["easy", "medium", "hard"], weights=[0.4, 0.4, 0.2]
            )[0]
        else:
            level = difficulty

        return random.choice(scenarios[level])

    @staticmethod
    def _poisson(lam: float) -> int:
        """Pure-Python Poisson random variate (Knuth algorithm)."""
        L = math.exp(-lam)
        k, p = 0, 1.0
        while p > L:
            k += 1
            p *= random.random()
        return k - 1


# ── PART 2: SYNTHETIC SIMULATOR ───────────────────────────────────────────────

class SyntheticSimulator:
    """
    Runs a full N-day simulation using synthetic games.
    AI makes picks without seeing results; results revealed after picks locked.
    """

    def __init__(self, intelligence, database):
        self._intel     = intelligence
        self._db        = database
        self._generator = SyntheticGameGenerator()

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run_simulation(
        self,
        days:             int   = 10,
        starting_balance: float = 100.0,
        difficulty:       str   = "mixed",
    ) -> dict:
        """Run a full N-day simulation with daily + rollover slips."""
        balance = starting_balance
        results = []

        # Rollover state
        rollover_active    = False
        rollover_stake     = round(starting_balance * 0.10, 2)
        rollover_day       = 0
        rollover_target    = 3.0
        rollover_days_won  = 0
        rollover_completed = False
        rollover_payout    = 0.0

        logger.info(
            f"[SIM] Starting {days}-day synthetic simulation | "
            f"balance=${balance:.2f} difficulty={difficulty}"
        )

        for day_num in range(1, days + 1):
            date_str = f"Simulation Day {day_num}"
            logger.info(
                f"[SIM] ── Day {day_num}/{days} | balance=${balance:.2f}"
            )

            # 1. Generate games (AI will NOT see _true_result)
            games     = self._generator.generate_day(
                date_str, n_nba=3, n_mlb=2, difficulty=difficulty
            )
            games_for_ai = [
                {k: v for k, v in g.items() if not k.startswith("_")}
                for g in games
            ]

            # 2. Research all games
            researched = await self._research_games(games_for_ai)

            # 3. Evaluate picks → build candidate pool
            candidates = await self._evaluate_picks(researched)
            logger.info(
                f"[SIM] Day {day_num}: {len(candidates)} candidate(s) "
                f"from {len(games)} game(s)"
            )

            # Reveal true results once picks are about to be assembled
            # (Generator still holds _true_result on the original objects)
            true_results = self._generator.reveal_results(games)

            # 4. Daily slip
            daily_result = None
            if len(candidates) >= 2:
                daily_result = await self._build_slip(
                    candidates, true_results, balance,
                    slip_type="DAILY", target_odds=2.0,
                    min_legs=2, max_legs=4, stake_pct=0.10,
                )
                if daily_result:
                    balance = round(balance + daily_result["pnl"], 2)
                    daily_result["balance_after"] = balance

            # 5. Rollover slip (separate stake, compounding)
            rollover_result = None
            if not rollover_completed and candidates:
                if not rollover_active:
                    rollover_active = True
                    rollover_stake  = round(starting_balance * 0.10, 2)
                    rollover_day    = 1

                ro = await self._build_slip(
                    candidates, true_results, rollover_stake,
                    slip_type="ROLLOVER", target_odds=rollover_target,
                    min_legs=2, max_legs=3, fixed_stake=rollover_stake,
                )

                if ro and ro.get("combined_odds", 0) >= 2.5:
                    if ro["won"]:
                        ro_payout = round(rollover_stake * ro["combined_odds"], 2)
                        rollover_days_won += 1

                        if rollover_day >= 5:
                            rollover_completed = True
                            rollover_payout    = ro_payout
                            logger.info(
                                f"[SIM] 🏆 Rollover COMPLETED day {rollover_day} "
                                f"→ ${ro_payout:.2f}"
                            )
                        else:
                            rollover_stake = ro_payout
                            rollover_day  += 1

                        rollover_result = {
                            "result": "WON", "day": rollover_day,
                            "stake": rollover_stake, "payout": ro_payout,
                            "odds": ro["combined_odds"],
                            "legs": ro["legs"],
                        }
                    else:
                        logger.info(
                            f"[SIM] Rollover lost on day {rollover_day} — resetting"
                        )
                        rollover_result = {
                            "result": "LOST", "day": rollover_day,
                            "stake": rollover_stake,
                            "legs": ro["legs"],
                        }
                        rollover_active  = False
                        rollover_day     = 0
                        rollover_stake   = round(starting_balance * 0.10, 2)

            results.append({
                "day":               day_num,
                "date":              date_str,
                "balance":           balance,
                "daily":             daily_result,
                "rollover":          rollover_result,
                "games_generated":   len(games),
                "candidates_found":  len(candidates),
                "game_summaries":    self._summarise_games(games),
            })

        # Final summary
        daily_list  = [r["daily"] for r in results if r.get("daily")]
        daily_wins  = sum(1 for d in daily_list if d.get("won"))

        return {
            "simulation_type":   "SYNTHETIC",
            "days":              days,
            "difficulty":        difficulty,
            "starting_balance":  starting_balance,
            "ending_balance":    balance,
            "total_pnl":         round(balance - starting_balance, 2),
            "daily_slips_total": len(daily_list),
            "daily_slips_won":   daily_wins,
            "daily_win_rate":    (
                round(daily_wins / len(daily_list) * 100, 1)
                if daily_list else 0
            ),
            "rollover_completed":  rollover_completed,
            "rollover_days_won":   rollover_days_won,
            "rollover_payout":     rollover_payout,
            "daily_breakdown":     results,
        }

    # ── Pipeline helpers ───────────────────────────────────────────────────────

    async def _research_games(self, games_for_ai: list) -> list:
        researched = []
        for i, game in enumerate(games_for_ai):
            logger.info(f"[SIM] Researching: {game.get('title','?')}")
            try:
                research = await self._intel.research_game(game)
                game["research"] = research
            except Exception as e:
                logger.warning(f"[SIM] Research failed for {game.get('title')}: {e}")
                game["research"] = {"research_quality": 0}
            researched.append(game)
            if i < len(games_for_ai) - 1:
                await asyncio.sleep(8)   # keep well under 30k TPM
        return researched

    async def _evaluate_picks(self, researched: list) -> list:
        candidates = []
        for game in researched:
            research = game.get("research", {})
            if not research.get("research_quality", 0):
                continue

            for market in game.get("markets", []):
                yes_ask = float(market.get("yes_ask_dollars") or 0)
                if yes_ask <= 0.05 or yes_ask >= 0.95:
                    continue

                pick = {
                    "game":          game["title"],
                    "sport":         game["sport"],
                    "event_ticker":  game["event_ticker"],
                    "market_type":   market["market_type"],
                    "pick":          market["yes_sub_title"],
                    "kalshi_ticker": market["ticker"],
                    "current_odds":  round(1 / yes_ask, 4),
                    "yes_ask":       yes_ask,
                    "game_start":    game.get("game_date", ""),
                    "is_synthetic":  True,
                }

                try:
                    ev = await self._intel.evaluate_pick(pick, research, "DAILY")
                except Exception as e:
                    logger.warning(f"[SIM] Eval failed for {pick['pick']}: {e}")
                    ev = {"include_in_slip": False, "confidence": 0}

                conf    = float(ev.get("confidence") or 0)
                include = ev.get("include_in_slip", False)

                logger.info(
                    f"[SIM] {pick['pick'][:40]} → "
                    f"conf={conf} include={include}"
                )

                if include and conf >= 6.5:
                    pick["eval_daily"]    = ev
                    pick["confidence"]    = conf
                    pick["calibrated_pick"] = ev.get("calibrated_pick") or pick["pick"]
                    candidates.append(pick)

                await asyncio.sleep(4)

        return candidates

    async def _build_slip(
        self,
        candidates:  list,
        true_results: dict,
        balance:     float,
        slip_type:   str,
        target_odds: float,
        min_legs:    int,
        max_legs:    int,
        stake_pct:   float  = 0.10,
        fixed_stake: float  = None,
    ) -> dict | None:
        leg_dicts = [{
            "game":            c["game"],
            "sport":           c["sport"],
            "market_type":     c["market_type"],
            "pick":            c.get("calibrated_pick", c["pick"]),
            "kalshi_ticker":   c["kalshi_ticker"],
            "individual_odds": round(1 / c["yes_ask"], 4),
            "confidence":      c["confidence"],
            "ai_reasoning":    c.get("eval_daily", {}).get("reasoning", ""),
            "game_start":      c.get("game_start", ""),
        } for c in candidates]

        try:
            slip_data = await self._intel.assemble_slip(
                candidates=leg_dicts,
                slip_type=slip_type,
                target_odds=target_odds,
                min_legs=min_legs,
                max_legs=max_legs,
            )
        except Exception as e:
            logger.warning(f"[SIM] Assemble failed ({slip_type}): {e}")
            return None

        if not slip_data or not slip_data.get("selected_legs"):
            return None

        combined_odds = float(slip_data.get("combined_odds") or 0)
        if combined_odds <= 0:
            combined_odds = 1.0
            for leg in slip_data["selected_legs"]:
                lo = float(leg.get("individual_odds") or 0)
                if lo > 0:
                    combined_odds *= lo
            combined_odds = round(combined_odds, 4)

        stake = (
            fixed_stake if fixed_stake is not None
            else round(min(balance * stake_pct, 10_000.0), 2)
        )

        # Score each leg
        legs_out = []
        all_won  = True
        for leg in slip_data["selected_legs"]:
            ticker = leg.get("kalshi_ticker") or leg.get("ticker", "")
            won    = bool(true_results.get(ticker, False))
            if not won:
                all_won = False
            legs_out.append({
                "game":          leg.get("game", ""),
                "pick":          leg.get("pick", ""),
                "ticker":        ticker,
                "odds":          round(float(leg.get("individual_odds") or 0), 3),
                "confidence":    round(float(leg.get("confidence") or 0), 1),
                "won":           won,
                "result":        "WON" if won else "LOST",
            })

        pnl = (
            round(stake * (combined_odds - 1), 2)
            if all_won
            else -stake
        )

        logger.info(
            f"[SIM] {slip_type} slip: {len(legs_out)} legs "
            f"@ {combined_odds:.2f}x stake=${stake:.2f} "
            f"→ {'WIN' if all_won else 'LOSS'} (P&L ${pnl:+.2f})"
        )

        return {
            "won":           all_won,
            "result":        "WON" if all_won else "LOST",
            "legs":          legs_out,
            "leg_count":     len(legs_out),
            "combined_odds": combined_odds,
            "stake":         stake,
            "pnl":           pnl,
            "potential_payout": round(stake * combined_odds, 2),
            "confidence":    slip_data.get("overall_confidence", 0),
        }

    # ── Utility ────────────────────────────────────────────────────────────────

    @staticmethod
    def _summarise_games(games: list) -> list:
        """Return a lightweight summary of each game's actual outcome."""
        out = []
        for g in games:
            tr = g.get("_true_result", {})
            sport = g.get("sport", "")
            if sport == "NBA":
                out.append({
                    "title":  g.get("title", ""),
                    "sport":  sport,
                    "score":  f"{tr.get('home_score','?')} – {tr.get('away_score','?')}",
                    "total":  tr.get("actual_total", "?"),
                    "winner": g.get("home_team") if tr.get("home_wins") else g.get("away_team"),
                })
            elif sport == "MLB":
                out.append({
                    "title":  g.get("title", ""),
                    "sport":  sport,
                    "score":  f"{tr.get('home_runs','?')} – {tr.get('away_runs','?')}",
                    "total":  tr.get("actual_total", "?"),
                    "winner": g.get("home_team") if tr.get("home_wins") else g.get("away_team"),
                })
        return out
