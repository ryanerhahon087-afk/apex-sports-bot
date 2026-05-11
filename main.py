"""
APEX/SPORTS BOT — Main Entry Point
Runs the dashboard server and slip generation scheduler.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import Flask, jsonify, request, Response
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.config import *
from logs.database import SportsDatabase
from data.kalshi_client import SportsKalshiClient
from utils.intelligence import SportsIntelligence
from utils.slip_generator import SlipGenerator
import config.config as cfg

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
    """Catch-all: return JSON instead of Flask's default HTML 500 page."""
    import traceback as tb
    logger.error(f"[FLASK] Unhandled exception: {e}", exc_info=True)
    return jsonify({
        "error": str(e),
        "type": type(e).__name__,
        "traceback": tb.format_exc()[-1000:],
    }), 500


# ── GLOBAL BOT STATE ──────────────────────────────────────────────────────────
db: Optional[SportsDatabase] = None
kalshi: Optional[SportsKalshiClient] = None
intelligence: Optional[SportsIntelligence] = None
generator: Optional[SlipGenerator] = None
bot_running = False
bot_paused = False
cb_active = False
cb_resume_at = None
last_generation_date = None
generation_running = False   # lock: prevents background + manual button from overlapping


def init_bot():
    """Initialize all bot components."""
    global db, kalshi, intelligence, generator

    db = SportsDatabase(DB_PATH)

    kalshi = SportsKalshiClient(
        api_key_id=KALSHI_API_KEY_ID,
        private_key_pem=KALSHI_PRIVATE_KEY,
        base_url=KALSHI_BASE_URL,
        paper_mode=PAPER_MODE,
    )

    intelligence = SportsIntelligence(
        api_key=ANTHROPIC_API_KEY,
        model=AI_MODEL,
    )

    generator = SlipGenerator(
        kalshi_client=kalshi,
        intelligence=intelligence,
        database=db,
        config=cfg,
    )

    logger.info(f"[BOT] Sports bot initialized | "
               f"paper={'YES' if PAPER_MODE else 'NO'}")


# ── API ENDPOINTS ─────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/api/status")
def status():
    global bot_paused, cb_active, cb_resume_at

    balance = db.get_balance() if db else STARTING_BALANCE
    today_stats = db.get_today_stats() if db else {}
    all_stats = db.get_stats() if db else {}

    total = all_stats.get("total", 0)
    wins = all_stats.get("wins", 0)
    win_rate = wins / total if total > 0 else 0

    return jsonify({
        "balance": balance,
        "paper_mode": PAPER_MODE,
        "bot_running": bot_running,
        "bot_paused": bot_paused,
        "cb_active": cb_active,
        "cb_resume_at": cb_resume_at,
        "today_pnl": today_stats.get("pnl", 0),
        "today_slips": today_stats.get("total", 0),
        "all_time_pnl": all_stats.get("total_pnl", 0),
        "all_time_slips": total,
        "win_rate": round(win_rate, 4),
        "wins": wins,
        "losses": all_stats.get("losses", 0),
        "next_generation": _next_generation_time(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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


@app.route("/api/rollover/active")
def get_active_rollover():
    rollover = db.get_active_rollover() if db else None
    return jsonify({"rollover": rollover})


@app.route("/api/rollover/history")
def get_rollover_history():
    history = db.get_rollover_history(10) if db else []
    return jsonify({"history": history})


@app.route("/api/portfolio")
def get_portfolio():
    days = int(request.args.get("days", 30))
    history = db.get_portfolio_history(days) if db else []
    return jsonify({"history": history})


@app.route("/api/stats")
def get_stats():
    all_stats = db.get_stats() if db else {}
    today_stats = db.get_today_stats() if db else {}
    cb_history = db.get_cb_history(4) if db else []
    return jsonify({
        "all_time": all_stats,
        "today": today_stats,
        "circuit_breaker_history": cb_history,
    })


@app.route("/api/rollover/calculate", methods=["POST"])
def calculate_rollover():
    """Calculate rollover projections without placing any bets."""
    data = request.get_json() or {}
    days = int(data.get("days", 5))
    odds = float(data.get("odds_per_day", 3.0))
    stake = float(data.get("starting_stake", 10.0))

    result = intelligence.calculate_rollover(days, odds, stake)
    return jsonify(result)


@app.route("/api/rollover/start", methods=["POST"])
def start_rollover():
    """Start a manual rollover from the calculator."""
    data = request.get_json() or {}
    days = int(data.get("days", 5))
    odds = float(data.get("odds_per_day", 3.0))
    stake = float(data.get("starting_stake", 10.0))

    # Validate first
    validation = intelligence.calculate_rollover(days, odds, stake)
    if not validation.get("valid"):
        return jsonify({"error": validation.get("errors", ["Invalid rollover"])}), 400

    # Check if there's already an active rollover
    existing = db.get_active_rollover()
    if existing:
        return jsonify({
            "error": "There is already an active rollover. "
                    "Complete or cancel it first."
        }), 400

    # Check balance
    balance = db.get_balance()
    if stake > balance:
        return jsonify({"error": f"Insufficient balance. Have ${balance:.2f}, need ${stake:.2f}"}), 400

    rollover_id = db.create_rollover(
        target_odds=odds,
        target_days=days,
        starting_stake=stake,
        source="MANUAL",
    )

    return jsonify({
        "success": True,
        "rollover_id": rollover_id,
        "message": f"Rollover started. First slip will be generated tonight.",
        "projection": validation,
    })


@app.route("/api/generate", methods=["POST"])
def generate_now():
    """Manually trigger slip generation."""
    global generation_running

    if bot_paused:
        return jsonify({"error": "Bot is paused"}), 400

    if kalshi is None or generator is None:
        return jsonify({"error": "Bot not initialized yet, try again in 10 seconds"}), 503

    if generation_running:
        return jsonify({"error": "Generation already in progress, please wait"}), 409

    try:
        generation_running = True
        async def _run():
            # Create a FRESH client + generator for this request's own event loop.
            # Never touch the global kalshi/_session — it belongs to the background
            # thread's loop and mixing loops causes "Event loop is closed" errors.
            local_kalshi = SportsKalshiClient(
                api_key_id=KALSHI_API_KEY_ID,
                private_key_pem=KALSHI_PRIVATE_KEY,
                base_url=KALSHI_BASE_URL,
                paper_mode=PAPER_MODE,
            )
            await local_kalshi.connect()
            local_gen = SlipGenerator(
                kalshi_client=local_kalshi,
                intelligence=intelligence,
                database=db,
                config=cfg,
            )
            try:
                result = await local_gen.generate_all_slips()
            finally:
                await local_kalshi.disconnect()
            return result

        loop = asyncio.new_event_loop()
        # Do NOT call asyncio.set_event_loop(loop) — would replace the global
        # event loop reference, potentially confusing the background thread.
        result = loop.run_until_complete(_run())
        loop.close()
        return jsonify({"success": True, "result": result})
    except Exception as e:
        import traceback
        logger.error(f"[GEN] Generate endpoint error: {e}", exc_info=True)
        return jsonify({"error": str(e), "traceback": traceback.format_exc()[-500:]}), 500
    finally:
        generation_running = False


@app.route("/api/pause", methods=["POST"])
def toggle_pause():
    global bot_paused
    bot_paused = not bot_paused
    db.set_state("bot_paused", str(bot_paused))
    return jsonify({"paused": bot_paused})


@app.route("/api/reset-circuit-breaker", methods=["POST"])
def reset_cb():
    global cb_active, cb_resume_at
    cb_active = False
    cb_resume_at = None
    db.mark_cb_reset(manually=True)
    db.set_state("cb_active", "false")
    logger.info("[CB] Circuit breaker manually reset")
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
  
  // FIX 3: Sport-based icon instead of slip-type-based
  function getSlipIcon(slip) {
    const sports = (slip.sport_mix || '').toUpperCase();
    if (slip.slip_type === 'ROLLOVER') return '🔄';
    if (slip.slip_type === 'LOTTO') return '🎰';
    if (sports.includes('NBA')) return '🏀';
    if (sports.includes('MLB')) return '⚾';
    if (sports.includes('SOCCER')) return '⚽';
    if (sports.includes('NHL')) return '🏒';
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
    const sportPills = sports.map(sp => {
      const cls = sp.trim() === 'NBA' ? 'sp-nba' : sp.trim() === 'MLB' ? 'sp-mlb' : 'sp-soccer';
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
  
  const sportIcon = { NBA:'🏀', MLB:'⚾', SOCCER:'⚽' };
  
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
  fetchStatus();
  fetchSlips();
  fetchRollover();
  fetchStats();
  loadChart('1d', document.querySelector('.tog-btn.active'));
  calcRollover();
  
  setInterval(fetchStatus, 10000);
  setInterval(fetchSlips, 30000);
  setInterval(fetchRollover, 30000);
  setInterval(fetchStats, 60000);
}

window.addEventListener('load', init);
</script>
</body>
</html>"""


# ── HELPER FUNCTIONS ──────────────────────────────────────────────────────────

def _next_generation_time() -> str:
    """Calculate next scheduled generation time."""
    now = datetime.now(timezone.utc)
    # Convert to Eastern Time (UTC-4 in EDT)
    eastern_hour = (now.hour - 4) % 24
    
    if GENERATION_HOUR_START <= eastern_hour < GENERATION_HOUR_END:
        return "Now (generation window open)"
    
    # Calculate hours until next window
    if eastern_hour < GENERATION_HOUR_START:
        hours_until = GENERATION_HOUR_START - eastern_hour
    else:
        hours_until = 24 - eastern_hour + GENERATION_HOUR_START
    
    next_time = now + timedelta(hours=hours_until)
    return next_time.strftime("%H:%M UTC")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def run_background_tasks():
    """Background loop: initialize bot, check pending slips, run scheduled generation."""
    global bot_running, last_generation_date

    # Init bot here so Flask can start first and pass the healthcheck
    try:
        init_bot()
    except Exception as e:
        logger.error(f"[BOT] init_bot failed: {e}", exc_info=True)
        return

    try:
        await kalshi.connect()
        logger.info("[BOT] Kalshi client connected successfully")
    except Exception as e:
        logger.error(f"[BOT] Kalshi connect failed: {e}", exc_info=True)
        # Continue anyway — generate_now() will reconnect per-request
    bot_running = True
    logger.info("[BOT] Background tasks started")

    while True:
        try:
            # Check pending slips every 5 minutes
            await generator.check_pending_slips()

            # Check if it's generation time (10 AM - 12 PM Eastern)
            now = datetime.now(timezone.utc)
            eastern_hour = (now.hour - 4) % 24
            today = now.date().isoformat()

            if (GENERATION_HOUR_START <= eastern_hour < GENERATION_HOUR_END
                    and today != last_generation_date
                    and not bot_paused
                    and not cb_active
                    and not generation_running):
                global generation_running
                generation_running = True
                try:
                    logger.info("[BOT] Generation window open — starting slip generation")
                    await generator.generate_all_slips()
                    last_generation_date = today
                finally:
                    generation_running = False

            await asyncio.sleep(300)  # Check every 5 minutes

        except Exception as e:
            logger.error(f"[BOT] Background error: {e}", exc_info=True)
            await asyncio.sleep(60)


def main():
    """Start the sports bot."""
    import pathlib
    # Ensure storage directory exists before anything else
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("  APEX/SPORTS BOT — STARTING")
    logger.info(f"  Paper mode: {PAPER_MODE}")
    logger.info(f"  DB path: {DB_PATH}")
    logger.info(f"  Generation window: {GENERATION_HOUR_START}:00-{GENERATION_HOUR_END}:00 Eastern")
    logger.info("=" * 60)

    # NOTE: init_bot() is called inside the background thread so Flask can
    # bind first and pass Railway's healthcheck on /api/health.

    # Start background tasks in a separate thread
    import threading

    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_background_tasks())

    bg_thread = threading.Thread(target=run_async, daemon=True)
    bg_thread.start()

    # Start Flask dashboard
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
