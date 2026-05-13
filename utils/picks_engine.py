"""
APEX/SPORTS - Picks Engine
Builds 3 daily slips: 2x odds, 3x odds, 5x odds
Uses all available Kalshi markets across NBA/MLB/NHL
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import anthropic

logger = logging.getLogger(__name__)

SERIES_TO_SPORT = {
    "KXNBAGAME":   "NBA",
    "KXNBATOTAL":  "NBA",
    # KXNBATEAMTOTAL excluded — every market is an individual team total
    # (hallucination risk; Kalshi text doesn't match keyword filters)
    "KXNBAPLAYER": "NBA",
    "KXMLBGAME":   "MLB",
    "KXMLBTOTAL":  "MLB",
    "KXMLBRFI":    "MLB",
    "KXNHLGAME":   "NHL",
    "KXNHLTOTAL":  "NHL",
    "KXNFLGAME":   "NFL",
    "KXNFLTOTAL":  "NFL",
}

ALL_SERIES = list(SERIES_TO_SPORT.keys())


class PicksEngine:

    def __init__(self, api_key: str, kalshi_client):
        self._anthropic = anthropic.Anthropic(api_key=api_key)
        self._kalshi = kalshi_client

    async def generate_all_slips(self, balance: float) -> dict:
        """Generate all 3 slips for today."""
        markets = await self._fetch_markets()
        if len(markets) < 5:
            return {"error": f"Not enough markets available ({len(markets)} found)"}

        logger.info(f"[PICKS] {len(markets)} markets available across all series")

        slip_2x = await self._build_slip(markets, balance, target_odds=2.0,
                                          slip_name="SAFE (2x)")
        await asyncio.sleep(3)

        slip_3x = await self._build_slip(markets, balance, target_odds=3.0,
                                          slip_name="STANDARD (3x)")
        await asyncio.sleep(3)

        slip_5x = await self._build_slip(markets, balance, target_odds=5.0,
                                          slip_name="BOLD (5x)")

        return {
            "date":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "markets_available":  len(markets),
            "slip_2x":            slip_2x,
            "slip_3x":            slip_3x,
            "slip_5x":            slip_5x,
        }

    async def _fetch_markets(self) -> list:
        """Fetch all open markets across all sports series (no odds filter here)."""
        all_markets = []

        for series in ALL_SERIES:
            try:
                data = await self._kalshi._get("/markets", {
                    "series_ticker": series,
                    "status": "open",
                    "limit": 100,
                })
                sport = SERIES_TO_SPORT.get(series, "OTHER")

                for m in data.get("markets", []):
                    yes_ask = float(m.get("yes_ask_dollars") or 0)
                    if yes_ask <= 0 or yes_ask >= 1:
                        continue

                    # Filter out individual team total markets (hallucinated by AI)
                    pick_text  = (m.get("yes_sub_title") or "").lower()
                    title_text = (m.get("title") or "").lower()
                    skip_keywords = [
                        "team total", "to score over", "score more than",
                        "points by", "runs by", "goals by",
                    ]
                    if any(kw in pick_text or kw in title_text for kw in skip_keywords):
                        continue

                    ticker = m.get("ticker", "")
                    if "GAME" in series:
                        mtype = "GAME_WINNER"
                    elif "TOTAL" in series:
                        mtype = "TOTAL"
                    elif "PLAYER" in series:
                        mtype = "PLAYER_PROP"
                    elif "RFI" in series:
                        mtype = "FIRST_INNING"
                    else:
                        mtype = "OTHER"

                    game_time = (m.get("occurrence_datetime") or
                                 m.get("close_time") or "")

                    all_markets.append({
                        "ticker":       ticker,
                        "series":       series,
                        "sport":        sport,
                        "market_type":  mtype,
                        "title":        m.get("title", ""),
                        "pick":         m.get("yes_sub_title", "") or m.get("title", ""),
                        "yes_ask":      yes_ask,
                        "yes_bid":      float(m.get("yes_bid_dollars") or 0),
                        "odds":         round(1 / yes_ask, 3),
                        "game_time":    game_time,
                        "event_ticker": m.get("event_ticker", ""),
                    })

                count = sum(1 for m in all_markets if m["series"] == series)
                if count:
                    logger.info(f"[PICKS] {series}: {count} valid markets")

            except Exception as e:
                logger.warning(f"[PICKS] {series} fetch error: {e}")

        all_markets.sort(key=lambda x: x.get("game_time", ""))
        return all_markets

    def _get_eligible_markets(self, markets: list, target_odds: float) -> list:
        """Filter markets to the odds range appropriate for this slip's target.

        yes_ask ceiling = 0.87 → min 1.15x per leg (prevents near-certain garbage)
        yes_ask floor varies by tier → caps max per-leg odds to prevent overshoot.
        """
        if target_odds <= 2.0:
            # per-leg target ~1.15x-1.35x → allow yes_ask 0.74-0.87
            min_ask, max_ask = 0.74, 0.87
        elif target_odds <= 3.0:
            # per-leg target ~1.20x-1.50x → allow yes_ask 0.67-0.87
            min_ask, max_ask = 0.67, 0.87
        else:
            # per-leg target ~1.20x-2.50x → allow yes_ask 0.40-0.87
            min_ask, max_ask = 0.40, 0.87

        return [m for m in markets if min_ask <= m["yes_ask"] <= max_ask]

    async def _build_slip(self, markets: list, balance: float,
                           target_odds: float, slip_name: str) -> dict:
        """
        Build one slip targeting specific combined odds.
        Claude with web_search researches and selects picks in one call.
        """
        logger.info(f"[PICKS] Building {slip_name} slip...")

        eligible = self._get_eligible_markets(markets, target_odds)
        if len(eligible) < 3:
            return {
                "slip_name":   slip_name,
                "target_odds": target_odds,
                "status":      "FAILED",
                "error":       f"Not enough eligible markets for {slip_name} ({len(eligible)} found)",
            }

        lines = []
        for m in eligible[:80]:
            lines.append(
                f"{m['ticker']} | {m['sport']} | {m['market_type']} | "
                f"{m['pick']} | odds={m['odds']:.2f}x | "
                f"game_time={m['game_time'][:16]}"
            )
        markets_text = "\n".join(lines)

        if target_odds <= 2.0:
            min_legs, max_legs = 4, 6
            legs_desc = "4-6 legs"
            odds_range_desc = "1.15x-1.30x per leg"
            per_leg_max = 1.35
            combined_min, combined_max = 1.8, 2.5
            math_example = (
                "5 legs at 1.18x each = 1.18^5 = 2.29x ✓\n"
                "4 legs at 1.22x each = 1.22^4 = 2.22x ✓\n"
                "4 legs at 1.50x each = 1.50^4 = 5.06x ✗ (too high — stay under 1.35x per leg)"
            )
        elif target_odds <= 3.0:
            min_legs, max_legs = 4, 6
            legs_desc = "4-6 legs"
            odds_range_desc = "1.20x-1.45x per leg"
            per_leg_max = 1.50
            combined_min, combined_max = 2.5, 3.5
            math_example = (
                "5 legs at 1.25x each = 1.25^5 = 3.05x ✓\n"
                "4 legs at 1.32x each = 1.32^4 = 3.03x ✓\n"
                "5 legs at 1.60x each = 1.60^5 = 10.49x ✗ (too high — stay under 1.50x per leg)"
            )
        else:
            min_legs, max_legs = 6, 8
            legs_desc = "6-8 legs"
            odds_range_desc = "1.20x-2.00x per leg, mixing safer and bolder picks"
            per_leg_max = 2.5
            combined_min, combined_max = 4.5, 6.0
            math_example = (
                "6 legs at 1.31x each = 1.31^6 = 5.0x ✓\n"
                "4 legs at 1.20x + 2 legs at 1.70x = 2.07 × 2.89 = 5.99x ✓\n"
                "7 legs at 1.20x each = 1.20^7 = 3.58x ✗ (too low — add some 1.4x-2.0x legs)\n"
                "5 legs at 1.60x each = 1.60^5 = 10.49x ✗ (too high)"
            )

        stake = round(min(balance * 0.10, 10_000.0), 2)

        prompt = f"""You are a sharp sports analyst building a Kalshi prediction market parlay slip.

TODAY: {datetime.now(timezone.utc).strftime('%A %B %d, %Y')}
TARGET: {slip_name} slip

COMBINED ODDS TARGET: {combined_min}x to {combined_max}x (aim for {target_odds}x)
PER-LEG ODDS: {odds_range_desc} — DO NOT exceed {per_leg_max}x on any single leg

MATH CHECK — make sure your picks multiply to the target:
{math_example}

AVAILABLE KALSHI MARKETS:
{markets_text}

YOUR STRATEGY:
Use {legs_desc}. Each leg: highly likely to win (65-85% probability).
The combined product of all leg odds MUST land between {combined_min}x and {combined_max}x.
Before finalising, multiply out your leg odds and verify you are in range.

IMPORTANT: Only Kalshi market types that actually exist:
1. GAME_WINNER - Will [team] win? YES/NO
2. TOTAL - Will BOTH teams combined score over X?
3. PLAYER_PROP - Will [player] achieve X stat? (if available)

DO NOT pick individual team scoring totals.
DO NOT pick markets with odds below 1.15x (too certain, bad value).
DO NOT pick any single leg above {per_leg_max}x odds.

PICK SELECTION RULES:
1. Use {min_legs}-{max_legs} legs — NEVER fewer than {min_legs}
2. Mix sports when possible (NBA + MLB + NHL adds diversity)
3. Mix market types when possible (totals + game winners + player props)
4. For TOTAL markets: pick OVER on LOW lines (safer), not UNDER on high lines
5. For GAME_WINNER: only pick clear favorites (odds 1.2x-2.0x)
6. For PLAYER_PROP: pick player props with very low bars (1+ threes, 10+ points)
7. All picks from games happening today or tomorrow ONLY
8. Never pick two markets from the exact same event_ticker
9. ONLY use tickers from the list above — never invent tickers

Use web search to check:
- Current injury reports (is the star player actually playing?)
- Recent team form (last 5 games)
- Head to head records
- Today's confirmed lineups if available

Respond ONLY with valid JSON, no other text:
{{
    "slip_name": "{slip_name}",
    "target_odds": {target_odds},
    "legs": [
        {{
            "ticker": "exact ticker from list above",
            "sport": "NBA/MLB/NHL/NFL",
            "market_type": "TOTAL/GAME_WINNER/PLAYER_PROP/FIRST_INNING",
            "game": "Team A vs Team B",
            "pick": "exact pick description",
            "odds": 1.25,
            "confidence": 8.2,
            "reasoning": "why this pick is safe and likely to hit"
        }}
    ],
    "combined_odds": {target_odds},
    "overall_confidence": 7.8,
    "summary": "one sentence why this slip is solid"
}}"""

        try:
            response = self._anthropic.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )

            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text

            text = text.strip()
            if "```" in text:
                text = re.sub(r"```json?\n?", "", text).rstrip("`").strip()

            json_match = re.search(r"\{[\s\S]*\}", text)
            if not json_match:
                raise ValueError(f"No JSON found in response: {text[:200]}")

            slip_data = json.loads(json_match.group())
            legs = slip_data.get("legs", [])

            if len(legs) < 2:
                raise ValueError(f"Only {len(legs)} legs returned")

            # Validate tickers and pin odds to real Kalshi prices
            ticker_map = {m["ticker"]: m for m in markets}
            valid_legs = []
            combined = 1.0

            for leg in legs:
                ticker = leg.get("ticker", "")
                if ticker not in ticker_map:
                    logger.warning(f"[PICKS] Invalid ticker: {ticker!r}")
                    continue
                mkt = ticker_map[ticker]
                leg["odds"]      = mkt["odds"]
                leg["yes_ask"]   = mkt["yes_ask"]
                leg["game_time"] = mkt["game_time"]
                combined *= mkt["odds"]
                valid_legs.append(leg)

            if len(valid_legs) < 2:
                raise ValueError("Not enough valid tickers after validation")

            combined = round(combined, 4)
            logger.info(
                f"[PICKS] {slip_name}: {len(valid_legs)} legs, "
                f"{combined}x odds, conf={slip_data.get('overall_confidence')}"
            )

            return {
                "slip_name":          slip_name,
                "target_odds":        target_odds,
                "legs":               valid_legs,
                "combined_odds":      combined,
                "leg_count":          len(valid_legs),
                "overall_confidence": float(slip_data.get("overall_confidence") or 0),
                "summary":            slip_data.get("summary", ""),
                "stake":              stake,
                "potential_payout":   round(stake * combined, 2),
                "status":             "READY",
            }

        except Exception as e:
            logger.error(f"[PICKS] {slip_name} failed: {e}")
            return {
                "slip_name":   slip_name,
                "target_odds": target_odds,
                "status":      "FAILED",
                "error":       str(e),
            }
