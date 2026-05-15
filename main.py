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
        balance = db.get_balance() if db else STARTING_BALANCE

        async def _run():
            # Create a brand-new Kalshi client in this event loop so the
            # aiohttp session is never shared across loops ("Timeout context
            # manager should be used inside a task" otherwise).
            fresh_kalshi = SportsKalshiClient(
                api_key_id=KALSHI_API_KEY_ID,
                private_key_pem=KALSHI_PRIVATE_KEY,
                base_url=KALSHI_BASE_URL,
                paper_mode=PAPER_MODE,
            )
            await fresh_kalshi.connect()
            try:
                engine = PicksEngine(ANTHROPIC_API_KEY, fresh_kalshi)
                return await engine.generate_all_slips(balance)
            finally:
                await fresh_kalshi.disconnect()

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
.logo-mark { width:40px; height:40px; background:linear-gradient(135deg,var(--green),var(--blue)); border-radius:10px; display:flex; align-items:center; justify-content:center; font-family:'Bebas Neue'; font-size:18px; color:#000; letter-spacing:1px; }
.logo-text { font-family:'Bebas Neue'; font-size:22px; letter-spacing:2px; }
.logo-sub { font-size:11px; color:var(--muted); letter-spacing:3px; text-transform:uppercase; margin-top:1px; }
.hdr-right { display:flex; align-items:center; gap:12px; }
.badge { padding:5px 14px; border-radius:20px; font-size:11px; font-family:'Space Mono'; letter-spacing:1px; }
.badge-paper { background:rgba(255,200,32,0.1); border:1px solid rgba(255,200,32,0.3); color:var(--gold); }
.badge-live { background:rgba(0,232,122,0.1); border:1px solid rgba(0,232,122,0.3); color:var(--green); }
.status-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot-green { background:var(--green); box-shadow:0 0 8px var(--green); animation:pulse 2s infinite; }
.dot-red { background:var(--red); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.6} }
.ctrl-btn { padding:7px 18px; border:1px solid var(--border); background:var(--card); color:var(--muted); border-radius:8px; cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.2s; }
.ctrl-btn:hover { border-color:var(--blue); color:var(--text); }

/* CB BANNER */
.cb-banner { background:rgba(255,51,85,0.08); border-bottom:1px solid rgba(255,51,85,0.3); padding:12px 32px; display:none; align-items:center; justify-content:space-between; }
.cb-banner.active { display:flex; }
.cb-banner-title { color:var(--red); font-weight:700; font-size:14px; }
.cb-banner-reason { color:var(--muted); font-size:13px; }
.cb-resume-btn { padding:7px 20px; background:var(--green); color:#000; border:none; border-radius:8px; font-weight:700; font-size:13px; cursor:pointer; }

/* MAIN */
.main { padding:24px 32px 60px; display:grid; gap:20px; }

/* STAT BAR */
.stats-row { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; }
.stat-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px 20px; position:relative; overflow:hidden; }
.stat-card::before { content:''; position:absolute; top:0; left:0; right:0; height:2px; }
.sc-green::before { background:linear-gradient(90deg,var(--green),transparent); }
.sc-blue::before { background:linear-gradient(90deg,var(--blue),transparent); }
.sc-gold::before { background:linear-gradient(90deg,var(--gold),transparent); }
.sc-purple::before { background:linear-gradient(90deg,var(--purple),transparent); }
.stat-label { font-size:10px; color:var(--muted); letter-spacing:2px; text-transform:uppercase; margin-bottom:8px; font-weight:600; }
.stat-value { font-family:'Space Mono'; font-size:24px; font-weight:700; }
.stat-sub { font-size:12px; color:var(--muted); margin-top:4px; }
.val-green { color:var(--green); }
.val-red { color:var(--red); }
.val-gold { color:var(--gold); }

/* TODAY'S PICKS */
.picks-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:24px; }
.card-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
.card-title { font-size:11px; color:var(--muted); letter-spacing:2px; text-transform:uppercase; font-weight:700; display:flex; align-items:center; gap:8px; }
.card-dot { width:6px; height:6px; border-radius:50%; }
.picks-empty { text-align:center; padding:40px 24px; color:var(--muted); font-size:14px; line-height:1.8; }
.picks-empty strong { display:block; font-size:16px; color:var(--text); margin-bottom:8px; }
.picks-gen-btn { padding:8px 20px; background:rgba(0,232,122,0.1); border:1px solid rgba(0,232,122,0.3); border-radius:8px; color:var(--green); cursor:pointer; font-size:13px; font-family:'DM Sans'; font-weight:700; transition:all 0.2s; }
.picks-gen-btn:hover { background:rgba(0,232,122,0.2); }

/* RECENT ACTIVITY */
.activity-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:22px; }
.activity-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
.activity-filters { display:flex; gap:6px; }
.af-btn { padding:4px 14px; border-radius:6px; border:1px solid var(--border); background:transparent; color:var(--muted); cursor:pointer; font-size:12px; font-family:'DM Sans'; transition:all 0.15s; }
.af-btn.active { background:rgba(45,156,255,0.1); border-color:var(--blue); color:var(--blue); }
.activity-table { width:100%; border-collapse:collapse; }
.activity-table th { text-align:left; padding:8px 14px; color:var(--muted); font-size:10px; letter-spacing:1.5px; text-transform:uppercase; border-bottom:1px solid var(--border); font-weight:600; }
.activity-table td { padding:11px 14px; border-bottom:1px solid rgba(26,40,64,0.4); vertical-align:middle; }
.activity-table tr:last-child td { border-bottom:none; }
.activity-table tr { cursor:pointer; transition:background 0.15s; }
.activity-table tr:hover td { background:rgba(255,255,255,0.02); }
.type-badge { display:inline-flex; padding:3px 10px; border-radius:6px; font-size:11px; font-family:'Space Mono'; font-weight:700; letter-spacing:0.5px; }
.tb-safe { background:rgba(0,232,122,0.1); color:var(--green); }
.tb-standard { background:rgba(45,156,255,0.1); color:var(--blue); }
.tb-bold { background:rgba(255,200,32,0.1); color:var(--gold); }
.status-badge { display:inline-flex; padding:3px 10px; border-radius:6px; font-size:11px; font-family:'Space Mono'; font-weight:700; }
.sb-open { background:rgba(255,200,32,0.1); color:var(--gold); }
.sb-won { background:rgba(0,232,122,0.1); color:var(--green); }
.sb-lost { background:rgba(255,51,85,0.1); color:var(--red); }
.sport-pill { display:inline-flex; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:700; letter-spacing:0.5px; margin-right:2px; }
.sp-nba { background:rgba(29,66,138,0.3); color:#5b8dd9; }
.sp-mlb { background:rgba(227,25,55,0.2); color:#ff6b7a; }
.sp-nhl { background:rgba(0,100,200,0.2); color:#4db8ff; }
.sp-nfl { background:rgba(1,51,105,0.3); color:#7ab3f5; }
.sp-tennis { background:rgba(200,180,0,0.2); color:#ffe033; }
.mono { font-family:'Space Mono'; font-size:12px; }
.pnl-pos { color:var(--green); font-family:'Space Mono'; font-weight:700; }
.pnl-neg { color:var(--red); font-family:'Space Mono'; font-weight:700; }

/* MODAL */
.modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); backdrop-filter:blur(4px); z-index:200; display:none; align-items:center; justify-content:center; }
.modal-overlay.show { display:flex; }
.modal { background:var(--bg2); border:1px solid var(--border); border-radius:16px; padding:28px; width:600px; max-height:80vh; overflow-y:auto; position:relative; }
.modal-close { position:absolute; top:16px; right:16px; background:transparent; border:1px solid var(--border); color:var(--muted); width:28px; height:28px; border-radius:6px; cursor:pointer; font-size:16px; display:flex; align-items:center; justify-content:center; }
.modal-title { font-size:16px; font-weight:700; margin-bottom:6px; }
.modal-subtitle { font-size:12px; color:var(--muted); margin-bottom:20px; }
.leg-card { background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:10px; }
.leg-game { font-weight:700; font-size:14px; }
.leg-pick { color:var(--green); font-size:13px; margin-bottom:4px; font-family:'Space Mono'; }
.leg-reasoning { font-size:12px; color:var(--muted); margin-top:8px; line-height:1.5; }
.leg-meta { display:flex; gap:12px; margin-top:8px; font-size:11px; color:var(--muted); }
.leg-status { font-weight:700; }
.ls-pending { color:var(--gold); }
.ls-won { color:var(--green); }
.ls-lost { color:var(--red); }

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
    <button class="ctrl-btn" onclick="togglePause()">&#9646;&#9646; Pause</button>
    <button class="ctrl-btn" onclick="generateNow()">&#9889; Generate Now</button>
  </div>
</div>

<!-- CB BANNER -->
<div class="cb-banner" id="cb-banner">
  <div style="display:flex;align-items:center;gap:12px">
    <span style="font-size:18px">&#9889;</span>
    <div>
      <div class="cb-banner-title">CIRCUIT BREAKER ACTIVE — All trading halted</div>
      <div class="cb-banner-reason" id="cb-reason">Win rate dropped below threshold</div>
    </div>
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
        <button class="ctrl-btn" onclick="refreshPicks()" id="refresh-btn">&#8635; Refresh</button>
        <button class="picks-gen-btn" onclick="generatePicks()" id="gen-picks-btn">&#9889; Generate Picks</button>
      </div>
    </div>
    <div id="picks-content">
      <div class="picks-empty">
        <strong>No picks generated yet today</strong>
        Click "Generate Picks" to build today's 3 slips (Safe 2x &middot; Standard 3x &middot; Bold 5x).<br>
        <span style="font-size:12px">Each slip uses 4-6 high-probability legs researched with live web data.</span>
      </div>
    </div>
  </div>

  <!-- STAT BAR -->
  <div class="stats-row">
    <div class="stat-card sc-green">
      <div class="stat-label">Cash Reserves</div>
      <div class="stat-value val-green" id="balance">$0.00</div>
      <div class="stat-sub" id="balance-tier">Loading...</div>
    </div>
    <div class="stat-card sc-blue">
      <div class="stat-label">Today P&amp;L</div>
      <div class="stat-value" id="today-pnl">+$0.00</div>
      <div class="stat-sub" id="today-slips">0 slips today</div>
    </div>
    <div class="stat-card sc-gold">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value val-gold" id="win-rate">0.0%</div>
      <div class="stat-sub" id="wl-record">0W / 0L</div>
    </div>
    <div class="stat-card sc-purple">
      <div class="stat-label">All-Time P&amp;L</div>
      <div class="stat-value" id="alltime-pnl">+$0.00</div>
      <div class="stat-sub" id="alltime-slips">0 all-time slips</div>
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
        <button class="af-btn" onclick="filterSlips('OPEN',this)">Open</button>
        <button class="af-btn" onclick="filterSlips('WON',this)">Won</button>
        <button class="af-btn" onclick="filterSlips('LOST',this)">Lost</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table class="activity-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Type</th>
            <th>Sports</th>
            <th>Legs</th>
            <th>Odds</th>
            <th>Stake</th>
            <th>Status</th>
            <th>P&amp;L</th>
          </tr>
        </thead>
        <tbody id="activity-tbody"></tbody>
      </table>
    </div>
  </div>

</div><!-- /main -->
</div><!-- /wrap -->

<!-- SLIP DETAIL MODAL -->
<div class="modal-overlay" id="slip-modal" onclick="closeModal(event)">
  <div class="modal" id="modal-content">
    <button class="modal-close" onclick="closeModal()">&#10005;</button>
    <div id="modal-body"></div>
  </div>
</div>

<script>
let allSlips = [];
let currentFilter = 'all';

// ── STATUS ────────────────────────────────────────────────────────────────────
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    const bal = d.balance || 0;
    document.getElementById('balance').textContent = '$' + bal.toFixed(2);
    let tier = 'Early Stage';
    if (bal >= 10000) tier = 'Elite';
    else if (bal >= 1000) tier = 'Growth';
    else if (bal >= 500) tier = 'Building';
    document.getElementById('balance-tier').textContent = tier + ' tier';

    const pnl = d.today_pnl || 0;
    const pnlEl = document.getElementById('today-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'val-green' : 'val-red');
    document.getElementById('today-slips').textContent = (d.today_slips || 0) + ' slips today';

    const wr = (d.win_rate || 0) * 100;
    document.getElementById('win-rate').textContent = wr.toFixed(1) + '%';
    document.getElementById('wl-record').textContent = (d.wins||0) + 'W / ' + (d.losses||0) + 'L';

    const atpnl = d.all_time_pnl || 0;
    const atEl = document.getElementById('alltime-pnl');
    atEl.textContent = (atpnl >= 0 ? '+' : '') + '$' + Math.abs(atpnl).toFixed(2);
    atEl.className = 'stat-value ' + (atpnl >= 0 ? 'val-green' : 'val-red');
    document.getElementById('alltime-slips').textContent = (d.all_time_slips||0) + ' all-time slips';

    const statusEl = document.getElementById('status-text');
    if (d.cb_active) {
      statusEl.textContent = 'CIRCUIT BREAKER';
      document.getElementById('cb-banner').classList.add('active');
    } else if (d.bot_paused) {
      statusEl.textContent = 'PAUSED';
      document.getElementById('cb-banner').classList.remove('active');
    } else {
      statusEl.textContent = 'ACTIVE';
      document.getElementById('cb-banner').classList.remove('active');
    }
  } catch(e) { console.error('Status error:', e); }
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

function slipTypeBadge(combined_odds) {
  if (combined_odds >= 4.0) return '<span class="type-badge tb-bold">BOLD</span>';
  if (combined_odds >= 2.5) return '<span class="type-badge tb-standard">STANDARD</span>';
  return '<span class="type-badge tb-safe">SAFE</span>';
}

function renderSlips(filter) {
  let slips = [...allSlips];
  if (filter === 'OPEN') slips = slips.filter(s => s.status === 'PENDING');
  else if (filter === 'WON') slips = slips.filter(s => s.status === 'WON');
  else if (filter === 'LOST') slips = slips.filter(s => s.status === 'LOST');

  const sportPillClass = { NBA:'sp-nba', MLB:'sp-mlb', NHL:'sp-nhl', NFL:'sp-nfl', NCAAFB:'sp-nfl', NCAAMB:'sp-nba', TENNIS:'sp-tennis' };

  const html = slips.map(s => {
    const pnl = s.net_pnl || 0;
    const odds = s.combined_odds || 0;
    const sports = (s.sport_mix || '').split(',').map(sp => {
      const key = sp.trim().toUpperCase();
      return `<span class="sport-pill ${sportPillClass[key]||'sp-nba'}">${sp.trim()}</span>`;
    }).join('');

    let statusBadge = '';
    if (s.status === 'PENDING') statusBadge = '<span class="status-badge sb-open">OPEN</span>';
    else if (s.status === 'WON') statusBadge = '<span class="status-badge sb-won">WIN</span>';
    else statusBadge = '<span class="status-badge sb-lost">LOSS</span>';

    const dateStr = s.created_at ? new Date(s.created_at).toLocaleDateString('en-US',{month:'short',day:'numeric'}) : '—';

    return `<tr onclick="openSlipModal(${s.id})">
      <td style="font-size:12px;color:var(--muted)">${dateStr}</td>
      <td>${slipTypeBadge(odds)}</td>
      <td>${sports}</td>
      <td class="mono" style="color:var(--muted)">${s.leg_count || 0}</td>
      <td class="mono" style="color:var(--blue)">${odds.toFixed(2)}x</td>
      <td class="mono">$${(s.stake||0).toFixed(2)}</td>
      <td>${statusBadge}</td>
      <td class="${pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${s.status !== 'PENDING' ? (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2) : '—'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:32px">No slips yet — generate your first slip</td></tr>';

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
        <span class="leg-status ${statusClass}" style="margin-left:auto">${leg.status}</span>
      </div>
      <div class="leg-pick">${leg.pick}</div>
      <div class="leg-meta">
        <span>Odds: <strong style="color:var(--blue)">${(leg.individual_odds||0).toFixed(2)}x</strong></span>
        <span>Conf: <strong style="color:var(--gold)">${(leg.confidence||0).toFixed(1)}/10</strong></span>
        <span>${leg.market_type}</span>
        ${leg.game_start ? `<span>Game: ${formatTime(leg.game_start)}</span>` : ''}
      </div>
      ${leg.ai_reasoning ? `<div class="leg-reasoning">&#128161; ${leg.ai_reasoning}</div>` : ''}
    </div>`;
  }).join('');

  const odds = slip.combined_odds || 0;
  const color = odds >= 4 ? 'var(--gold)' : odds >= 2.5 ? 'var(--blue)' : 'var(--green)';
  const typeLabel = odds >= 4 ? 'BOLD' : odds >= 2.5 ? 'STANDARD' : 'SAFE';

  document.getElementById('modal-body').innerHTML = `
    <div class="modal-title">${typeLabel} SLIP #${slip.id}</div>
    <div class="modal-subtitle">
      <span style="color:${color};font-family:'Space Mono'">${odds.toFixed(2)}x</span> combined &middot;
      $${slip.stake?.toFixed(2)} stake &middot;
      Conf: ${slip.confidence?.toFixed(1)}/10 &middot;
      Potential: $${slip.potential_payout?.toFixed(2)}
    </div>
    ${legsHtml}
    <div style="font-size:11px;color:var(--muted);margin-top:8px">Generated: ${formatTime(slip.created_at)}</div>
  `;
  document.getElementById('slip-modal').classList.add('show');
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('slip-modal')) {
    document.getElementById('slip-modal').classList.remove('show');
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
    await fetch('/api/generate', { method: 'POST' });
    btn.textContent = '⚡ Generate Now';
    btn.disabled = false;
    refreshPicks();
    fetchSlips();
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

// ── TODAY'S PICKS ─────────────────────────────────────────────────────────────
async function generatePicks() {
  const btn = document.getElementById('gen-picks-btn');
  btn.textContent = '⏳ Generating...';
  btn.disabled = true;
  document.getElementById('picks-content').innerHTML =
    '<div style="text-align:center;padding:40px;color:var(--muted)">Researching markets with live web data...<br><span style="font-size:11px">Building 3 slips: Safe 2x &middot; Standard 3x &middot; Bold 5x</span></div>';
  try {
    const r = await fetch('/api/generate', {method:'POST'});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    renderPicksFromResult(d.result);
    fetchSlips();
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
             target="_blank" style="font-size:10px;color:var(--blue);text-decoration:none;display:block;margin-top:6px">Kalshi &#8594;</a>
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
            <div style="font-family:'Space Mono';font-size:14px;color:${color};font-weight:700">&#8594; $${(slip.potential_payout||0).toFixed(2)} if all win</div>
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
          <span style="color:var(--muted);font-size:12px">${slip.sport_mix||''} &middot; ${slip.status||''}</span>
          <span style="font-family:'Space Mono';font-size:20px;font-weight:700;color:${color}">${odds.toFixed(2)}x</span>
        </div>
        ${legsHtml}
        <div style="margin-top:12px;font-size:11px;color:var(--muted)">
          Stake $${(slip.stake||0).toFixed(2)} &#8594; <span style="color:${color};font-weight:700">$${(slip.potential_payout||0).toFixed(2)}</span> potential
        </div>
      </div>`;
  }).join('');
  document.getElementById('picks-content').innerHTML = html;
}

// ── HELPERS ───────────────────────────────────────────────────────────────────
function formatTime(isoStr) {
  if (!isoStr) return '—';
  try {
    return new Date(isoStr).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  } catch(e) { return isoStr; }
}

// ── INIT ──────────────────────────────────────────────────────────────────────
function init() {
  refreshPicks();
  fetchStatus();
  fetchSlips();
  setInterval(fetchStatus, 10000);
  setInterval(fetchSlips, 30000);
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
