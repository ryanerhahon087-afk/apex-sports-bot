"""
APEX/SPORTS BOT — Kalshi API Client
Handles authentication, market fetching, and order placement.
"""
import asyncio
import aiohttp
import base64
import hashlib
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SportsKalshiClient:
    """
    Kalshi API client for sports markets.
    Uses RSA PKCS1v15 signing for authentication.
    """

    def __init__(self, api_key_id: str, private_key_pem: str,
                 base_url: str, paper_mode: bool = True):
        self._api_key_id = api_key_id
        self._private_key_pem = private_key_pem
        self._base_url = base_url
        self._paper_mode = paper_mode
        self._session: Optional[aiohttp.ClientSession] = None
        self._private_key = None

    async def connect(self):
        """Initialize session and load RSA key."""
        self._session = aiohttp.ClientSession()
        self._load_private_key()
        logger.info(f"[KALSHI] Connected to {self._base_url} "
                   f"({'paper' if self._paper_mode else 'LIVE'} mode)")

    def _load_private_key(self):
        """Load RSA private key from PEM string."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem = self._private_key_pem
        if "\\n" in pem and "\n" not in pem:
            pem = pem.replace("\\n", "\n")
        self._private_key = load_pem_private_key(pem.encode(), password=None)
        logger.info(f"[KALSHI] RSA key loaded (id={self._api_key_id[:8]}...)")

    def _auth_headers(self, method: str, path: str) -> dict:
        """Generate RSA-signed auth headers."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        clean_path = path.split("?")[0]
        message = (timestamp + method.upper() + clean_path).encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict = None) -> dict:
        """Make authenticated GET request."""
        url = self._base_url + path
        headers = self._auth_headers("GET", path)
        try:
            async with self._session.get(
                url, params=params, headers=headers
            ) as r:
                if r.status == 200:
                    return await r.json()
                else:
                    text = await r.text()
                    logger.warning(f"[KALSHI] GET {path} → {r.status}: {text[:100]}")
                    return {}
        except Exception as e:
            logger.error(f"[KALSHI] GET {path} error: {e}")
            return {}

    async def _post(self, path: str, body: dict) -> dict:
        """Make authenticated POST request."""
        url = self._base_url + path
        headers = self._auth_headers("POST", path)
        try:
            async with self._session.post(
                url, json=body, headers=headers
            ) as r:
                if r.status in (200, 201):
                    return await r.json()
                else:
                    text = await r.text()
                    logger.warning(f"[KALSHI] POST {path} → {r.status}: {text[:200]}")
                    return {}
        except Exception as e:
            logger.error(f"[KALSHI] POST {path} error: {e}")
            return {}

    # ── MARKET FETCHING ───────────────────────────────────────────────────────

    async def fetch_series_markets(self, series_ticker: str,
                                    limit: int = 100,
                                    status: str = "open") -> list:
        """Fetch markets for a given series (default: open; pass 'settled' for backtest)."""
        data = await self._get("/markets", {
            "series_ticker": series_ticker,
            "status": status,
            "limit": limit,
        })
        markets = data.get("markets", [])
        logger.debug(f"[KALSHI] {series_ticker}: {len(markets)} markets")
        return markets

    async def fetch_all_sports_markets(self, series_list: list) -> dict:
        """
        Fetch markets for all active series.
        Returns dict keyed by series ticker.
        """
        results = {}
        for series in series_list:
            markets = await self.fetch_series_markets(series)
            if markets:
                results[series] = markets
                # Log sample
                m = markets[0]
                yes_ask = float(m.get("yes_ask_dollars") or 0)
                yes_bid = float(m.get("yes_bid_dollars") or 0)
                logger.info(f"[MARKETS] {series}: {len(markets)} markets | "
                           f"sample: {m.get('title','')[:50]} | "
                           f"ask={yes_ask:.2f} bid={yes_bid:.2f}")
            else:
                logger.info(f"[MARKETS] {series}: 0 markets")

        # FIX 5: Summary log
        total = sum(len(v) for v in results.values())
        logger.info(
            f"[MARKETS] Series scan complete ({total} total): " +
            ", ".join(f"{k}:{len(v)}" for k, v in results.items())
        )
        return results

    async def get_market(self, ticker: str) -> dict:
        """Fetch a single market by ticker."""
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", {})

    async def get_market_status(self, ticker: str) -> dict:
        """Check if a market has settled."""
        data = await self._get(f"/markets/{ticker}")
        market = data.get("market", {})
        return {
            "status": market.get("status", ""),
            "result": market.get("result", ""),
            "resolved": market.get("status") in ("settled", "finalized"),
            "yes_ask": float(market.get("yes_ask_dollars") or 0),
            "yes_bid": float(market.get("yes_bid_dollars") or 0),
        }

    # ── ORDER PLACEMENT ───────────────────────────────────────────────────────

    async def place_order(self, ticker: str, direction: str,
                          entry_price: float, quantity: int,
                          slip_id: int) -> dict:
        """
        Place a YES/NO order on a market.
        In paper mode: simulate the fill.
        """
        if self._paper_mode:
            logger.info(f"[ORDER] PAPER {direction} {ticker} | "
                       f"qty={quantity} price={entry_price:.3f} "
                       f"stake=${entry_price * quantity:.2f} slip={slip_id}")
            return {
                "order_id": f"PAPER-{slip_id}-{ticker}",
                "ticker": ticker,
                "direction": direction,
                "price": entry_price,
                "quantity": quantity,
                "status": "filled",
                "paper": True,
            }

        # Live order
        side = "yes" if direction == "YES" else "no"
        body = {
            "ticker": ticker,
            "client_order_id": f"apex-sports-{slip_id}-{int(time.time())}",
            "type": "limit",
            "action": "buy",
            "side": side,
            "count": quantity,
            "yes_price": int(entry_price * 100),
        }
        result = await self._post("/orders", body)
        if result:
            logger.info(f"[ORDER] LIVE {direction} {ticker} | "
                       f"qty={quantity} price={entry_price:.3f} → {result}")
        return result

    # ── ACCOUNT ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get current account balance."""
        data = await self._get("/portfolio/balance")
        cents = data.get("balance", 0)
        return cents / 100.0  # Kalshi returns cents

    async def disconnect(self):
        if self._session:
            await self._session.close()
        logger.info("[KALSHI] Disconnected")
