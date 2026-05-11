"""
APEX/SPORTS BOT — Intelligence Layer
The brain. Uses Claude Sonnet + web search to research, evaluate,
calibrate and score every potential pick.
"""
import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)


class SportsIntelligence:
    """
    Researches and evaluates sports picks using Claude Sonnet
    with web search for real-time data.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    # ── RESEARCH ──────────────────────────────────────────────────────────────

    async def research_game(self, game: dict) -> dict:
        """
        Deep research on a single game using web search.
        Returns structured research data.
        """
        game_title = game.get("title", "")
        sport = game.get("sport", "")
        game_start = game.get("game_start", "")

        logger.info(f"[INTEL] Researching: {game_title}")

        prompt = f"""You are a sharp sports analyst researching a game for prediction markets.

Game: {game_title}
Sport: {sport}
Start time: {game_start}
Available markets: {json.dumps(game.get('markets', []), indent=2)}

Use web search to research this game thoroughly. Find:

1. INJURY REPORT: Who is listed as out, doubtful, or questionable? 
   Check the most recent reports (last 24 hours).

2. RECENT FORM: How has each team/player performed in the last 5-10 games?
   Win/loss record, point totals, scoring trends.

3. HEAD TO HEAD: What happened in recent matchups between these teams?
   Scoring patterns, who typically wins.

4. SITUATIONAL FACTORS:
   - Is either team on a back-to-back (played yesterday)?
   - Home vs away advantage?
   - Any motivation factors (must-win, elimination game)?
   - Pace of play (affects totals)?

5. EXPERT CONSENSUS: What are sharp bettors and analysts predicting?
   Look for consensus lines on major sportsbooks.

After research, provide your analysis in this exact JSON format:
{{
    "game": "{game_title}",
    "research_quality": 8.5,  // 0-10, how much data you found
    "injury_impact": "high/medium/low/none",
    "injuries": ["Player X (OUT - ankle)", "Player Y (QUESTIONABLE - knee)"],
    "team1_form": "description of recent form",
    "team2_form": "description of recent form", 
    "h2h_summary": "head to head summary",
    "situational_notes": ["back-to-back", "elimination game", etc],
    "expert_consensus": {{
        "game_winner": "TEAM_NAME or unclear",
        "total_line": 220.5,  // consensus total if applicable
        "total_direction": "OVER or UNDER or unclear"
    }},
    "key_factors": ["most important factor 1", "factor 2", "factor 3"],
    "risks": ["main risk 1", "risk 2"]
}}

Return ONLY the JSON, no other text."""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )

            # Extract text from response
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text

            # Parse JSON
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"```json?\n?", "", text).rstrip("`").strip()

            research = json.loads(text)
            logger.info(f"[INTEL] Research complete: {game_title} | "
                       f"quality={research.get('research_quality', 0)}")
            return research

        except Exception as e:
            logger.error(f"[INTEL] Research failed for {game_title}: {e}")
            return {
                "game": game_title,
                "research_quality": 0,
                "error": str(e)
            }

    # ── PICK EVALUATION ───────────────────────────────────────────────────────

    async def evaluate_pick(self, pick: dict, research: dict,
                             slip_type: str) -> dict:
        """
        Evaluate a specific pick against research data.
        Returns confidence score, calibrated line, and reasoning.
        """
        prompt = f"""You are a sharp sports bettor evaluating a prediction market pick.

PICK TO EVALUATE:
Game: {pick.get('game')}
Market type: {pick.get('market_type')}
Original pick: {pick.get('pick')}
Current Kalshi odds: {pick.get('current_odds', 'unknown')}
Slip type: {slip_type}

RESEARCH DATA:
{json.dumps(research, indent=2)}

EVALUATION TASK:
1. What is the realistic probability this pick wins? (0-100%)
2. Is there any reason this pick might fail?
3. Should we calibrate (adjust) the line to a safer position?
4. What calibrated pick gives a better probability while keeping meaningful odds?

CALIBRATION RULES:
- For totals (OVER/UNDER points or runs):
  * Move the line {self._calibration_buffer(slip_type)} points in the safer direction
  * Example: OVER 220.5 consensus → play OVER 213.5 (safer buffer)
  * Example: UNDER 220.5 consensus → play UNDER 227.5 (safer buffer)
  * The more uncertain the game, the bigger the buffer
  * But don't dilute odds too much — find the sweet spot
- For game winners: only recommend if there's a clear favorite with injury/form edge
- For player props: ensure the player is confirmed playing

SLIP TYPE REQUIREMENTS:
- DAILY: Need confidence >= 7.5/10. High certainty picks only.
- ROLLOVER: Need confidence >= 7.0/10. Solid picks.
- LOTTO: Need confidence >= 5.0/10. Interesting picks, more speculative ok.

Respond in this exact JSON format:
{{
    "pick": "{pick.get('pick')}",
    "calibrated_pick": "adjusted safer version or same if no adjustment needed",
    "calibrated_line": null or number if applicable,
    "win_probability": 72,  // 0-100
    "confidence": 7.8,      // 0-10 for this pick
    "include_in_slip": true or false,
    "reasoning": "2-3 sentence explanation of why this pick is good or bad",
    "key_risk": "main reason this could fail",
    "calibration_explanation": "why you adjusted the line or why no adjustment needed"
}}

Return ONLY the JSON."""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r"```json?\n?", "", text).rstrip("`").strip()

            evaluation = json.loads(text)
            logger.info(f"[INTEL] Pick evaluated: {pick.get('pick')} | "
                       f"conf={evaluation.get('confidence')} | "
                       f"include={evaluation.get('include_in_slip')}")
            return evaluation

        except Exception as e:
            logger.error(f"[INTEL] Evaluation failed: {e}")
            return {
                "pick": pick.get("pick"),
                "confidence": 0,
                "include_in_slip": False,
                "reasoning": f"Evaluation error: {e}",
            }

    def _calibration_buffer(self, slip_type: str) -> str:
        """Return calibration instructions based on slip type."""
        if slip_type == "DAILY":
            return "5-8"
        elif slip_type == "ROLLOVER":
            return "4-6"
        else:  # LOTTO
            return "2-3"

    # ── SLIP ASSEMBLY ─────────────────────────────────────────────────────────

    async def assemble_slip(self, candidates: list, slip_type: str,
                             target_odds: float, min_legs: int,
                             max_legs: int) -> dict:
        """
        From a list of evaluated candidates, select the best combination
        to build a slip that meets target odds and confidence requirements.
        """
        prompt = f"""You are assembling a sports betting parlay slip.

SLIP TYPE: {slip_type}
TARGET COMBINED ODDS: {target_odds}x minimum
MINIMUM LEGS: {min_legs}
MAXIMUM LEGS: {max_legs}

AVAILABLE CANDIDATES (already researched and evaluated):
{json.dumps(candidates, indent=2)}

ASSEMBLY RULES:
1. INVALID COMBINATIONS (never combine these from the SAME game):
   - Spread + Game winner from the same game
   - Any two contradictory picks from the same game

2. VALID COMBINATIONS:
   - Game winner + Total from same game (allowed)
   - Any picks from different games (always allowed)
   - Different sports can be combined

3. SPORT PRIORITY ORDER: NBA first, then MLB, then Soccer
   Eliminate the least probable/most speculative when choosing

4. ODDS CALIBRATION:
   - Individual odds = 1 / (win_probability / 100)
   - Combined odds = multiply all individual odds
   - If combined odds exceed maximum, remove the least confident leg
   - If combined odds are below minimum, add more legs

5. FOR LOTTO SLIP: Target {target_odds}x-455x combined odds.
   More legs, more speculative picks allowed.

Select the optimal combination and return in this exact JSON format:
{{
    "selected_legs": [
        {{
            "game": "Team A vs Team B",
            "sport": "NBA",
            "market_type": "GAME_WINNER",
            "pick": "Team A",
            "calibrated_pick": "Team A",
            "original_line": null,
            "calibrated_line": null,
            "kalshi_ticker": "KXNBAGAME-26MAY11OKCLAL-OKC",
            "individual_odds": 1.25,
            "confidence": 8.2,
            "ai_reasoning": "OKC is heavily favored, LAL missing key players"
        }}
    ],
    "combined_odds": 3.24,
    "overall_confidence": 7.9,
    "slip_quality": "HIGH/MEDIUM/LOW",
    "assembly_notes": "explanation of why these legs were selected",
    "rejected_legs": ["leg A rejected because...", "leg B rejected because..."]
}}

Return ONLY the JSON."""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r"```json?\n?", "", text).rstrip("`").strip()

            slip = json.loads(text)
            logger.info(f"[INTEL] Slip assembled: {len(slip.get('selected_legs',[]))} legs | "
                       f"odds={slip.get('combined_odds')} | "
                       f"conf={slip.get('overall_confidence')}")
            return slip

        except Exception as e:
            logger.error(f"[INTEL] Slip assembly failed: {e}")
            return {"selected_legs": [], "combined_odds": 0, "error": str(e)}

    # ── COMBO VALIDATION ──────────────────────────────────────────────────────

    def validate_combo(self, legs: list) -> tuple[bool, str]:
        """
        Check if a slip combination is valid on Kalshi.
        Returns (is_valid, reason_if_invalid).
        """
        # Check for same-game spread + moneyline
        game_market_types = {}
        for leg in legs:
            game = leg.get("game", "")
            mtype = leg.get("market_type", "")
            if game not in game_market_types:
                game_market_types[game] = []
            game_market_types[game].append(mtype)

        for game, types in game_market_types.items():
            if "SPREAD" in types and "GAME_WINNER" in types:
                return False, (f"Invalid combo: Cannot combine SPREAD and "
                              f"GAME_WINNER from the same game ({game})")

        # Check for contradictory picks
        game_picks = {}
        for leg in legs:
            game = leg.get("game", "")
            ticker = leg.get("kalshi_ticker", "")
            if game not in game_picks:
                game_picks[game] = []
            game_picks[game].append(ticker)

        # If two tickers from same game event_ticker, check they're not
        # opposite sides of same market
        for game, tickers in game_picks.items():
            if len(tickers) >= 2:
                # Check for -SAS and -MIN from same base ticker
                bases = [t.rsplit("-", 1)[0] for t in tickers if t]
                if len(bases) != len(set(bases)):
                    return False, (f"Invalid combo: Opposite sides of same "
                                  f"market in {game}")

        return True, ""

    # ── ROLLOVER CALCULATOR ───────────────────────────────────────────────────

    def calculate_rollover(self, days: int, odds_per_day: float,
                            starting_stake: float) -> dict:
        """
        Calculate rollover projections and validate feasibility.
        """
        from config.config import (CALC_MAX_DAYS, CALC_MAX_ODDS_PER_DAY,
                                    CALC_MIN_ODDS_PER_DAY)

        errors = []

        # Validate inputs
        if days > CALC_MAX_DAYS:
            errors.append(f"Maximum {CALC_MAX_DAYS} days allowed")
        if odds_per_day > CALC_MAX_ODDS_PER_DAY:
            errors.append(f"Maximum {CALC_MAX_ODDS_PER_DAY}x odds per day")
        if odds_per_day < CALC_MIN_ODDS_PER_DAY:
            errors.append(f"Minimum {CALC_MIN_ODDS_PER_DAY}x odds per day — "
                         f"not worth rolling over")
        if starting_stake <= 0:
            errors.append("Starting stake must be positive")

        # Check if Kalshi can realistically provide these odds
        # Kalshi sports markets typically offer 1.1x - 5x odds
        if odds_per_day > 5.0:
            errors.append(f"Kalshi sports markets rarely exceed 5x per day — "
                         f"this rollover may not be achievable")

        if errors:
            return {"valid": False, "errors": errors}

        # Calculate day-by-day projections
        daily_breakdown = []
        current = starting_stake
        for day in range(1, days + 1):
            payout = current * odds_per_day
            daily_breakdown.append({
                "day": day,
                "stake": round(current, 2),
                "payout": round(payout, 2),
                "profit": round(payout - current, 2),
            })
            current = payout

        final_payout = round(starting_stake * (odds_per_day ** days), 2)
        total_profit = round(final_payout - starting_stake, 2)
        roi_pct = round((total_profit / starting_stake) * 100, 1)

        # Realistic probability estimate
        # Assume ~65% daily win rate for 3x odds, scales with odds
        base_win_rate = max(0.35, min(0.75, 0.85 - (odds_per_day - 1) * 0.15))
        completion_prob = round((base_win_rate ** days) * 100, 2)

        return {
            "valid": True,
            "days": days,
            "odds_per_day": odds_per_day,
            "starting_stake": starting_stake,
            "final_payout": final_payout,
            "total_profit": total_profit,
            "roi_pct": roi_pct,
            "daily_breakdown": daily_breakdown,
            "completion_probability": completion_prob,
            "base_daily_win_rate": round(base_win_rate * 100, 1),
            "reality_check": self._rollover_reality_check(
                completion_prob, days, odds_per_day
            ),
        }

    def _rollover_reality_check(self, completion_prob: float,
                                  days: int, odds: float) -> dict:
        """Generate a realistic assessment of the rollover."""
        if completion_prob >= 50:
            level = "GOOD"
            message = (f"Strong rollover — {completion_prob}% estimated "
                      f"completion probability. Solid choice.")
        elif completion_prob >= 20:
            level = "MODERATE"
            message = (f"Achievable rollover — {completion_prob}% estimated "
                      f"completion probability. Will reset occasionally.")
        elif completion_prob >= 5:
            level = "AGGRESSIVE"
            message = (f"Aggressive rollover — {completion_prob}% estimated "
                      f"completion probability. High reward, frequent resets.")
        else:
            level = "LOTTERY"
            message = (f"Lottery-tier — {completion_prob}% estimated "
                      f"completion probability. Treat as a high-risk play.")

        return {"level": level, "message": message}
