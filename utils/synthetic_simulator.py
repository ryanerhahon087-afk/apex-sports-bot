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

        # Actual scores — realistic margin-based split
        # Winner gets ~50-65% of the total, loser gets the rest
        margin = random.uniform(3, 22)   # realistic NBA winning margin
        if home_wins:
            home_score = round((true_total + margin) / 2)
            away_score = true_total - home_score
        else:
            away_score = round((true_total + margin) / 2)
            home_score = true_total - away_score

        home_score   = max(88, int(home_score))
        away_score   = max(88, int(away_score))
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

        # MLB scenario modifier is NBA-scale; scale it down to MLB context
        mlb_mod = scenario["total_modifier"] * 0.1

        expected_home = (home["avg_runs_scored"] * 0.5 +
                         away["avg_runs_allowed"] * 0.5 +
                         home["home_boost"] +
                         mlb_mod)
        expected_away = (away["avg_runs_scored"] * 0.5 +
                         home["avg_runs_allowed"] * 0.5 +
                         mlb_mod)

        expected_home = max(1.0, expected_home)
        expected_away = max(1.0, expected_away)

        home_runs    = self._poisson(expected_home)
        away_runs    = self._poisson(expected_away)
        actual_total = home_runs + away_runs

        expected_total = expected_home + expected_away
        # Cap market line at 10.5 — realistic MLB total ceiling
        market_line = min(10.5, round(expected_total * 2) / 2)

        # Lines capped at 9.5 to stay in realistic MLB range
        lines = [
            min(9.5, market_line - 1.5),
            min(9.5, market_line - 0.5),
            min(9.5, market_line + 0.5),
            min(9.5, market_line + 1.5),
        ]

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
        rollover_target    = 1.8   # 2-leg parlay @ ~1.4x each = ~2.0x easily achieved
        rollover_days_won  = 0
        rollover_completed = False
        rollover_payout    = 0.0

        logger.info(
            f"[SIM] Starting {days}-day synthetic simulation | "
            f"balance=${balance:.2f} difficulty={difficulty}"
        )

        for day_num in range(1, days + 1):
            date_str = f"Simulation Day {day_num}"

            # Difficulty progression (only when caller chose "mixed")
            if difficulty == "mixed":
                if day_num <= 3:
                    day_difficulty = "easy"
                elif day_num <= 6:
                    day_difficulty = "hard"
                elif day_num <= 9:
                    day_difficulty = "medium"
                else:
                    day_difficulty = "medium"   # Day 10 recovery test
            else:
                day_difficulty = difficulty

            logger.info(
                f"[SIM] ── Day {day_num}/{days} | "
                f"difficulty={day_difficulty} | balance=${balance:.2f}"
            )

            # 1. Generate games (AI will NOT see _true_result)
            games     = self._generator.generate_day(
                date_str, n_nba=3, n_mlb=2, difficulty=day_difficulty
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

                if ro and ro.get("combined_odds", 0) >= 1.5:
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
                "difficulty":        day_difficulty,
                "balance":           balance,
                "daily":             daily_result,
                "rollover":          rollover_result,
                "rollover_day":      rollover_day,
                "rollover_stake":    round(rollover_stake, 2),
                "rollover_active":   rollover_active,
                "rollover_completed": rollover_completed,
                "games_generated":   len(games),
                "candidates_found":  len(candidates),
                "game_summaries":    self._summarise_games(games),
                "scenarios":         [
                    g.get("scenario_description", "")
                    for g in games_for_ai
                ],
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
        """
        Build synthetic research from generator stats — no web search needed.
        Synthetic games don't exist on the internet so we skip the AI call and
        instead construct the research dict from the game generator's team data.
        """
        for game in games_for_ai:
            sport  = game.get("sport", "")
            title  = game.get("title", "")
            scenario = game.get("scenario_description", "Normal game conditions")
            logger.info(f"[SIM] Building synthetic research: {title}")

            if sport == "NBA":
                home_avg  = game.get("home_avg_points",  113.0)
                away_avg  = game.get("away_avg_points",  113.0)
                home_def  = game.get("home_avg_allowed", 111.0)
                away_def  = game.get("away_avg_allowed", 111.0)
                home_pace = game.get("home_pace", 97.0)
                away_pace = game.get("away_pace", 97.0)
                exp_total = (home_avg + away_avg + home_def + away_def) / 2
                research = {
                    "game":           title,
                    "research_quality": 7.5,
                    "injury_impact":  "low",
                    "injuries":       [],
                    "team1_form": (
                        f"{game.get('home_team','Home')} avg {home_avg:.1f} pts/g "
                        f"(def {home_def:.1f}), pace {home_pace:.1f}"
                    ),
                    "team2_form": (
                        f"{game.get('away_team','Away')} avg {away_avg:.1f} pts/g "
                        f"(def {away_def:.1f}), pace {away_pace:.1f}"
                    ),
                    "h2h_summary": f"Scenario: {scenario}",
                    "situational_notes": [scenario],
                    "expert_consensus": {
                        "game_winner":    "unclear",
                        "total_line":     round(exp_total),
                        "total_direction": "OVER" if exp_total > 218 else "UNDER",
                    },
                    "key_factors": [
                        f"Expected combined total ~{exp_total:.0f} pts",
                        f"Home pace {home_pace:.1f}, Away pace {away_pace:.1f}",
                        scenario,
                    ],
                    "risks": ["Pace variance", "Score distribution uncertainty"],
                }
            elif sport == "MLB":
                home_scored  = game.get("home_avg_runs_scored",  4.5)
                away_scored  = game.get("away_avg_runs_scored",  4.2)
                home_allowed = game.get("home_avg_runs_allowed", 4.0)
                away_allowed = game.get("away_avg_runs_allowed", 3.8)
                exp_total = home_scored * 0.5 + away_allowed * 0.5 + \
                            away_scored * 0.5 + home_allowed * 0.5
                research = {
                    "game":           title,
                    "research_quality": 7.5,
                    "injury_impact":  "low",
                    "injuries":       [],
                    "team1_form": (
                        f"{game.get('home_team','Home')} avg {home_scored:.1f} R/g "
                        f"(allows {home_allowed:.1f})"
                    ),
                    "team2_form": (
                        f"{game.get('away_team','Away')} avg {away_scored:.1f} R/g "
                        f"(allows {away_allowed:.1f})"
                    ),
                    "h2h_summary": f"Scenario: {scenario}",
                    "situational_notes": [scenario],
                    "expert_consensus": {
                        "game_winner":    "unclear",
                        "total_line":     round(exp_total, 1),
                        "total_direction": "OVER" if exp_total > 8.5 else "UNDER",
                    },
                    "key_factors": [
                        f"Expected combined total ~{exp_total:.1f} runs",
                        scenario,
                        "Standard pitching conditions assumed",
                    ],
                    "risks": ["Pitcher variance", "Weather conditions"],
                }
            else:
                research = {
                    "game": title,
                    "research_quality": 5.0,
                }

            game["research"] = research

        return games_for_ai

    async def _evaluate_picks(self, researched: list) -> list:
        """
        Select candidates using market probability only — no API calls.
        yes_ask is the synthetic market's estimate of YES winning (true_prob ± noise).
        We select markets with yes_ask >= 0.55 (55%+ edge) and scale confidence
        from 6.0 (at 0.55) up to 9.5 (at 0.90).
        One candidate per game (highest yes_ask) to keep legs diversified.
        """
        # Threshold: pick markets where the synthetic line gives 55%+ true probability
        MIN_YES_ASK   = 0.55   # ~1.82x odds — genuine edge
        MAX_YES_ASK   = 0.90   # avoid near-certainties that look synthetic
        LEGS_PER_GAME = 2      # up to 2 picks per game (best by yes_ask)

        candidates = []
        for game in researched:
            game_picks = []
            for market in game.get("markets", []):
                yes_ask = float(market.get("yes_ask_dollars") or 0)
                if yes_ask < MIN_YES_ASK or yes_ask > MAX_YES_ASK:
                    continue

                # Confidence: linear map from 0.55→6.0 to 0.90→9.5
                conf = 6.0 + (yes_ask - 0.55) / (0.90 - 0.55) * (9.5 - 6.0)
                conf = round(conf, 1)
                indiv_odds = round(1.0 / yes_ask, 4)

                game_picks.append({
                    "game":            game["title"],
                    "sport":           game["sport"],
                    "event_ticker":    game["event_ticker"],
                    "market_type":     market["market_type"],
                    "pick":            market["yes_sub_title"],
                    "calibrated_pick": market["yes_sub_title"],
                    "kalshi_ticker":   market["ticker"],
                    "yes_ask":         yes_ask,
                    "individual_odds": indiv_odds,
                    "confidence":      conf,
                    "game_start":      game.get("game_date", ""),
                    "is_synthetic":    True,
                })
                logger.info(
                    f"[SIM] Candidate: {market['yes_sub_title'][:40]} "
                    f"yes_ask={yes_ask:.2f} conf={conf}"
                )

            # Take top LEGS_PER_GAME by yes_ask
            game_picks.sort(key=lambda x: x["yes_ask"], reverse=True)
            candidates.extend(game_picks[:LEGS_PER_GAME])

        return candidates

    def _select_legs(
        self,
        candidates: list,
        min_legs:   int,
        max_legs:   int,
        target_odds: float,
    ) -> list:
        """
        Greedily pick up to max_legs candidates (highest confidence first)
        whose combined odds are at least target_odds.
        Returns the selected leg list, or [] if min_legs can't be met.
        """
        sorted_c = sorted(candidates, key=lambda x: x["confidence"], reverse=True)
        selected = []
        combined = 1.0
        for c in sorted_c:
            if len(selected) >= max_legs:
                break
            selected.append(c)
            combined *= c["individual_odds"]
            if len(selected) >= min_legs and combined >= target_odds:
                break

        if len(selected) < min_legs:
            return []

        return selected

    async def _build_slip(
        self,
        candidates:   list,
        true_results: dict,
        balance:      float,
        slip_type:    str,
        target_odds:  float,
        min_legs:     int,
        max_legs:     int,
        stake_pct:    float = 0.10,
        fixed_stake:  float = None,
    ) -> dict | None:
        """
        Build a slip by direct leg selection (no AI assemble call).
        Scores each leg against the pre-revealed true results.
        """
        legs_chosen = self._select_legs(candidates, min_legs, max_legs, target_odds)
        if not legs_chosen:
            logger.info(
                f"[SIM] {slip_type}: not enough legs "
                f"({len(candidates)} candidates, need ≥{min_legs})"
            )
            return None

        combined_odds = 1.0
        for leg in legs_chosen:
            combined_odds *= leg["individual_odds"]
        combined_odds = round(combined_odds, 4)

        stake = (
            fixed_stake if fixed_stake is not None
            else round(min(balance * stake_pct, 10_000.0), 2)
        )

        legs_out = []
        all_won  = True
        for leg in legs_chosen:
            ticker = leg.get("kalshi_ticker", "")
            won    = bool(true_results.get(ticker, False))
            if not won:
                all_won = False
            legs_out.append({
                "game":       leg["game"],
                "pick":       leg["calibrated_pick"],
                "ticker":     ticker,
                "odds":       round(leg["individual_odds"], 3),
                "confidence": leg["confidence"],
                "won":        won,
                "result":     "WON" if won else "LOST",
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
            "won":              all_won,
            "result":           "WON" if all_won else "LOST",
            "slip_type":        slip_type,
            "legs":             legs_out,
            "leg_count":        len(legs_out),
            "combined_odds":    combined_odds,
            "stake":            stake,
            "pnl":              pnl,
            "potential_payout": round(stake * combined_odds, 2),
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
