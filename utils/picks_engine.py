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
        """Filter markets to the odds range appropriate for this slip's target."""
        if target_odds <= 2.0:
            min_ask, max_ask = 0.55, 0.87   # 1.15x – 1.82x per leg
        elif target_odds <= 3.0:
            min_ask, max_ask = 0.45, 0.87   # 1.15x – 2.22x per leg
        else:
            min_ask, max_ask = 0.35, 0.87   # 1.15x – 2.86x per leg

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
            odds_range_desc = "1.15x-1.82x per leg"
            bold_instruction = ""
        elif target_odds <= 3.0:
            min_legs, max_legs = 4, 6
            legs_desc = "4-6 legs"
            odds_range_desc = "1.15x-2.22x per leg"
            bold_instruction = ""
        else:
            min_legs, max_legs = 6, 8
            legs_desc = "6-8 legs"
            odds_range_desc = "1.15x-2.86x per leg"
            bold_instruction = """
YOU MUST reach at least 4.5x combined odds minimum.
Use 6-8 legs. Include some picks at 1.5x-2.5x odds alongside the safer 1.15x-1.25x picks.
A mix of safer and slightly bolder picks is better than all low-odds picks that can't reach the target.
Example that works: 4 legs at 1.20x + 2 legs at 1.60x = 1.20^4 × 1.60^2 = 5.31x ✓
Example that fails: 7 legs at 1.20x = 3.58x ✗"""

        stake = round(min(balance * 0.10, 10_000.0), 2)

        prompt = f"""You are a sharp sports analyst building a Kalshi prediction market parlay slip.

TODAY: {datetime.now(timezone.utc).strftime('%A %B %d, %Y')}
TARGET: {slip_name} slip — combined odds as close to {target_odds}x as possible
{bold_instruction}
AVAILABLE KALSHI MARKETS (filtered to {odds_range_desc}):
{markets_text}

YOUR STRATEGY:
Build this slip using {legs_desc} ({odds_range_desc}).
Each individual leg should be highly likely to win (65-85% probability).
Together they must multiply to AT LEAST {target_odds * 0.90:.1f}x combined odds (target {target_odds}x).

IMPORTANT: Only Kalshi market types that actually exist:
1. GAME_WINNER - Will [team] win? YES/NO
2. TOTAL - Will BOTH teams combined score over X?
3. PLAYER_PROP - Will [player] achieve X stat? (if available)

DO NOT pick individual team scoring totals.
DO NOT pick markets with odds below 1.15x (too certain, bad value).
DO NOT pick markets with odds above 2.9x per single leg (too risky).

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
