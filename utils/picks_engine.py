"""
APEX/SPORTS — Picks Engine
One job: find good Kalshi sports picks and build a combo slip.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import anthropic

logger = logging.getLogger(__name__)

SERIES = [
    "KXNBAGAME",
    "KXNBATOTAL",
    "KXMLBGAME",
    "KXMLBTOTAL",
    "KXNHLGAME",
    "KXNHLTOTAL",
]


class PicksEngine:

    def __init__(self, api_key: str, kalshi_client):
        self._anthropic = anthropic.Anthropic(api_key=api_key)
        self._kalshi = kalshi_client

    async def generate_picks(self, balance: float) -> dict:
        """Main entry point. Returns a ready-to-use slip with 3-5 legs."""
        try:
            markets = await self._get_available_markets()
            if not markets:
                return {"error": "No markets available right now"}
            logger.info(f"[PICKS] Found {len(markets)} available markets")
            return await self._build_slip(markets, balance)
        except Exception as e:
            logger.error(f"[PICKS] Error: {e}", exc_info=True)
            return {"error": str(e)}

    async def _get_available_markets(self) -> list:
        """Fetch all open sports markets from Kalshi."""
        all_markets = []
        for series in SERIES:
            try:
                data = await self._kalshi._get("/markets", {
                    "series_ticker": series,
                    "status": "open",
                    "limit": 50,
                })
                for m in data.get("markets", []):
                    yes_ask = float(m.get("yes_ask_dollars") or 0)
                    if yes_ask <= 0.05 or yes_ask >= 0.95:
                        continue
                    game_time = (
                        m.get("occurrence_datetime") or
                        m.get("close_time", "")
                    )
                    all_markets.append({
                        "ticker":           m.get("ticker", ""),
                        "title":            m.get("title", ""),
                        "pick_description": m.get("yes_sub_title", "") or m.get("title", ""),
                        "series":           series,
                        "yes_ask":          yes_ask,
                        "yes_bid":          float(m.get("yes_bid_dollars") or 0),
                        "odds":             round(1 / yes_ask, 3),
                        "game_time":        game_time,
                        "event_ticker":     m.get("event_ticker", ""),
                    })
            except Exception as e:
                logger.warning(f"[PICKS] Error fetching {series}: {e}")

        all_markets.sort(key=lambda x: x.get("game_time", ""))
        return all_markets

    async def _build_slip(self, markets: list, balance: float) -> dict:
        """
        Give Claude the market list and ask it to build the best slip.
        One API call does research + selection.
        """
        markets_text = "\n".join([
            f"- {m['ticker']} | {m['title']} | {m['pick_description']} | "
            f"odds={m['odds']:.2f}x (yes_ask={m['yes_ask']:.2f}) | "
            f"game_time={m['game_time'][:16]}"
            for m in markets[:60]
        ])

        stake = round(min(balance * 0.10, 10_000.0), 2)

        prompt = f"""You are a sharp sports analyst building a Kalshi prediction market parlay slip.

TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

AVAILABLE KALSHI MARKETS:
{markets_text}

YOUR JOB:
Select 3-5 picks from the list above to build a parlay worth 2.0x–5.0x combined odds.

HARD RULES:
1. Select EXACTLY 3 to 5 picks — never fewer than 3, never more than 5
2. All picks must be from games happening TODAY or TOMORROW only
3. No two picks from the same event_ticker (same game)
4. Combined odds target: 2.0x minimum, 5.0x maximum
5. Only use tickers that appear verbatim in the list above — do NOT invent tickers
6. Prefer picks where yes_ask is between 0.55 and 0.80 (safer, higher probability)

RESEARCH:
Use your sports knowledge to evaluate each pick:
- Which team/total has the statistical edge right now?
- Recent form, injuries, home/away splits, pace matchups
- For TOTAL markets: is a low-total OVER safer than a high-total OVER?
- For GAME markets: does one team have a clear edge?

CALIBRATION:
For TOTAL markets, prefer the lower line variants (safer OVER).
Example: if you like OVER on a high-pace game, pick the OVER 210.5 not OVER 220.5.

Return ONLY this JSON — no other text, no markdown fences:
{{
    "legs": [
        {{
            "ticker": "exact ticker from list",
            "game": "Team A vs Team B",
            "pick": "what we are betting (e.g. Over 218.5 pts, Lakers win)",
            "odds": 1.45,
            "confidence": 8.2,
            "reasoning": "2 sentence explanation of why this pick is good"
        }}
    ],
    "combined_odds": 3.24,
    "overall_confidence": 7.8,
    "summary": "One sentence describing why this is a solid slip today"
}}"""

        try:
            response = self._anthropic.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
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
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                text = json_match.group()

            slip_data = json.loads(text)

            # Validate: tickers must exist in our real market list
            valid_tickers = {m["ticker"] for m in markets}
            validated_legs = [
                leg for leg in slip_data.get("legs", [])
                if leg.get("ticker") in valid_tickers
            ]

            invalid_count = len(slip_data.get("legs", [])) - len(validated_legs)
            if invalid_count:
                logger.warning(
                    f"[PICKS] Dropped {invalid_count} legs with invalid tickers"
                )

            if len(validated_legs) < 2:
                return {"error": "Not enough valid picks found — no valid tickers"}

            # Pin odds to actual Kalshi prices (not what AI guessed)
            ticker_map = {m["ticker"]: m for m in markets}
            combined = 1.0
            for leg in validated_legs:
                mkt = ticker_map[leg["ticker"]]
                leg["odds"]     = mkt["odds"]
                leg["yes_ask"]  = mkt["yes_ask"]
                leg["game_time"] = mkt["game_time"]
                combined *= mkt["odds"]

            combined = round(combined, 4)

            return {
                "date":               datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "legs":               validated_legs,
                "combined_odds":      combined,
                "overall_confidence": float(slip_data.get("overall_confidence") or 0),
                "summary":            slip_data.get("summary", ""),
                "stake":              stake,
                "potential_payout":   round(stake * combined, 2),
                "leg_count":          len(validated_legs),
            }

        except json.JSONDecodeError as e:
            logger.error(f"[PICKS] JSON parse error: {e} | text: {text[:300]}")
            return {"error": f"Failed to parse picks response: {e}"}
        except Exception as e:
            logger.error(f"[PICKS] Build slip error: {e}", exc_info=True)
            return {"error": str(e)}
