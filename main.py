"""
APEX/SPORTS BOT — Main Entry Point
Build: 2026-05-12-v13
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import Flask, jsonify, request, Response
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.config import *
from logs.database import SportsDatabase
from data.kalshi_client import SportsKalshiClient

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


@app.errorhandler(Exception)
def handle_any_exception(e):
    import traceback as tb
    logger.error(f"[FLASK] Unhandled exception: {e}", exc_info=True)
    return jsonify({
        "error": str(e),
        "type": type(e).__name__,
        "traceback": tb.format_exc()[-1000:],
    }), 500


# ── GLOBAL STATE ──────────────────────────────────────────────────────────────
db: Optional[SportsDatabase] = None
kalshi: Optional[SportsKalshiClient] = None
bot_running = False
bot_paused  = False


def init_bot():
    global db, kalshi
    db = SportsDatabase(DB_PATH)
    kalshi = SportsKalshiClient(
        api_key_id=KALSHI_API_KEY_ID,
        private_key_pem=KALSHI_PRIVATE_KEY,
        base_url=KALSHI_BASE_URL,
        paper_mode=PAPER_MODE,
    )
    logger.info(f"[BOT] Initialized | paper={'YES' if PAPER_MODE else 'NO'}")


# ── API ENDPOINTS ─────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/status")
def status():
    balance    = db.get_balance()    if db else STARTING_BALANCE
    today      = db.get_today_stats() if db else {}
    all_stats  = db.get_stats()      if db else {}
    total = all_stats.get("total", 0)
    wins  = all_stats.get("wins",  0)
    return jsonify({
        "balance":        balance,
        "paper_mode":     PAPER_MODE,
        "bot_running":    bot_running,
        "bot_paused":     bot_paused,
        "cb_active":      False,
        "today_pnl":      today.get("pnl", 0),
        "today_slips":    today.get("total", 0),
        "all_time_pnl":   all_stats.get("total_pnl", 0),
        "all_time_slips": total,
        "win_rate":       round(wins / total, 4) if total else 0,
        "wins":           wins,
        "losses":         all_stats.get("losses", 0),
        "next_generation": _next_generation_time(),
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/slips")
def get_slips():
    limit = int(request.args.get("limit", 25))
    slips = db.get_recent_slips(limit) if db else []
    return jsonify({"slips": slips})


@app.route("/api/slips/<int:slip_id>/legs")
def get_slip_legs(slip_id):
    legs = db.get_slip_legs(slip_id) if db else []
    return jsonify({"legs": legs})


@app.route("/api/portfolio")
def get_portfolio():
    days = int(request.args.get("days", 30))
    history = db.get_portfolio_history(days) if db else []
    return jsonify({"history": history})


@app.route("/api/stats")
def get_stats():
    return jsonify({
        "all_time":               db.get_stats()      if db else {},
        "today":                  db.get_today_stats() if db else {},
        "circuit_breaker_history": db.get_cb_history(4) if db else [],
    })


# ── Stub endpoints so dashboard JS doesn't throw hard errors ──────────────────

@app.route("/api/rollover/active")
def get_active_rollover():
    return jsonify({"rollover": None})

@app.route("/api/rollover/history")
def get_rollover_history():
    return jsonify({"history": []})

@app.route("/api/rollover/calculate", methods=["POST"])
def calculate_rollover():
    return jsonify({"valid": False, "errors": ["Rollover feature removed"]})

@app.route("/api/rollover/start", methods=["POST"])
def start_rollover():
    return jsonify({"error": "Rollover feature removed"}), 410

@app.route("/api/backtest", methods=["POST"])
def run_backtest():
    return jsonify({"error": "Backtester removed in v13"}), 410

@app.route("/api/backtest/results")
def get_backtest_results():
    return jsonify({"running": False, "result": None})

@app.route("/api/simulate", methods=["POST"])
def run_simulate():
    return jsonify({"error": "Simulator removed in v13"}), 410

@app.route("/api/simulate/results")
def get_simulate_results():
    return jsonify({"running": False, "result": None})


# ── PICKS GENERATION ──────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def generate_picks():
    if kalshi is None:
        return jsonify({"error": "Bot not ready, try again in 10 seconds"}), 503

    try:
        from utils.picks_engine import PicksEngine
        engine  = PicksEngine(ANTHROPIC_API_KEY, kalshi)
        balance = db.get_balance() if db else STARTING_BALANCE

        async def _run():
            # Always create a fresh aiohttp session in this event loop.
            # Reusing a session from a different loop causes "Timeout context
            # manager should be used inside a task".
            if kalshi._session:
                await kalshi._session.close()
                kalshi._session = None
            await kalshi.connect()
            return await engine.generate_all_slips(balance)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_run())
        loop.close()

        if result.get("error"):
            return jsonify({"error": result["error"]}), 500

        # Save each valid slip to DB
        if db:
            for slip_key in ("slip_2x", "slip_3x", "slip_5x"):
                slip = result.get(slip_key, {})
                if slip.get("status") != "READY" or not slip.get("legs"):
                    continue
                try:
                    slip_id = db.save_slip(
                        slip={
                            "slip_type":       "DAILY",
                            "sport_mix":       ",".join(sorted(set(
                                l.get("sport", "?") for l in slip["legs"]
                            ))),
                            "combined_odds":   slip["combined_odds"],
                            "stake":           slip["stake"],
                            "potential_payout": slip["potential_payout"],
                            "confidence":      slip["overall_confidence"],
                            "projected_finish": max(
                                (l.get("game_time", "") for l in slip["legs"]),
                                default="",
                            ),
                        },
                        legs=[{
                            "sport":          l.get("sport", "?"),
                            "game":           l.get("game", ""),
                            "market_type":    l.get("market_type", ""),
                            "pick":           l.get("pick", ""),
                            "original_line":  l.get("pick", ""),
                            "calibrated_line": l.get("pick", ""),
                            "kalshi_ticker":  l.get("ticker", ""),
                            "individual_odds": l.get("odds", 0),
                            "confidence":     l.get("confidence", 0),
                            "ai_reasoning":   l.get("reasoning", ""),
                            "game_start":     l.get("game_time", ""),
                        } for l in slip["legs"]],
                    )
                    result[slip_key]["slip_id"] = slip_id
                except Exception as save_err:
                    logger.error(f"[GEN] Save {slip_key} error: {save_err}")

        return jsonify({"success": True, "result": result})

    except Exception as e:
        logger.error(f"[GEN] Generate error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/picks/today")
def todays_picks():
    try:
        slips = db.get_recent_slips(10) if db else []
        today = datetime.now(timezone.utc).date().isoformat()
        today_slips = [s for s in slips if (s.get("created_at") or "")[:10] == today]
        result = []
        for slip in today_slips:
            legs = db.get_slip_legs(slip["id"]) if db else []
            result.append({
                "slip_id":        slip["id"],
                "slip_type":      slip["slip_type"],
                "combined_odds":  slip["combined_odds"],
                "confidence":     slip["confidence"],
                "stake":          slip["stake"],
                "potential_payout": slip["potential_payout"],
                "status":         slip["status"],
                "sport_mix":      slip.get("sport_mix", ""),
                "legs":           legs,
            })
        return jsonify({"date": today, "slips": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pause", methods=["POST"])
def toggle_pause():
    global bot_paused
    bot_paused = not bot_paused
    return jsonify({"paused": bot_paused})


@app.route("/api/reset-circuit-breaker", methods=["POST"])
def reset_cb():
    return jsonify({"success": True})


@app.route("/api/reset-balance", methods=["POST"])
def reset_balance():
    if not PAPER_MODE:
        return jsonify({"error": "Can only reset balance in paper mode"}), 400
    db.set_balance(STARTING_BALANCE)
    return jsonify({"success": True, "balance": STARTING_BALANCE})


# ── DASHBOARD HTML ────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return Response(_build_dashboard_html(), mimetype="text/html")


def _build_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>APEX/SPORTS</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #080c14;
  --bg2: #0d1320;
  --bg3: #111a2e;
  --border: #1a2840;
  --green: #00e87a;
  --red: #ff3355;
  --gold: #ffc820;
  --blue: #2d9cff;
  --purple: #a855f7;
  --nba-blue: #1d428a;
  --mlb-red: #e31937;
  --soccer-green: #00a550;
  --text: #e2eaf5;
  --muted: #4a6080;
  --card: #0b1222;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; min-height:100vh; }
body::before { content:''; position:fixed; inset:0; background:radial-gradient(ellipse at 20% 50%, rgba(45,156,255,0.04) 0%, transparent 60%), radial-gradient(ellipse at 80% 20%, rgba(168,85,247,0.04) 0%, transparent 60%); pointer-events:none; z-index:0; }
.wrap { position:relative; z-index:1; }

/* HEADER */
.hdr { padding:18px 32px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--border); background:rgba(8,12,20,0.95); backdrop-filter:blur(12px); position:sticky; top:0; z-index:100; }
.logo { display:flex; align-items:center; gap:14px; }
.logo-mark { width:40px; height:40px; background:linear-gradient(135deg, var(--green), var(--blue)); border-radius:10px; display:flex; align-items:center; justify-content:center; font-family:'Bebas Neue'; font-size:18px; color:#000; letter-spacing:1px; }
.logo-text { font-family:'Bebas Neue'; font-size:22px; letter-spacing:2px; color:var(--text); }
.logo-sub { font-size:11px; color:var(--muted); letter-spacing:3px; text-transform:uppercase; margin-top:1px; }
.hdr-right { display:flex; align-items:center; gap:12px; }
.badge { padding:5px 14px; border-radius:20px; font-size:11px; font-family:'Space Mono'; letter-spacing:1px; }
.badge-paper { background:rgba(255,200,32,0.1); border:1px solid rgba(255,200,32,0.3); color:var(--gold); }
.badge-live { background:rgba(0,232,122,0.1); border:1px solid rgba(0,232,122,0.3); color:var(--green); }
.status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot-green { background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }
.dot-red { background:var(--red); }
.dot-gold { background:var(--gold); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
.ctrl-btn { padding:7px 18px; border:1px solid var(--border); background:var(--card); color:var(--muted); border-radius:8px; cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.2s; }
.ctrl-btn:hover { border-color:var(--blue); color:var(--text); }

/* CB BANNER */
.cb-banner { background:rgba(255,51,85,0.08); border-bottom:1px solid rgba(255,51,85,0.3); padding:12px 32px; display:flex; align-items:center; justify-content:space-between; display:none; }
.cb-banner.active { display:flex; }
.cb-banner-text { display:flex; align-items:center; gap:12px; }
.cb-banner-title { color:var(--red); font-weight:700; font-size:14px; }
.cb-banner-reason { color:var(--muted); font-size:13px; }
.cb-resume-btn { padding:7px 20px; background:var(--green); color:#000; border:none; border-radius:8px; font-weight:700; font-size:13px; cursor:pointer; }

/* MAIN GRID */
.main { padding:24px 32px 60px; display:grid; gap:20px; }

/* STAT CARDS */
.stats-row { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; }
.stat-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; position:relative; overflow:hidden; transition:border-color 0.2s; }
.stat-card:hover { border-color:rgba(45,156,255,0.3); }
.stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; }
.sc-green::before { background:linear-gradient(90deg,var(--green),rgba(0,232,122,0)); }
.sc-blue::before { background:linear-gradient(90deg,var(--blue),rgba(45,156,255,0)); }
.sc-gold::before { background:linear-gradient(90deg,var(--gold),rgba(255,200,32,0)); }
.sc-purple::before { background:linear-gradient(90deg,var(--purple),rgba(168,85,247,0)); }
.stat-label { font-size:10px; color:var(--muted); letter-spacing:2px; text-transform:uppercase; margin-bottom:10px; font-weight:600; }
.stat-value { font-family:'Space Mono'; font-size:26px; font-weight:700; }
.stat-sub { font-size:12px; color:var(--muted); margin-top:6px; }
.val-green { color:var(--green); }
.val-red { color:var(--red); }
.val-gold { color:var(--gold); }

/* PORTFOLIO CHART */
.chart-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.card-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
.card-title { font-size:11px; color:var(--muted); letter-spacing:2px; text-transform:uppercase; font-weight:700; display:flex; align-items:center; gap:8px; }
.card-dot { width:6px; height:6px; border-radius:50%; }
.toggle-row { display:flex; gap:6px; }
.tog-btn { padding:4px 12px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.15s; }
.tog-btn.active { background:rgba(45,156,255,0.12); border-color:var(--blue); color:var(--blue); }

/* TWO COLUMN LAYOUT */
.two-col { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.three-col { display:grid; grid-template-columns:2fr 1fr; gap:20px; }

/* ROLLOVER TRACKER */
.rollover-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.rollover-empty { text-align:center; padding:32px; color:var(--muted); font-size:13px; }
.rollover-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:16px; }
.rollover-title { font-weight:700; font-size:15px; }
.rollover-stake { font-family:'Space Mono'; font-size:13px; color:var(--green); }
.rollover-progress { display:flex; align-items:center; gap:8px; margin:16px 0; }
.ro-day { display:flex; flex-direction:column; align-items:center; gap:4px; }
.ro-dot { width:28px; height:28px; border-radius:50%; border:2px solid var(--border); display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:700; color:var(--muted); transition:all 0.3s; }
.ro-dot.won { background:var(--green); border-color:var(--green); color:#000; }
.ro-dot.lost { background:var(--red); border-color:var(--red); color:#fff; }
.ro-dot.current { border-color:var(--gold); color:var(--gold); animation:pulse 2s infinite; }
.ro-line { flex:1; height:2px; background:var(--border); }
.ro-line.won { background:var(--green); }
.ro-day-label { font-size:9px; color:var(--muted); }
.rollover-payout { background:rgba(0,232,122,0.05); border:1px solid rgba(0,232,122,0.15); border-radius:8px; padding:12px 16px; margin-top:12px; display:flex; justify-content:space-between; align-items:center; }
.ro-payout-label { font-size:11px; color:var(--muted); }
.ro-payout-value { font-family:'Space Mono'; font-size:16px; color:var(--green); font-weight:700; }

/* RISK STATUS */
.risk-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.risk-row { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid rgba(26,40,64,0.5); }
.risk-row:last-child { border-bottom:none; }
.risk-label { font-size:12px; color:var(--muted); font-weight:600; letter-spacing:0.5px; }
.risk-value { font-family:'Space Mono'; font-size:13px; font-weight:700; }
.rv-green { color:var(--green); }
.rv-red { color:var(--red); }
.rv-gold { color:var(--gold); }
.rv-muted { color:var(--muted); }
.cb-history { margin-top:16px; }
.cb-event { font-size:11px; padding:6px 0; border-bottom:1px solid rgba(26,40,64,0.3); display:flex; gap:8px; align-items:center; }
.cb-event:last-child { border-bottom:none; }
.cb-wr { color:var(--red); font-family:'Space Mono'; font-size:11px; }
.cb-how { color:var(--muted); font-size:10px; }

/* ROLLOVER CALCULATOR */
.calc-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.calc-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:16px; }
.calc-field { display:flex; flex-direction:column; gap:6px; }
.calc-label { font-size:11px; color:var(--muted); letter-spacing:1px; text-transform:uppercase; font-weight:600; }
.calc-input { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:10px 14px; color:var(--text); font-family:'Space Mono'; font-size:14px; width:100%; transition:border-color 0.2s; }
.calc-input:focus { outline:none; border-color:var(--blue); }
.calc-results { background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:16px; margin-top:12px; display:none; }
.calc-results.show { display:block; }
.calc-summary { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:16px; }
.calc-sum-item { text-align:center; }
.calc-sum-label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; margin-bottom:4px; }
.calc-sum-value { font-family:'Space Mono'; font-size:18px; font-weight:700; color:var(--green); }
.calc-table { width:100%; border-collapse:collapse; font-size:12px; }
.calc-table th { text-align:left; padding:6px 10px; color:var(--muted); font-size:10px; letter-spacing:1px; text-transform:uppercase; border-bottom:1px solid var(--border); }
.calc-table td { padding:8px 10px; border-bottom:1px solid rgba(26,40,64,0.4); font-family:'Space Mono'; }
.calc-table tr:last-child td { border-bottom:none; }
.reality-check { margin-top:12px; padding:10px 14px; border-radius:8px; font-size:12px; }
.rc-good { background:rgba(0,232,122,0.05); border:1px solid rgba(0,232,122,0.2); color:var(--green); }
.rc-moderate { background:rgba(255,200,32,0.05); border:1px solid rgba(255,200,32,0.2); color:var(--gold); }
.rc-aggressive { background:rgba(255,51,85,0.05); border:1px solid rgba(255,51,85,0.2); color:var(--red); }
.rc-lottery { background:rgba(168,85,247,0.05); border:1px solid rgba(168,85,247,0.2); color:var(--purple); }
.calc-error { background:rgba(255,51,85,0.08); border:1px solid rgba(255,51,85,0.3); border-radius:8px; padding:12px 16px; color:var(--red); font-size:13px; margin-top:12px; display:none; }
.calc-error.show { display:block; }
.calc-actions { display:flex; gap:10px; margin-top:14px; }
.calc-continue-btn { padding:10px 24px; background:var(--green); color:#000; border:none; border-radius:8px; font-weight:700; font-size:14px; cursor:pointer; display:none; }
.calc-continue-btn.show { display:block; }
.calc-calc-btn { padding:10px 24px; background:var(--bg3); border:1px solid var(--border); color:var(--text); border-radius:8px; font-weight:600; font-size:14px; cursor:pointer; flex:1; }
.calc-calc-btn:hover { border-color:var(--blue); }

/* RECENT ACTIVITY */
.activity-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.activity-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
.activity-filters { display:flex; gap:6px; }
.af-btn { padding:4px 14px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.15s; }
.af-btn.active { background:rgba(45,156,255,0.1); border-color:var(--blue); color:var(--blue); }
.activity-table { width:100%; border-collapse:collapse; }
.activity-table th { text-align:left; padding:8px 14px; color:var(--muted); font-size:10px; letter-spacing:1.5px; text-transform:uppercase; border-bottom:1px solid var(--border); font-weight:600; }
.activity-table td { padding:12px 14px; border-bottom:1px solid rgba(26,40,64,0.4); vertical-align:middle; }
.activity-table tr:last-child td { border-bottom:none; }
.activity-table tr { cursor:pointer; transition:background 0.15s; }
.activity-table tr:hover td { background:rgba(255,255,255,0.02); }
.slip-icon { width:32px; height:32px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:16px; flex-shrink:0; }
.si-daily { background:rgba(0,232,122,0.1); }
.si-rollover { background:rgba(168,85,247,0.1); }
.si-lotto { background:rgba(255,200,32,0.1); }
.slip-type-badge { display:inline-flex; padding:3px 10px; border-radius:6px; font-size:11px; font-family:'Space Mono'; font-weight:700; letter-spacing:0.5px; }
.stb-daily { background:rgba(0,232,122,0.1); color:var(--green); }
.stb-rollover { background:rgba(168,85,247,0.1); color:var(--purple); }
.stb-lotto { background:rgba(255,200,32,0.1); color:var(--gold); }
.status-badge { display:inline-flex; padding:3px 10px; border-radius:6px; font-size:11px; font-family:'Space Mono'; font-weight:700; }
.sb-open { background:rgba(255,200,32,0.1); color:var(--gold); }
.sb-won { background:rgba(0,232,122,0.1); color:var(--green); }
.sb-lost { background:rgba(255,51,85,0.1); color:var(--red); }
.conf-bar { display:inline-flex; align-items:center; gap:6px; }
.conf-fill-wrap { width:40px; height:3px; background:rgba(255,255,255,0.06); border-radius:2px; }
.conf-fill { height:100%; border-radius:2px; }
.mono { font-family:'Space Mono'; font-size:12px; }
.pnl-pos { color:var(--green); font-family:'Space Mono'; font-weight:700; }
.pnl-neg { color:var(--red); font-family:'Space Mono'; font-weight:700; }
.sport-pill { display:inline-flex; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; letter-spacing:0.5px; }
.sp-nba { background:rgba(29,66,138,0.3); color:#5b8dd9; }
.sp-mlb { background:rgba(227,25,55,0.2); color:#ff6b7a; }
.sp-nhl { background:rgba(0,100,200,0.2); color:#4db8ff; }
.sp-nfl { background:rgba(1,51,105,0.3); color:#7ab3f5; }
.sp-tennis { background:rgba(200,180,0,0.2); color:#ffe033; }
.sp-soccer { background:rgba(0,165,80,0.2); color:#00d465; }

/* MODAL */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); backdrop-filter:blur(4px); z-index:200; display:none; align-items:center; justify-content:center; }
.modal-overlay.show { display:flex; }
.modal { background:var(--bg2); border:1px solid var(--border); border-radius:16px; padding:28px; width:600px; max-height:80vh; overflow-y:auto; position:relative; }
.modal-close { position:absolute; top:16px; right:16px; background:transparent; border:1px solid var(--border); color:var(--muted); width:28px; height:28px; border-radius:6px; cursor:pointer; font-size:16px; display:flex; align-items:center; justify-content:center; }
.modal-title { font-size:16px; font-weight:700; margin-bottom:6px; }
.modal-subtitle { font-size:12px; color:var(--muted); margin-bottom:20px; }
.leg-card { background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:10px; }
.leg-game { font-weight:700; font-size:14px; margin-bottom:6px; }
.leg-pick { color:var(--green); font-size:13px; margin-bottom:4px; font-family:'Space Mono'; }
.leg-original { color:var(--muted); font-size:11px; text-decoration:line-through; }
.leg-reasoning { font-size:12px; color:var(--muted); margin-top:8px; line-height:1.5; }
.leg-meta { display:flex; gap:12px; margin-top:8px; font-size:11px; color:var(--muted); }
.leg-status { font-weight:700; }
.ls-pending { color:var(--gold); }
.ls-won { color:var(--green); }
.ls-lost { color:var(--red); }

/* CONTROLS PANEL */
.controls-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.controls-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.control-btn { padding:12px 16px; border:1px solid var(--border); background:var(--bg2); border-radius:10px; cursor:pointer; font-size:13px; font-family:'DM Sans'; font-weight:600; color:var(--text); text-align:center; transition:all 0.2s; }
.control-btn:hover { border-color:var(--blue); color:var(--blue); }
.control-btn.danger:hover { border-color:var(--red); color:var(--red); }
.control-btn.success:hover { border-color:var(--green); color:var(--green); }

/* GENERATION STATUS */
.gen-status { background:rgba(45,156,255,0.05); border:1px solid rgba(45,156,255,0.15); border-radius:10px; padding:12px 16px; display:flex; justify-content:space-between; align-items:center; }
.gen-label { font-size:12px; color:var(--muted); }
.gen-time { font-family:'Space Mono'; font-size:14px; color:var(--blue); }

/* BACKTEST */
.backtest-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.bt-summary { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
.bt-sum-item { background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:14px; text-align:center; }
.bt-sum-label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:1.5px; margin-bottom:6px; font-weight:600; }
.bt-sum-value { font-family:'Space Mono'; font-size:22px; font-weight:700; }
.bt-day-table { width:100%; border-collapse:collapse; font-size:13px; }
.bt-day-table th { text-align:left; padding:8px 12px; color:var(--muted); font-size:10px; letter-spacing:1.5px; text-transform:uppercase; border-bottom:1px solid var(--border); }
.bt-day-table td { padding:10px 12px; border-bottom:1px solid rgba(26,40,64,0.4); vertical-align:top; }
.bt-day-table tr:last-child td { border-bottom:none; }
.bt-win { color:var(--green); font-weight:700; font-family:'Space Mono'; }
.bt-loss { color:var(--red); font-weight:700; font-family:'Space Mono'; }
.bt-leg { font-size:11px; color:var(--muted); padding:2px 0; }
.bt-leg-won { color:var(--green); }
.bt-leg-lost { color:var(--red); }
.bt-run-row { display:flex; align-items:center; gap:12px; }
.bt-days-select { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:8px 12px; color:var(--text); font-family:'DM Sans'; font-size:13px; }

/* TODAY'S PICKS CARD */
.picks-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:24px; }
.picks-empty { text-align:center; padding:40px 24px; color:var(--muted); font-size:14px; line-height:1.8; }
.picks-empty strong { display:block; font-size:16px; color:var(--text); margin-bottom:8px; }
.ticket { background:var(--bg2); border:1px solid var(--border); border-radius:12px; overflow:hidden; margin-bottom:16px; position:relative; }
.ticket::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; }
.ticket-daily::before { background:linear-gradient(90deg,var(--green),rgba(0,232,122,0)); }
.ticket-rollover::before { background:linear-gradient(90deg,var(--purple),rgba(168,85,247,0)); }
.ticket-lotto::before { background:linear-gradient(90deg,var(--gold),rgba(255,200,32,0)); }
.ticket-hdr { display:flex; align-items:center; justify-content:space-between; padding:14px 18px; border-bottom:1px solid var(--border); }
.ticket-type { display:flex; align-items:center; gap:10px; }
.ticket-badge { padding:5px 14px; border-radius:20px; font-family:'Space Mono'; font-size:12px; font-weight:700; letter-spacing:1px; }
.tb-daily { background:rgba(0,232,122,0.12); color:var(--green); border:1px solid rgba(0,232,122,0.25); }
.tb-rollover { background:rgba(168,85,247,0.12); color:var(--purple); border:1px solid rgba(168,85,247,0.25); }
.tb-lotto { background:rgba(255,200,32,0.12); color:var(--gold); border:1px solid rgba(255,200,32,0.25); }
.ticket-status { font-size:11px; color:var(--muted); font-family:'Space Mono'; }
.ts-pending { color:var(--gold); }
.ts-won { color:var(--green); }
.ts-lost { color:var(--red); }
.ticket-legs { padding:0 18px; }
.pick-row { padding:16px 0; border-bottom:1px solid rgba(26,40,64,0.5); display:flex; gap:14px; align-items:flex-start; }
.pick-row:last-child { border-bottom:none; }
.pick-sport-icon { font-size:22px; flex-shrink:0; margin-top:2px; }
.pick-body { flex:1; min-width:0; }
.pick-game { font-size:15px; font-weight:700; color:var(--text); margin-bottom:5px; }
.pick-line { font-size:17px; font-family:'Space Mono'; color:var(--green); font-weight:700; margin-bottom:6px; }
.pick-reasoning { font-size:13px; color:var(--muted); line-height:1.55; margin-bottom:8px; }
.pick-meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.pick-conf { font-size:12px; font-family:'Space Mono'; padding:3px 10px; border-radius:6px; font-weight:700; }
.pc-high { background:rgba(0,232,122,0.1); color:var(--green); }
.pc-mid  { background:rgba(255,200,32,0.1); color:var(--gold); }
.pc-low  { background:rgba(255,51,85,0.1); color:var(--red); }
.pick-odds-badge { font-size:12px; font-family:'Space Mono'; color:var(--blue); background:rgba(45,156,255,0.1); padding:3px 10px; border-radius:6px; }
.kalshi-btn { padding:4px 14px; background:rgba(45,156,255,0.08); border:1px solid rgba(45,156,255,0.25); color:var(--blue); border-radius:6px; font-size:12px; cursor:pointer; font-family:'DM Sans'; font-weight:600; text-decoration:none; display:inline-flex; align-items:center; gap:5px; transition:all 0.2s; white-space:nowrap; }
.kalshi-btn:hover { background:rgba(45,156,255,0.18); border-color:var(--blue); }
.ticket-footer { background:rgba(0,0,0,0.2); padding:14px 18px; display:flex; align-items:center; justify-content:space-between; }
.ticket-combined { display:flex; align-items:baseline; gap:6px; }
.tc-odds { font-family:'Space Mono'; font-size:22px; font-weight:700; color:var(--green); }
.tc-label { font-size:12px; color:var(--muted); }
.ticket-stake-box { text-align:right; }
.tsb-label { font-size:11px; color:var(--muted); margin-bottom:2px; }
.tsb-value { font-family:'Space Mono'; font-size:16px; color:var(--gold); font-weight:700; }
.tsb-payout { font-size:11px; color:var(--muted); margin-top:2px; }
.picks-date-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:20px; }
.picks-date-label { font-family:'Space Mono'; font-size:13px; color:var(--muted); }
.picks-refresh-btn { padding:6px 16px; background:transparent; border:1px solid var(--border); border-radius:7px; color:var(--muted); cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.2s; }
.picks-refresh-btn:hover { border-color:var(--blue); color:var(--blue); }
.picks-gen-btn { padding:8px 20px; background:rgba(0,232,122,0.1); border:1px solid rgba(0,232,122,0.3); border-radius:8px; color:var(--green); cursor:pointer; font-size:13px; font-family:'DM Sans'; font-weight:700; transition:all 0.2s; }
.picks-gen-btn:hover { background:rgba(0,232,122,0.2); }

/* SCROLLBAR */
::-webkit-scrollbar { width:5px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
</style>
</head>
<body>
<div class="wrap">

<!-- HEADER -->
<div class="hdr">
  <div class="logo">
    <div class="logo-mark">AX</div>
    <div>
      <div class="logo-text">APEX/SPORTS</div>
      <div class="logo-sub">Prediction Market Bot</div>
    </div>
  </div>
  <div class="hdr-right">
    <span id="mode-badge" class="badge badge-paper">PAPER MODE</span>
    <span id="status-indicator">
      <span class="status-dot dot-green"></span>
      <span id="status-text" style="font-size:13px;font-weight:600">ACTIVE</span>
    </span>
    <button class="ctrl-btn" onclick="togglePause()">⏸ Pause</button>
    <button class="ctrl-btn" onclick="generateNow()">⚡ Generate Now</button>
  </div>
</div>

<!-- CB BANNER -->
<div class="cb-banner" id="cb-banner">
  <div class="cb-banner-text">
    <span style="font-size:18px">⚡</span>
    <div>
      <div class="cb-banner-title">CIRCUIT BREAKER ACTIVE — All trading halted</div>
      <div class="cb-banner-reason" id="cb-reason">Win rate dropped below threshold</div>
    </div>
    <div style="margin-left:16px;font-size:12px;color:var(--muted)" id="cb-timer"></div>
  </div>
  <button class="cb-resume-btn" onclick="resetCB()">Resume Trading</button>
</div>

<!-- MAIN -->
<div class="main">

  <!-- TODAY'S PICKS -->
  <div class="picks-card" id="picks-section">
    <div class="card-hdr">
      <div class="card-title">
        <span class="card-dot" style="background:var(--gold)"></span>
        Today's Picks
        <span id="picks-date" style="font-size:11px;color:var(--muted);margin-left:8px"></span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="picks-count" class="badge" style="background:rgba(255,200,32,0.1);border:1px solid rgba(255,200,32,0.3);color:var(--gold)"></span>
        <button class="ctrl-btn" onclick="refreshPicks()" id="refresh-btn">↺ Refresh</button>
        <button class="picks-gen-btn" onclick="generatePicks()" id="gen-picks-btn">⚡ Generate Picks</button>
      </div>
    </div>
    <div id="picks-content" style="margin-top:20px">
      <div class="picks-empty">
        <strong>No picks generated yet today</strong>
        Click "Generate Picks" to build today's 3 slips (Safe 2x · Standard 3x · Bold 5x).<br>
        <span style="font-size:12px">Each slip uses 4-6 high-probability legs researched with live web data.</span>
      </div>
    </div>
  </div>

  <!-- STAT CARDS -->
  <div class="stats-row">
    <div class="stat-card sc-green">
      <div class="stat-label">Cash Reserves</div>
      <div class="stat-value val-green" id="balance">$0.00</div>
      <div class="stat-sub" id="balance-tier">Loading...</div>
    </div>
    <div class="stat-card sc-blue">
      <div class="stat-label">Today P&L</div>
      <div class="stat-value" id="today-pnl">+$0.00</div>
      <div class="stat-sub" id="today-slips">0 slips today</div>
    </div>
    <div class="stat-card sc-gold">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value val-gold" id="win-rate">0.0%</div>
      <div class="stat-sub" id="wl-record">0W / 0L</div>
    </div>
    <div class="stat-card sc-purple">
      <div class="stat-label">All-Time P&L</div>
      <div class="stat-value" id="alltime-pnl">+$0.00</div>
      <div class="stat-sub" id="alltime-slips">0 all-time slips</div>
    </div>
  </div>

  <!-- PORTFOLIO CHART -->
  <div class="chart-card">
    <div class="card-hdr">
      <div class="card-title">
        <span class="card-dot" style="background:var(--green)"></span>
        Portfolio Growth
      </div>
      <div class="toggle-row">
        <button class="tog-btn active" onclick="loadChart('1d',this)">1D</button>
        <button class="tog-btn" onclick="loadChart('7d',this)">7D</button>
        <button class="tog-btn" onclick="loadChart('30d',this)">1M</button>
        <button class="tog-btn" onclick="loadChart('all',this)">ALL</button>
      </div>
    </div>
    <canvas id="portfolioChart" height="160"></canvas>
  </div>

  <!-- ROLLOVER + RISK -->
  <div class="two-col">

    <!-- ROLLOVER TRACKER -->
    <div class="rollover-card">
      <div class="card-hdr" style="margin-bottom:0">
        <div class="card-title">
          <span class="card-dot" style="background:var(--purple)"></span>
          Active Rollover
        </div>
        <button class="ctrl-btn" onclick="showCalc()" style="font-size:11px;padding:5px 12px">+ New Calculator</button>
      </div>
      <div id="rollover-content">
        <div class="rollover-empty">No active rollover<br><span style="font-size:11px">Use the calculator below to start one</span></div>
      </div>
    </div>

    <!-- RISK STATUS -->
    <div class="risk-card">
      <div class="card-title" style="margin-bottom:14px">
        <span class="card-dot" style="background:var(--red)"></span>
        Risk Status
      </div>
      <div class="risk-row">
        <span class="risk-label">Today's Loss</span>
        <span class="risk-value rv-green" id="today-loss">$0.00</span>
      </div>
      <div class="risk-row">
        <span class="risk-label">Next Generation</span>
        <span class="risk-value rv-gold" id="next-gen">--:--</span>
      </div>
      <div class="risk-row">
        <span class="risk-label">Circuit Breaker</span>
        <span class="risk-value rv-green" id="cb-status">Inactive</span>
      </div>
      <div class="cb-history">
        <div style="font-size:10px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;font-weight:600">Circuit Breaker History</div>
        <div id="cb-history-list"></div>
      </div>
    </div>
  </div>

  <!-- ROLLOVER CALCULATOR -->
  <div class="calc-card" id="calc-section" style="display:none">
    <div class="card-hdr" style="margin-bottom:16px">
      <div class="card-title">
        <span class="card-dot" style="background:var(--gold)"></span>
        Rollover Calculator
      </div>
      <button class="ctrl-btn" onclick="hideCalc()" style="font-size:11px">✕ Close</button>
    </div>
    <div class="calc-grid">
      <div class="calc-field">
        <label class="calc-label">Days (max 10)</label>
        <input class="calc-input" type="number" id="calc-days" value="5" min="1" max="10" oninput="calcRollover()">
      </div>
      <div class="calc-field">
        <label class="calc-label">Odds per day (max 5x)</label>
        <input class="calc-input" type="number" id="calc-odds" value="3.0" min="1.1" max="5.0" step="0.1" oninput="calcRollover()">
      </div>
      <div class="calc-field">
        <label class="calc-label">Starting stake ($)</label>
        <input class="calc-input" type="number" id="calc-stake" value="10" min="1" oninput="calcRollover()">
      </div>
    </div>
    <div class="calc-error" id="calc-error"></div>
    <div class="calc-results" id="calc-results">
      <div class="calc-summary">
        <div class="calc-sum-item">
          <div class="calc-sum-label">Final Payout</div>
          <div class="calc-sum-value" id="calc-final">$0</div>
        </div>
        <div class="calc-sum-item">
          <div class="calc-sum-label">Total Profit</div>
          <div class="calc-sum-value" id="calc-profit">$0</div>
        </div>
        <div class="calc-sum-item">
          <div class="calc-sum-label">Completion Prob</div>
          <div class="calc-sum-value" id="calc-prob">0%</div>
        </div>
      </div>
      <table class="calc-table">
        <thead><tr><th>Day</th><th>Stake</th><th>Payout</th><th>Profit</th></tr></thead>
        <tbody id="calc-table-body"></tbody>
      </table>
      <div class="reality-check" id="calc-reality"></div>
      <div class="calc-actions">
        <button class="calc-calc-btn" onclick="calcRollover()">Recalculate</button>
        <button class="calc-continue-btn" id="calc-start-btn" onclick="startRollover()">Continue → Start Rollover</button>
      </div>
    </div>
  </div>

  <!-- RECENT ACTIVITY -->
  <div class="activity-card">
    <div class="activity-hdr">
      <div class="card-title">
        <span class="card-dot" style="background:var(--gold)"></span>
        Recent Activity
      </div>
      <div class="activity-filters">
        <button class="af-btn active" onclick="filterSlips('all',this)">All</button>
        <button class="af-btn" onclick="filterSlips('DAILY',this)">🏀 Daily</button>
        <button class="af-btn" onclick="filterSlips('ROLLOVER',this)">🔄 Rollover</button>
        <button class="af-btn" onclick="filterSlips('LOTTO',this)">🎰 Lotto</button>
        <button class="af-btn" onclick="filterSlips('WON',this)">Wins</button>
        <button class="af-btn" onclick="filterSlips('LOST',this)">Losses</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="activity-table">
        <thead>
          <tr>
            <th style="width:40px"></th>
            <th>Type</th>
            <th>Games</th>
            <th>Odds</th>
            <th>Stake</th>
            <th>Status</th>
            <th>Conf</th>
            <th>Finish</th>
            <th>P&L</th>
          </tr>
        </thead>
        <tbody id="activity-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- BACKTESTER -->
  <div class="backtest-card">
    <div class="card-hdr" style="margin-bottom:18px">
      <div class="card-title">
        <span class="card-dot" style="background:var(--purple)"></span>
        Strategy Backtester
      </div>
      <div class="bt-run-row">
        <select id="bt-days" class="bt-days-select">
          <option value="3">3 days</option>
          <option value="7" selected>7 days</option>
          <option value="14">14 days</option>
        </select>
        <button class="ctrl-btn" id="bt-run-btn" onclick="runBacktest()" style="border-color:var(--purple);color:var(--purple)">🔬 Run Backtest</button>
        <span id="bt-status" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>
    <div id="bt-results">
      <div style="text-align:center;padding:24px;color:var(--muted);font-size:13px">
        No backtest run yet — click Run Backtest to simulate the past week's picks
      </div>
    </div>
  </div>

  <!-- SYNTHETIC SIMULATOR -->
  <div class="card" style="margin-bottom:24px">
    <div class="card-hdr" style="margin-bottom:18px">
      <div class="card-title">
        <span class="card-dot" style="background:var(--gold)"></span>
        Synthetic Game Simulator
      </div>
      <div class="bt-run-row">
        <select id="sim-difficulty" class="bt-days-select">
          <option value="easy">Easy (high conf)</option>
          <option value="mixed" selected>Mixed</option>
          <option value="hard">Hard (low conf)</option>
        </select>
        <select id="sim-days" class="bt-days-select">
          <option value="5">5 days</option>
          <option value="10" selected>10 days</option>
          <option value="14">14 days</option>
        </select>
        <button class="ctrl-btn" id="sim-run-btn" onclick="runSimulation()" style="border-color:var(--gold);color:var(--gold)">🎮 Run Simulation</button>
        <span id="sim-status" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>
    <div id="sim-results">
      <div style="text-align:center;padding:24px;color:var(--muted);font-size:13px">
        No simulation run yet — pick a difficulty and click Run Simulation
      </div>
    </div>
  </div>

  <!-- CONTROLS -->
  <div class="controls-card">
    <div class="card-title" style="margin-bottom:16px">
      <span class="card-dot" style="background:var(--blue)"></span>
      Controls
    </div>
    <div class="controls-grid">
      <button class="control-btn" onclick="togglePause()">⏸ Pause / Resume</button>
      <button class="control-btn success" onclick="generateNow()">⚡ Generate Slips Now</button>
      <button class="control-btn danger" onclick="resetCB()">🔄 Reset Circuit Breaker</button>
      <button class="control-btn danger" onclick="resetBalance()">💰 Reset Balance (Paper)</button>
      <button class="control-btn" onclick="showCalc()">📊 Rollover Calculator</button>
      <button class="control-btn" onclick="location.reload()">🔃 Refresh Dashboard</button>
    </div>
  </div>

</div><!-- /main -->
</div><!-- /wrap -->

<!-- SLIP DETAIL MODAL -->
<div class="modal-overlay" id="slip-modal" onclick="closeModal(event)">
  <div class="modal" id="modal-content">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div id="modal-body"></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<script>
let allSlips = [];
let currentFilter = 'all';
let portfolioChart = null;
let lastCalcResult = null;

// ── STATUS POLLING ────────────────────────────────────────────────────────────
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    
    // Balance
    const bal = d.balance || 0;
    document.getElementById('balance').textContent = '$' + bal.toFixed(2);
    
    // Tier
    let tier = 'Early Stage';
    if (bal >= 10000) tier = 'Elite';
    else if (bal >= 1000) tier = 'Growth';
    else if (bal >= 500) tier = 'Building';
    document.getElementById('balance-tier').textContent = tier + ' tier';
    
    // Today P&L
    const pnl = d.today_pnl || 0;
    const pnlEl = document.getElementById('today-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'val-green' : 'val-red');
    document.getElementById('today-slips').textContent = (d.today_slips || 0) + ' slips today';
    
    // Win rate
    const wr = (d.win_rate || 0) * 100;
    document.getElementById('win-rate').textContent = wr.toFixed(1) + '%';
    document.getElementById('wl-record').textContent = (d.wins||0) + 'W / ' + (d.losses||0) + 'L';
    
    // All-time P&L
    const atpnl = d.all_time_pnl || 0;
    const atEl = document.getElementById('alltime-pnl');
    atEl.textContent = (atpnl >= 0 ? '+' : '') + '$' + Math.abs(atpnl).toFixed(2);
    atEl.className = 'stat-value ' + (atpnl >= 0 ? 'val-green' : 'val-red');
    document.getElementById('alltime-slips').textContent = (d.all_time_slips||0) + ' all-time slips';
    
    // Status
    const statusEl = document.getElementById('status-text');
    if (d.cb_active) {
      statusEl.textContent = 'CIRCUIT BREAKER';
      document.getElementById('cb-banner').classList.add('active');
      document.getElementById('cb-status').textContent = 'ACTIVE';
      document.getElementById('cb-status').className = 'risk-value rv-red';
    } else if (d.bot_paused) {
      statusEl.textContent = 'PAUSED';
      document.getElementById('cb-banner').classList.remove('active');
    } else {
      statusEl.textContent = 'ACTIVE';
      document.getElementById('cb-banner').classList.remove('active');
      document.getElementById('cb-status').textContent = 'Inactive (WR ' + wr.toFixed(1) + '%)';
      document.getElementById('cb-status').className = 'risk-value rv-green';
    }
    
    // Next generation
    if (d.next_generation) {
      document.getElementById('next-gen').textContent = d.next_generation;
    }
    
    // Today's loss
    const todayLoss = Math.min(0, d.today_pnl || 0);
    const lossEl = document.getElementById('today-loss');
    lossEl.textContent = todayLoss < 0 ? '-$' + Math.abs(todayLoss).toFixed(2) : '$0.00';
    lossEl.className = 'risk-value ' + (todayLoss < 0 ? 'rv-red' : 'rv-green');
    
  } catch(e) { console.error('Status fetch error:', e); }
}

// ── SLIPS ─────────────────────────────────────────────────────────────────────
async function fetchSlips() {
  try {
    const r = await fetch('/api/slips?limit=50');
    const d = await r.json();
    allSlips = d.slips || [];
    renderSlips(currentFilter);
  } catch(e) {}
}

function filterSlips(f, btn) {
  currentFilter = f;
  document.querySelectorAll('.af-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderSlips(f);
}

function renderSlips(filter) {
  let slips = [...allSlips];
  if (filter !== 'all') {
    if (['DAILY','ROLLOVER','LOTTO'].includes(filter)) {
      slips = slips.filter(s => s.slip_type === filter);
    } else if (filter === 'WON') {
      slips = slips.filter(s => s.status === 'WON');
    } else if (filter === 'LOST') {
      slips = slips.filter(s => s.status === 'LOST');
    }
  }
  
  function getSlipIcon(slip) {
    const sports = (slip.sport_mix || '').toUpperCase();
    if (slip.slip_type === 'ROLLOVER') return '🔄';
    if (slip.slip_type === 'LOTTO') return '🎰';
    if (sports.includes('NFL')) return '🏈';
    if (sports.includes('NCAAFB')) return '🏈';
    if (sports.includes('NBA')) return '🏀';
    if (sports.includes('NCAAMB')) return '🏀';
    if (sports.includes('MLB')) return '⚾';
    if (sports.includes('NHL')) return '🏒';
    if (sports.includes('TENNIS')) return '🎾';
    if (sports.includes('SOCCER')) return '⚽';
    return '📋';
  }
  const iconBg = { DAILY:'si-daily', ROLLOVER:'si-rollover', LOTTO:'si-lotto' };
  const typeBadge = { DAILY:'stb-daily', ROLLOVER:'stb-rollover', LOTTO:'stb-lotto' };
  
  const html = slips.map(s => {
    const pnl = s.net_pnl || 0;
    const conf = s.confidence || 0;
    const confColor = conf >= 8 ? 'var(--green)' : conf >= 6 ? 'var(--gold)' : 'var(--red)';
    
    let statusBadge = '';
    if (s.status === 'PENDING') statusBadge = '<span class="status-badge sb-open">OPEN</span>';
    else if (s.status === 'WON') statusBadge = '<span class="status-badge sb-won">WIN</span>';
    else statusBadge = '<span class="status-badge sb-lost">LOSS</span>';
    
    const sports = (s.sport_mix || '').split(',');
    const sportPillClass = { NBA:'sp-nba', MLB:'sp-mlb', SOCCER:'sp-soccer', NHL:'sp-nhl', NFL:'sp-nfl', NCAAFB:'sp-nfl', NCAAMB:'sp-nba', TENNIS:'sp-tennis' };
    const sportPills = sports.map(sp => {
      const key = sp.trim().toUpperCase();
      const cls = sportPillClass[key] || 'sp-nba';
      return `<span class="sport-pill ${cls}">${sp.trim()}</span>`;
    }).join(' ');
    
    let rolloverInfo = '';
    if (s.slip_type === 'ROLLOVER' && s.rollover_day) {
      rolloverInfo = ` <span style="font-size:10px;color:var(--muted)">Day ${s.rollover_day}</span>`;
    }
    
    return `<tr onclick="openSlipModal(${s.id})">
      <td><div class="slip-icon ${iconBg[s.slip_type] || 'si-daily'}">${getSlipIcon(s)}</div></td>
      <td>
        <span class="slip-type-badge ${typeBadge[s.slip_type] || 'stb-daily'}">${s.slip_type}</span>
        ${rolloverInfo}
      </td>
      <td>${sportPills} <span style="font-size:11px;color:var(--muted)">${s.leg_count || 0} legs</span></td>
      <td class="mono" style="color:var(--blue)">${(s.combined_odds||0).toFixed(2)}x</td>
      <td class="mono">$${(s.stake||0).toFixed(2)}</td>
      <td>${statusBadge}</td>
      <td>
        <div class="conf-bar">
          <span class="mono" style="color:${confColor}">${conf.toFixed(1)}</span>
          <div class="conf-fill-wrap"><div class="conf-fill" style="width:${conf*10}%;background:${confColor}"></div></div>
        </div>
      </td>
      <td style="font-size:11px;color:var(--muted)">${formatFinish(s.projected_finish)}</td>
      <td class="${pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${s.status !== 'PENDING' ? (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2) : '—'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:32px">No slips yet — generate your first slip</td></tr>';
  
  document.getElementById('activity-tbody').innerHTML = html;
}

// ── SLIP MODAL ────────────────────────────────────────────────────────────────
async function openSlipModal(slipId) {
  const slip = allSlips.find(s => s.id === slipId);
  if (!slip) return;
  
  const r = await fetch(`/api/slips/${slipId}/legs`);
  const d = await r.json();
  const legs = d.legs || [];
  
  const sportIcon = { NBA:'🏀', NFL:'🏈', MLB:'⚾', NHL:'🏒', NCAAFB:'🏈', NCAAMB:'🏀', TENNIS:'🎾', SOCCER:'⚽' };
  
  const legsHtml = legs.map(leg => {
    const statusClass = leg.status === 'WON' ? 'ls-won' : leg.status === 'LOST' ? 'ls-lost' : 'ls-pending';
    return `<div class="leg-card">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span>${sportIcon[leg.sport] || '🎯'}</span>
        <span class="leg-game">${leg.game}</span>
        <span class="sport-pill sp-${leg.sport?.toLowerCase()}">${leg.sport}</span>
        <span class="leg-status ${statusClass}" style="margin-left:auto">${leg.status}</span>
      </div>
      <div class="leg-pick">${leg.pick}</div>
      ${leg.original_line !== leg.calibrated_line && leg.original_line ? 
        `<div class="leg-original">Original: ${leg.original_line}</div>` : ''}
      <div class="leg-meta">
        <span>Odds: <strong style="color:var(--blue)">${(leg.individual_odds||0).toFixed(2)}x</strong></span>
        <span>Confidence: <strong style="color:var(--gold)">${(leg.confidence||0).toFixed(1)}/10</strong></span>
        <span>Type: ${leg.market_type}</span>
        ${leg.game_start ? `<span>Game: ${formatTime(leg.game_start)}</span>` : ''}
      </div>
      ${leg.ai_reasoning ? `<div class="leg-reasoning">💡 ${leg.ai_reasoning}</div>` : ''}
    </div>`;
  }).join('');
  
  document.getElementById('modal-body').innerHTML = `
    <div class="modal-title">${slip.slip_type} SLIP #${slip.id}</div>
    <div class="modal-subtitle">
      ${slip.combined_odds?.toFixed(2)}x combined odds · 
      $${slip.stake?.toFixed(2)} stake · 
      Confidence: ${slip.confidence?.toFixed(1)}/10 ·
      Potential: $${slip.potential_payout?.toFixed(2)}
    </div>
    <div style="margin-bottom:16px">${legsHtml}</div>
    <div style="font-size:11px;color:var(--muted)">Generated: ${formatTime(slip.created_at)}</div>
  `;
  
  document.getElementById('slip-modal').classList.add('show');
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('slip-modal')) {
    document.getElementById('slip-modal').classList.remove('show');
  }
}

// ── ROLLOVER ──────────────────────────────────────────────────────────────────
async function fetchRollover() {
  try {
    const r = await fetch('/api/rollover/active');
    const d = await r.json();
    renderRollover(d.rollover);
  } catch(e) {}
}

function renderRollover(ro) {
  const el = document.getElementById('rollover-content');
  if (!ro) {
    el.innerHTML = '<div class="rollover-empty">No active rollover<br><span style="font-size:11px">Use the calculator below to start one</span></div>';
    return;
  }
  
  const days_json = JSON.parse(ro.days_json || '[]');
  const totalDays = ro.target_days;
  const currentDay = ro.current_day;
  
  // Build progress dots
  let dotsHtml = '';
  for (let i = 1; i <= totalDays; i++) {
    const dayResult = days_json.find(d => d.day === i);
    let dotClass = '';
    let dotContent = i;
    
    if (dayResult) {
      dotClass = dayResult.won ? 'won' : 'lost';
      dotContent = dayResult.won ? '✓' : '✗';
    } else if (i === currentDay) {
      dotClass = 'current';
      dotContent = i;
    }
    
    const lineClass = dayResult?.won ? 'won' : '';
    
    dotsHtml += `<div class="ro-day">
      <div class="ro-dot ${dotClass}">${dotContent}</div>
      <div class="ro-day-label">D${i}</div>
    </div>`;
    if (i < totalDays) dotsHtml += `<div class="ro-line ${lineClass}"></div>`;
  }
  
  const projectedFinal = ro.starting_stake * Math.pow(ro.target_odds, totalDays);
  const daysLeft = totalDays - currentDay + 1;
  
  el.innerHTML = `
    <div class="rollover-header">
      <div class="rollover-title">${totalDays}-Day ${ro.target_odds}x Rollover</div>
      <div class="rollover-stake">Current stake: $${(ro.current_stake||0).toFixed(2)}</div>
    </div>
    <div class="rollover-progress">${dotsHtml}</div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:8px">Day ${currentDay} of ${totalDays} · ${daysLeft} day${daysLeft !== 1 ? 's' : ''} remaining</div>
    <div class="rollover-payout">
      <div>
        <div class="ro-payout-label">Projected Final Payout</div>
        <div style="font-size:11px;color:var(--muted)">(if all remaining days win)</div>
      </div>
      <div class="ro-payout-value">$${projectedFinal.toFixed(2)}</div>
    </div>
  `;
}

// ── CB HISTORY ────────────────────────────────────────────────────────────────
async function fetchStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    const history = d.circuit_breaker_history || [];
    
    const html = history.map(cb => `
      <div class="cb-event">
        <div style="flex:1">
          <span style="color:var(--muted);font-size:10px">${formatTime(cb.triggered_at)}</span>
          <span class="cb-wr">WR ${((cb.win_rate||0)*100).toFixed(1)}%</span>
        </div>
        <span class="cb-how">${cb.manually_reset ? 'Manual reset' : 'Auto-expired'}</span>
      </div>
    `).join('') || '<div style="font-size:11px;color:var(--muted)">No circuit breaker events</div>';
    
    document.getElementById('cb-history-list').innerHTML = html;
  } catch(e) {}
}

// ── PORTFOLIO CHART ───────────────────────────────────────────────────────────
async function loadChart(period, btn) {
  document.querySelectorAll('.tog-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  
  const days = {
    '1d': 1, '7d': 7, '30d': 30, 'all': 365
  }[period] || 30;
  
  try {
    const r = await fetch(`/api/portfolio?days=${days}`);
    const d = await r.json();
    const history = d.history || [];
    
    const ctx = document.getElementById('portfolioChart').getContext('2d');
    if (portfolioChart) portfolioChart.destroy();
    
    const labels = history.map(p => {
      const dt = new Date(p.timestamp);
      return dt.toLocaleDateString('en-US', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
    });
    
    portfolioChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Daily',
            data: history.map(p => p.cumulative_pnl_daily || 0),
            borderColor: '#00e87a',
            backgroundColor: 'rgba(0,232,122,0.05)',
            borderWidth: 2,
            fill: false,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: 'Rollover',
            data: history.map(p => p.cumulative_pnl_rollover || 0),
            borderColor: '#a855f7',
            backgroundColor: 'rgba(168,85,247,0.05)',
            borderWidth: 2,
            fill: false,
            tension: 0.3,
            pointRadius: 0,
          },
          {
            label: 'Lotto',
            data: history.map(p => p.cumulative_pnl_lotto || 0),
            borderColor: '#ffc820',
            backgroundColor: 'rgba(255,200,32,0.05)',
            borderWidth: 2,
            fill: false,
            tension: 0.3,
            pointRadius: 0,
          },
        ]
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { color: '#4a6080', font: { size: 11 } } },
          tooltip: {
            backgroundColor: '#0d1320',
            borderColor: '#1a2840',
            borderWidth: 1,
            callbacks: { label: c => ` ${c.dataset.label}: ${c.parsed.y >= 0 ? '+' : ''}$${c.parsed.y.toFixed(2)}` }
          }
        },
        scales: {
          x: { grid: { color: 'rgba(26,40,64,0.6)' }, ticks: { color: '#4a6080', font: { size: 10 }, maxTicksLimit: 8 } },
          y: { grid: { color: 'rgba(26,40,64,0.6)' }, ticks: { color: '#4a6080', font: { size: 10 }, callback: v => '$' + v.toFixed(0) } }
        }
      }
    });
  } catch(e) {}
}

// ── CALCULATOR ────────────────────────────────────────────────────────────────
function showCalc() {
  document.getElementById('calc-section').style.display = 'block';
  document.getElementById('calc-section').scrollIntoView({ behavior: 'smooth' });
  calcRollover();
}

function hideCalc() {
  document.getElementById('calc-section').style.display = 'none';
}

async function calcRollover() {
  const days = parseInt(document.getElementById('calc-days').value) || 5;
  const odds = parseFloat(document.getElementById('calc-odds').value) || 3.0;
  const stake = parseFloat(document.getElementById('calc-stake').value) || 10;
  
  try {
    const r = await fetch('/api/rollover/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, odds_per_day: odds, starting_stake: stake })
    });
    const d = await r.json();
    lastCalcResult = d;
    
    const errEl = document.getElementById('calc-error');
    const resEl = document.getElementById('calc-results');
    const startBtn = document.getElementById('calc-start-btn');
    
    if (!d.valid) {
      errEl.textContent = (d.errors || ['Invalid rollover']).join(' · ');
      errEl.classList.add('show');
      resEl.classList.remove('show');
      startBtn.classList.remove('show');
      return;
    }
    
    errEl.classList.remove('show');
    resEl.classList.add('show');
    startBtn.classList.add('show');
    
    document.getElementById('calc-final').textContent = '$' + d.final_payout?.toFixed(2);
    document.getElementById('calc-profit').textContent = '$' + d.total_profit?.toFixed(2);
    document.getElementById('calc-prob').textContent = d.completion_probability + '%';
    
    // Daily breakdown table
    const tbody = document.getElementById('calc-table-body');
    tbody.innerHTML = (d.daily_breakdown || []).map(day => `
      <tr>
        <td>Day ${day.day}</td>
        <td>$${day.stake.toFixed(2)}</td>
        <td style="color:var(--green)">$${day.payout.toFixed(2)}</td>
        <td style="color:var(--green)">+$${day.profit.toFixed(2)}</td>
      </tr>
    `).join('');
    
    // Reality check
    const rc = d.reality_check || {};
    const rcClass = { GOOD: 'rc-good', MODERATE: 'rc-moderate', AGGRESSIVE: 'rc-aggressive', LOTTERY: 'rc-lottery' }[rc.level] || 'rc-moderate';
    document.getElementById('calc-reality').className = 'reality-check ' + rcClass;
    document.getElementById('calc-reality').textContent = rc.message || '';
    
  } catch(e) {
    console.error('Calc error:', e);
  }
}

async function startRollover() {
  if (!lastCalcResult?.valid) return;
  
  const days = parseInt(document.getElementById('calc-days').value);
  const odds = parseFloat(document.getElementById('calc-odds').value);
  const stake = parseFloat(document.getElementById('calc-stake').value);
  
  const confirmed = confirm(
    `Start a ${days}-day rollover at ${odds}x odds per day?\\n` +
    `Starting stake: $${stake.toFixed(2)}\\n` +
    `Projected payout: $${lastCalcResult.final_payout?.toFixed(2)}\\n` +
    `Completion probability: ${lastCalcResult.completion_probability}%\\n\\n` +
    `The first slip will be generated at the next scheduled time.`
  );
  
  if (!confirmed) return;
  
  try {
    const r = await fetch('/api/rollover/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, odds_per_day: odds, starting_stake: stake })
    });
    const d = await r.json();
    
    if (d.error) {
      alert('Error: ' + (Array.isArray(d.error) ? d.error.join('\\n') : d.error));
      return;
    }
    
    hideCalc();
    fetchRollover();
    alert('Rollover started! First slip will be generated tonight.');
  } catch(e) {
    alert('Failed to start rollover: ' + e.message);
  }
}

// ── CONTROLS ──────────────────────────────────────────────────────────────────
async function togglePause() {
  await fetch('/api/pause', { method: 'POST' });
  fetchStatus();
}

async function generateNow() {
  if (!confirm('Generate slips now? This will use real API calls.')) return;
  const btn = event.target;
  btn.textContent = '⏳ Generating...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/generate', { method: 'POST' });
    const d = await r.json();
    btn.textContent = '⚡ Generate Now';
    btn.disabled = false;
    fetchSlips();
    alert('Generation complete! Check Recent Activity.');
  } catch(e) {
    btn.textContent = '⚡ Generate Now';
    btn.disabled = false;
    alert('Generation failed: ' + e.message);
  }
}

async function resetCB() {
  if (!confirm('Reset circuit breaker and resume trading?')) return;
  await fetch('/api/reset-circuit-breaker', { method: 'POST' });
  fetchStatus();
}

async function resetBalance() {
  if (!confirm('Reset balance to starting amount? (Paper mode only)')) return;
  await fetch('/api/reset-balance', { method: 'POST' });
  fetchStatus();
}

// ── BACKTESTER ────────────────────────────────────────────────────────────────
let btPollTimer = null;

async function runBacktest() {
  const days = parseInt(document.getElementById('bt-days').value) || 7;
  const btn  = document.getElementById('bt-run-btn');
  const stat = document.getElementById('bt-status');

  if (!confirm(`Run a ${days}-day backtest? This will make AI calls for each settled game.`)) return;

  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  stat.textContent = '';

  try {
    const r = await fetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days }),
    });
    const d = await r.json();

    if (d.error) {
      stat.textContent = '❌ ' + d.error;
      btn.disabled = false;
      btn.textContent = '🔬 Run Backtest';
      return;
    }

    btn.textContent = '⏳ Running…';
    stat.textContent = `${days}-day backtest running in background — results appear below (~5-15 min)`;
    _btStartPolling();

  } catch(e) {
    stat.textContent = '❌ ' + e.message;
    btn.disabled = false;
    btn.textContent = '🔬 Run Backtest';
  }
}

function _btStartPolling() {
  if (btPollTimer) clearInterval(btPollTimer);
  btPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/backtest/results');
      const d = await r.json();
      if (!d.running && d.result) {
        clearInterval(btPollTimer);
        btPollTimer = null;
        _renderBacktestResults(d.result);
        document.getElementById('bt-run-btn').disabled = false;
        document.getElementById('bt-run-btn').textContent = '🔬 Run Backtest';
        document.getElementById('bt-status').textContent = '✅ Complete';
      }
    } catch(e) {}
  }, 20000);  // poll every 20 seconds
}

function _renderBacktestResults(res) {
  const report = res.full_report || {};
  const days   = report.daily_breakdown || [];

  const pnlColor = (report.total_pnl || 0) >= 0 ? 'var(--green)' : 'var(--red)';
  const pnlSign  = (report.total_pnl || 0) >= 0 ? '+' : '';

  const summary = `
    <div class="bt-summary">
      <div class="bt-sum-item">
        <div class="bt-sum-label">Slip Win Rate</div>
        <div class="bt-sum-value" style="color:${(report.slip_win_rate||0)>=50?'var(--green)':'var(--red)'}">${report.slip_win_rate||0}%</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${report.winning_slips||0}/${report.total_slips||0} slips</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">Leg Win Rate</div>
        <div class="bt-sum-value" style="color:${(report.leg_win_rate||0)>=50?'var(--green)':'var(--gold)'}">${report.leg_win_rate||0}%</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${report.winning_legs||0}/${report.total_legs||0} legs</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">Total P&amp;L</div>
        <div class="bt-sum-value" style="color:${pnlColor}">${pnlSign}$${Math.abs(report.total_pnl||0).toFixed(2)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">$100 → $${(report.ending_balance||0).toFixed(2)}</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">Period</div>
        <div class="bt-sum-value" style="font-size:18px;color:var(--blue)">${report.period_days||'?'}d</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${report.period||''}</div>
      </div>
    </div>`;

  const rows = days.map(day => {
    const legRows = (day.legs||[]).map(leg => {
      const wonCls  = leg.won ? 'bt-leg-won' : 'bt-leg-lost';
      const wonIcon = leg.won ? '✓' : '✗';
      return `<div class="bt-leg ${wonCls}">${wonIcon} ${leg.pick||leg.game||''} <span style="font-family:'Space Mono'">${(leg.odds||0).toFixed(2)}x</span> → actual: ${leg.actual_result||'?'}</div>`;
    }).join('');

    const pnl     = day.pnl || 0;
    const pnlCls  = pnl >= 0 ? 'bt-win' : 'bt-loss';
    const pnlTxt  = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);

    return `<tr>
      <td style="font-family:'Space Mono';font-size:12px;color:var(--muted)">${day.date||''}</td>
      <td>${legRows || '<span style="color:var(--muted);font-size:11px">no legs</span>'}</td>
      <td style="font-family:'Space Mono';font-size:12px">${(day.combined_odds||0).toFixed(2)}x</td>
      <td style="font-family:'Space Mono';font-size:12px">$${(day.stake||0).toFixed(2)}</td>
      <td class="${pnlCls}">${pnlTxt}</td>
      <td style="font-family:'Space Mono';font-size:12px;color:var(--muted)">$${(day.balance_after||0).toFixed(2)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:20px">No daily data</td></tr>';

  const runAt = report.run_at ? new Date(report.run_at).toLocaleString() : '';

  document.getElementById('bt-results').innerHTML = summary + `
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">Run at: ${runAt}</div>
    <div style="overflow-x:auto">
      <table class="bt-day-table">
        <thead><tr><th>Date</th><th>Picks</th><th>Odds</th><th>Stake</th><th>P&L</th><th>Balance</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// Auto-load any existing backtest result on page load
async function fetchBacktestResults() {
  try {
    const r = await fetch('/api/backtest/results');
    const d = await r.json();
    if (d.result) _renderBacktestResults(d.result);
    if (d.running) {
      document.getElementById('bt-run-btn').disabled = true;
      document.getElementById('bt-run-btn').textContent = '⏳ Running…';
      document.getElementById('bt-status').textContent = 'Backtest in progress…';
      _btStartPolling();
    }
  } catch(e) {}
}

// ── SIMULATOR ────────────────────────────────────────────────────────────────
let simPollTimer = null;

async function runSimulation() {
  const difficulty = document.getElementById('sim-difficulty').value;
  const days       = parseInt(document.getElementById('sim-days').value) || 10;
  const btn        = document.getElementById('sim-run-btn');
  const stat       = document.getElementById('sim-status');

  if (!confirm(`Run a ${days}-day ${difficulty} synthetic simulation? This uses AI calls (~${days*2} min).`)) return;

  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  stat.textContent = '';

  try {
    const r = await fetch('/api/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, difficulty, starting_balance: 100.0 }),
    });
    const d = await r.json();

    if (d.error) {
      stat.textContent = '❌ ' + d.error;
      btn.disabled = false;
      btn.textContent = '🎮 Run Simulation';
      return;
    }

    btn.textContent = '⏳ Running…';
    stat.textContent = `${days}-day ${difficulty} simulation running (~${days*2} min)…`;
    _simStartPolling();

  } catch(e) {
    stat.textContent = '❌ ' + e.message;
    btn.disabled = false;
    btn.textContent = '🎮 Run Simulation';
  }
}

function _simStartPolling() {
  if (simPollTimer) clearInterval(simPollTimer);
  simPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/simulate/results');
      const d = await r.json();
      if (!d.running && d.result) {
        clearInterval(simPollTimer);
        simPollTimer = null;
        _renderSimResults(d.result);
        document.getElementById('sim-run-btn').disabled = false;
        document.getElementById('sim-run-btn').textContent = '🎮 Run Simulation';
        document.getElementById('sim-status').textContent = '✅ Complete';
      }
    } catch(e) {}
  }, 20000);
}

function _renderSimResults(res) {
  if (res.error) {
    document.getElementById('sim-results').innerHTML =
      `<div style="color:var(--red);padding:16px">&#10060; ${res.error}</div>`;
    return;
  }
  const report = res.report || {};
  const days   = report.daily_breakdown || [];

  const totalPnl  = report.total_pnl || 0;
  const pnlColor  = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
  const pnlSign   = totalPnl >= 0 ? '+' : '';
  const diff      = res.difficulty || '?';
  const diffColor = diff === 'easy' ? 'var(--green)' : diff === 'hard' ? 'var(--red)' : 'var(--gold)';

  const roMaxDay  = report.rollover_days_won || 0;
  const roComp    = report.rollover_completed;
  const roStatus  = roComp
    ? '<span style="color:var(--green)">COMPLETED</span>'
    : roMaxDay > 0
      ? `<span style="color:var(--gold)">${roMaxDay}/5 days won</span>`
      : '<span style="color:var(--muted)">not started</span>';

  const summary = `
    <div class="bt-summary">
      <div class="bt-sum-item">
        <div class="bt-sum-label">Daily Win Rate</div>
        <div class="bt-sum-value" style="color:${(report.daily_win_rate||0)>=50?'var(--green)':'var(--red)'}">${(report.daily_win_rate||0).toFixed(1)}%</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${report.daily_slips_won||0}/${report.daily_slips_total||0} slips won</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">Total P&amp;L</div>
        <div class="bt-sum-value" style="color:${pnlColor}">${pnlSign}$${Math.abs(totalPnl).toFixed(2)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">$${(res.starting_balance||100).toFixed(0)} to $${(report.ending_balance||0).toFixed(2)}</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">5-Day Rollover</div>
        <div class="bt-sum-value" style="font-size:15px">${roStatus}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">payout $${(report.rollover_payout||0).toFixed(2)}</div>
      </div>
      <div class="bt-sum-item">
        <div class="bt-sum-label">Difficulty</div>
        <div class="bt-sum-value" style="color:${diffColor};font-size:16px;text-transform:uppercase">${diff}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">${res.days||'?'}d progressive</div>
      </div>
    </div>`;

  function _diffBadge(d) {
    const c = d === 'easy' ? 'var(--green)' : d === 'hard' ? 'var(--red)' : 'var(--gold)';
    return `<span style="font-size:9px;font-weight:700;color:${c};border:1px solid ${c};padding:1px 5px;border-radius:3px;text-transform:uppercase;margin-right:4px">${d||'?'}</span>`;
  }

  const rows = days.map(day => {
    const dl  = day.daily || null;
    const ro  = day.rollover || null;
    const dif = day.difficulty || '?';
    const scenarios = (day.scenarios || []).filter(Boolean);

    // Day label + difficulty badge
    const dayLabel = `<div style="font-family:'Space Mono';font-size:11px;color:var(--muted)">${day.date||''}</div>
      <div style="margin-top:3px">${_diffBadge(dif)}</div>
      <div style="font-size:10px;color:var(--muted);margin-top:3px">${day.candidates_found||0} cands</div>`;

    // Rollover status cell
    let roCell = '';
    if (day.rollover_completed) {
      roCell = `<div style="font-size:10px;color:var(--green);font-weight:700">RO DONE!</div>`;
    } else if (day.rollover_active) {
      roCell = `<div style="font-size:10px;color:var(--purple)">RO Day ${day.rollover_day}/5</div>
        <div style="font-size:10px;color:var(--muted)">stake $${(day.rollover_stake||0).toFixed(2)}</div>`;
    }
    if (ro) {
      const roCls = ro.result === 'WON' ? 'color:var(--green)' : 'color:var(--red)';
      roCell += `<div style="font-size:10px;${roCls};font-weight:600">RO: ${ro.result}</div>`;
    }

    // Picks column — game title + pick + won/lost
    let picksHtml = '';
    if (dl && dl.legs && dl.legs.length) {
      picksHtml = dl.legs.map(leg => {
        const wonCls  = leg.won ? 'bt-leg-won' : 'bt-leg-lost';
        const wonIcon = leg.won ? '&#10003;' : '&#10007;';
        const game    = leg.game ? `<span style="color:var(--muted);font-size:10px">${leg.game}</span><br>` : '';
        const conf    = leg.confidence ? ` <span style="color:var(--muted);font-size:10px">[${Number(leg.confidence).toFixed(1)}]</span>` : '';
        return `<div class="bt-leg ${wonCls}" style="margin-bottom:3px">${game}${wonIcon} ${leg.pick||''}${conf}</div>`;
      }).join('');
    } else {
      picksHtml = `<span style="color:var(--muted);font-size:11px">no slip (${day.candidates_found||0} cands &lt; 2)</span>`;
    }

    // Scenario descriptions (first unique one)
    const scen = scenarios.length ? `<div style="font-size:10px;color:var(--muted);font-style:italic;margin-top:4px">${[...new Set(scenarios)].slice(0,2).join(' | ')}</div>` : '';

    // P&L
    const pnl    = dl ? (dl.pnl || 0) : null;
    const pnlTxt = pnl !== null ? ((pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2)) : '—';
    const pnlCls = pnl !== null ? (pnl >= 0 ? 'bt-win' : 'bt-loss') : '';

    const odds    = dl ? (dl.combined_odds || 0) : 0;
    const stake   = dl ? (dl.stake || 0) : 0;
    const balAfter = dl ? (dl.balance_after || day.balance || 0) : (day.balance || 0);

    return `<tr>
      <td>${dayLabel}</td>
      <td>${picksHtml}${scen}</td>
      <td style="vertical-align:top">${roCell}</td>
      <td style="font-family:'Space Mono';font-size:12px;vertical-align:top">${odds ? odds.toFixed(2)+'x' : '—'}</td>
      <td style="font-family:'Space Mono';font-size:12px;vertical-align:top">${stake ? '$'+stake.toFixed(2) : '—'}</td>
      <td class="${pnlCls}" style="vertical-align:top">${pnlTxt}</td>
      <td style="font-family:'Space Mono';font-size:12px;color:var(--muted);vertical-align:top">$${balAfter.toFixed(2)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">No daily data</td></tr>';

  const runAt = res.run_at ? new Date(res.run_at).toLocaleString() : '';

  document.getElementById('sim-results').innerHTML = summary + `
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">Run at: ${runAt}</div>
    <div style="overflow-x:auto">
      <table class="bt-day-table">
        <thead><tr><th>Day</th><th>Picks</th><th>Rollover</th><th>Odds</th><th>Stake</th><th>P&amp;L</th><th>Balance</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

async function fetchSimulationResults() {
  try {
    const r = await fetch('/api/simulate/results');
    const d = await r.json();
    if (d.result) _renderSimResults(d.result);
    if (d.running) {
      document.getElementById('sim-run-btn').disabled = true;
      document.getElementById('sim-run-btn').textContent = '⏳ Running…';
      document.getElementById('sim-status').textContent = 'Simulation in progress…';
      _simStartPolling();
    }
  } catch(e) {}
}

// ── TODAY'S PICKS ─────────────────────────────────────────────────────────────

async function generatePicks() {
  const btn = document.getElementById('gen-picks-btn');
  btn.textContent = '⏳ Generating...';
  btn.disabled = true;
  document.getElementById('picks-content').innerHTML =
    '<div style="text-align:center;padding:40px;color:var(--muted)">Researching markets with live web data...<br><span style="font-size:11px">Building 3 slips: Safe 2x · Standard 3x · Bold 5x</span></div>';
  try {
    const r = await fetch('/api/generate', {method:'POST'});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    renderPicksFromResult(d.result);
  } catch(e) {
    document.getElementById('picks-content').innerHTML =
      '<div style="color:var(--red);padding:20px;text-align:center">Error: ' + e.message + '</div>';
  } finally {
    btn.textContent = '⚡ Generate Picks';
    btn.disabled = false;
  }
}

async function refreshPicks() {
  try {
    const r = await fetch('/api/picks/today');
    const d = await r.json();
    if (d.slips && d.slips.length > 0) renderPicksFromDB(d.slips, d.date);
  } catch(e) { console.error('Refresh picks error:', e); }
}

function renderPicksFromResult(result) {
  if (!result || result.error) {
    document.getElementById('picks-content').innerHTML =
      '<div style="color:var(--red);padding:20px;text-align:center">' + (result?.error || 'Failed to generate') + '</div>';
    return;
  }
  const dateEl = document.getElementById('picks-date');
  const countEl = document.getElementById('picks-count');
  if (dateEl) dateEl.textContent = result.date || '';
  if (countEl) countEl.textContent = (result.markets_available || 0) + ' markets scanned';

  const slipDefs = [
    {key:'slip_2x', label:'SAFE',     color:'var(--green)', target:'~2x'},
    {key:'slip_3x', label:'STANDARD', color:'var(--blue)',  target:'~3x'},
    {key:'slip_5x', label:'BOLD',     color:'var(--gold)',  target:'~5x'},
  ];

  const html = slipDefs.map(({key, label, color, target}) => {
    const slip = result[key];
    if (!slip || slip.status === 'FAILED') {
      return `<div style="padding:14px;border:1px solid var(--border);border-radius:10px;margin-bottom:12px;opacity:0.6">
        <span style="color:${color};font-weight:700">${label} ${target}</span>
        <span style="color:var(--red);margin-left:12px;font-size:12px">Failed: ${slip?.error||'unknown'}</span>
      </div>`;
    }
    const sportIcon = s => ({NBA:'🏀',MLB:'⚾',NHL:'🏒',NFL:'🏈'})[s]||'🎯';
    const legsHtml = (slip.legs||[]).map(leg => `
      <div style="display:flex;align-items:flex-start;gap:12px;padding:12px 0;border-bottom:1px solid rgba(26,40,64,0.5)">
        <span style="font-size:20px;flex-shrink:0;margin-top:2px">${sportIcon(leg.sport)}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:14px;font-weight:700;margin-bottom:4px">${leg.game||'—'}</div>
          <div style="color:${color};font-family:'Space Mono';font-size:14px;font-weight:700;margin-bottom:6px">${leg.pick||'—'}</div>
          <div style="font-size:12px;color:var(--muted);line-height:1.5">${leg.reasoning||''}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;min-width:70px">
          <div style="font-family:'Space Mono';font-size:14px;color:${color};font-weight:700">${(leg.odds||0).toFixed(2)}x</div>
          <div style="font-size:10px;color:var(--muted);margin-top:2px">conf ${(leg.confidence||0).toFixed(1)}</div>
          <a href="https://kalshi.com/markets?search=${encodeURIComponent((leg.game||'').split(' vs ')[0]||leg.ticker||'')}"
             target="_blank" style="font-size:10px;color:var(--blue);text-decoration:none;display:block;margin-top:6px">
            Kalshi →
          </a>
        </div>
      </div>`).join('');
    return `
      <div style="background:var(--bg2);border:1px solid var(--border);border-left:3px solid ${color};border-radius:12px;padding:18px;margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="border:1px solid ${color};color:${color};padding:4px 14px;border-radius:6px;font-size:12px;font-weight:700;font-family:'Space Mono'">${label}</span>
            <span style="font-size:12px;color:var(--muted)">${slip.leg_count} legs</span>
          </div>
          <div style="text-align:right">
            <div style="font-family:'Space Mono';font-size:22px;font-weight:700;color:${color}">${(slip.combined_odds||0).toFixed(2)}x</div>
            <div style="font-size:10px;color:var(--muted)">combined odds</div>
          </div>
        </div>
        ${legsHtml}
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
          <div style="font-size:12px;color:var(--muted);flex:1;padding-right:16px">${slip.summary||''}</div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:11px;color:var(--muted)">Stake $${(slip.stake||0).toFixed(2)}</div>
            <div style="font-family:'Space Mono';font-size:14px;color:${color};font-weight:700">→ $${(slip.potential_payout||0).toFixed(2)} if all win</div>
          </div>
        </div>
      </div>`;
  }).join('');
  document.getElementById('picks-content').innerHTML = html;
}

function renderPicksFromDB(slips, date) {
  if (!slips || !slips.length) return;
  const dateEl = document.getElementById('picks-date');
  const countEl = document.getElementById('picks-count');
  if (dateEl) dateEl.textContent = date || '';
  if (countEl) countEl.textContent = slips.length + ' slip' + (slips.length!==1?'s':'') + ' saved';
  const sportIcon = s => ({NBA:'🏀',MLB:'⚾',NHL:'🏒',NFL:'🏈'})[s]||'🎯';
  const html = slips.map(slip => {
    const odds = slip.combined_odds || 0;
    const color = odds >= 4 ? 'var(--gold)' : odds >= 2.5 ? 'var(--blue)' : 'var(--green)';
    const legsHtml = (slip.legs||[]).map(leg => `
      <div style="padding:10px 0;border-bottom:1px solid rgba(26,40,64,0.4);display:flex;gap:10px;align-items:flex-start">
        <span style="font-size:18px;flex-shrink:0">${sportIcon(leg.sport)}</span>
        <div style="flex:1">
          <div style="font-size:13px;color:var(--muted);margin-bottom:2px">${leg.game||''}</div>
          <div style="font-size:14px;font-weight:700;color:${color}">${leg.pick||leg.calibrated_line||''}</div>
          ${leg.ai_reasoning?`<div style="font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5">${leg.ai_reasoning}</div>`:''}
        </div>
        <div style="font-family:'Space Mono';font-size:13px;color:${color};flex-shrink:0">${(leg.individual_odds||0).toFixed(2)}x</div>
      </div>`).join('');
    return `
      <div style="background:var(--bg2);border:1px solid var(--border);border-left:3px solid ${color};border-radius:12px;padding:16px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <span style="color:var(--muted);font-size:12px">${slip.sport_mix||''} · ${slip.status||''}</span>
          <span style="font-family:'Space Mono';font-size:20px;font-weight:700;color:${color}">${odds.toFixed(2)}x</span>
        </div>
        ${legsHtml}
        <div style="margin-top:12px;font-size:11px;color:var(--muted)">
          Stake $${(slip.stake||0).toFixed(2)} → <span style="color:${color};font-weight:700">$${(slip.potential_payout||0).toFixed(2)}</span> potential
        </div>
      </div>`;
  }).join('');
  document.getElementById('picks-content').innerHTML = html;
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function formatTime(isoStr) {
  if (!isoStr) return '—';
  try {
    return new Date(isoStr).toLocaleString('en-US', {
      month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'
    });
  } catch(e) { return isoStr; }
}

function formatFinish(isoStr) {
  if (!isoStr) return '—';
  try {
    const now = new Date();
    const finish = new Date(isoStr);
    const diff = finish - now;
    if (diff < 0) return 'Finished';
    const hours = Math.floor(diff / 3600000);
    const mins = Math.floor((diff % 3600000) / 60000);
    if (hours > 0) return `${hours}h ${mins}m`;
    return `${mins}m`;
  } catch(e) { return '—'; }
}

// ── INIT ──────────────────────────────────────────────────────────────────────
function init() {
  refreshPicks();
  fetchStatus();
  fetchSlips();
  fetchRollover();
  fetchStats();
  fetchBacktestResults();
  fetchSimulationResults();
  loadChart('1d', document.querySelector('.tog-btn.active'));
  calcRollover();

  setInterval(fetchStatus, 10000);
  setInterval(fetchSlips, 30000);
  setInterval(fetchRollover, 30000);
  setInterval(fetchStats, 60000);
  setInterval(refreshPicks, 60000);
}

window.addEventListener('load', init);
</script>
</body>
</html>"""


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _infer_sport(ticker: str) -> str:
    t = ticker.upper()
    if "NBA" in t: return "NBA"
    if "MLB" in t: return "MLB"
    if "NHL" in t: return "NHL"
    if "NFL" in t: return "NFL"
    return "OTHER"


def _next_generation_time() -> str:
    now = datetime.now(timezone.utc)
    eastern_hour = (now.hour - 4) % 24
    if GENERATION_HOUR_START <= eastern_hour < GENERATION_HOUR_END:
        return "Now (window open — click Generate Picks)"
    hours_until = (
        GENERATION_HOUR_START - eastern_hour
        if eastern_hour < GENERATION_HOUR_START
        else 24 - eastern_hour + GENERATION_HOUR_START
    )
    return (now + timedelta(hours=hours_until)).strftime("%H:%M UTC")


# ── BACKGROUND LOOP ───────────────────────────────────────────────────────────

async def run_background_tasks():
    global bot_running
    try:
        init_bot()
    except Exception as e:
        logger.error(f"[BOT] init_bot failed: {e}", exc_info=True)
        return

    try:
        await kalshi.connect()
        logger.info("[BOT] Kalshi client connected")
    except Exception as e:
        logger.error(f"[BOT] Kalshi connect failed: {e}", exc_info=True)

    bot_running = True
    logger.info("[BOT] Ready — use Generate Picks button to create a slip")

    while True:
        await asyncio.sleep(300)


def main():
    import pathlib
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("  APEX/SPORTS BOT v13 — STARTING")
    logger.info(f"  Paper mode: {PAPER_MODE}")
    logger.info(f"  DB path: {DB_PATH}")
    logger.info(f"  Port: {DASHBOARD_PORT}")
    logger.info("=" * 60)

    # NOTE: init_bot() is called inside the background thread so Flask can
    # bind first and pass Railway's healthcheck on /api/health.

    # Start background tasks in a separate thread
    import threading

    def run_async():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_background_tasks())
        except BaseException as e:
            logger.error(f"[BG] Background thread crashed: {type(e).__name__}: {e}", exc_info=True)

    bg_thread = threading.Thread(target=run_async, daemon=True)
    bg_thread.start()

    # Start Flask dashboard
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
