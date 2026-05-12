"""
APEX/SPORTS BOT — Configuration
All constants and settings in one place.
"""
import os

# ── API KEYS ──────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID   = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY  = os.environ.get("KALSHI_PRIVATE_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

# ── KALSHI API ────────────────────────────────────────────────────────────────
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ── PAPER MODE ────────────────────────────────────────────────────────────────
PAPER_MODE          = os.environ.get("PAPER_MODE", "true").lower() == "true"
STARTING_BALANCE    = float(os.environ.get("STARTING_BALANCE", "100.00"))

# ── MARKET SERIES TICKERS ─────────────────────────────────────────────────────
# These are the Kalshi series we actively scan
ACTIVE_SERIES = [
    # NBA
    "KXNBAGAME",      # NBA game winners
    "KXNBATOTAL",     # NBA totals (over/under points)
    "KXNBATEAMTOTAL", # NBA team totals
    # MLB
    "KXMLBGAME",      # MLB game winners
    "KXMLBTOTAL",     # MLB total runs
    # NHL
    "KXNHLGAME",      # NHL game winners
    "KXNHLTOTAL",     # NHL total goals
    # NFL (activates September)
    "KXNFLGAME",      # NFL game winners
    "KXNFLTOTAL",     # NFL totals
    # College Football (activates August)
    "KXNCAAFB",       # NCAAF game winners / totals
    # College Basketball (activates November)
    "KXNCAAMB",       # NCAAM game winners / totals
    # Tennis (year-round Grand Slams)
    "KXTENNIS",       # Tennis match winners
]

# ── SPORT PRIORITY ORDER ──────────────────────────────────────────────────────
# Bot prioritizes sports in this order when building slips
SPORT_PRIORITY = ["NBA", "NFL", "MLB", "NHL", "NCAAFB", "NCAAMB", "SOCCER", "TENNIS"]

# ── SLIP CONFIGURATION ────────────────────────────────────────────────────────

# Daily Picks Slip
DAILY_MIN_LEGS          = 2
DAILY_MAX_LEGS          = 5
DAILY_STAKE_PCT         = 0.10       # 10% of cash reserves
DAILY_HARD_CAP          = 10_000.0   # Max $10,000 per slip
DAILY_MIN_CONFIDENCE    = 7.5        # Minimum confidence per leg
DAILY_MIN_LEG_ODDS      = 1.30       # Minimum odds per leg (~77% implied prob)
DAILY_TARGET_COMBINED   = 2.0        # Target minimum combined odds

# Rollover Slip
ROLLOVER_STAKE_PCT      = 0.10       # 10% of cash reserves
ROLLOVER_HARD_CAP       = 1_000.0    # Max $1,000 starting stake
ROLLOVER_TARGET_ODDS    = 3.0        # Target combined odds per day
ROLLOVER_DAYS           = 5          # Days in rollover sequence
ROLLOVER_MIN_CONFIDENCE = 7.0        # Minimum confidence per leg
ROLLOVER_MIN_LEGS       = 2          # Minimum legs to reach target odds
ROLLOVER_MAX_LEGS       = 4          # Maximum legs per rollover day

# Backtest — slightly looser threshold because we're simulating at 50/50 odds
# (no real pre-game price data), so the AI has less price signal to work with
BACKTEST_MIN_CONFIDENCE = 6.5

# Lotto Slip
LOTTO_STAKE_PCT         = 0.02       # 2% of cash reserves
LOTTO_HARD_CAP          = 50.0       # Max $50 per lotto slip
LOTTO_MIN_ODDS          = 50.0       # Minimum combined odds
LOTTO_MAX_ODDS          = 455.0      # Maximum combined odds
LOTTO_MIN_CONFIDENCE    = 5.0        # More permissive — lottery tier
LOTTO_FREQUENCY_DAYS    = 7          # Generate weekly
LOTTO_MIN_LEGS          = 5          # Need enough legs for high odds
LOTTO_MAX_LEGS          = 12         # Max legs

# ── ROLLOVER CALCULATOR LIMITS ────────────────────────────────────────────────
CALC_MAX_DAYS           = 10
CALC_MAX_ODDS_PER_DAY   = 5.0
CALC_MIN_ODDS_PER_DAY   = 1.10       # Below this = not worth rolling

# ── CALIBRATION SETTINGS ─────────────────────────────────────────────────────
# How much to adjust totals lines toward safer positions
# Higher confidence = smaller buffer (we're more sure)
CALIBRATION_BUFFER = {
    "9.0_plus":  {"total_points": 3,   "total_runs": 0.5},
    "8.0_8.9":   {"total_points": 5,   "total_runs": 0.75},
    "7.5_7.9":   {"total_points": 7,   "total_runs": 1.0},
    "7.0_7.4":   {"total_points": 10,  "total_runs": 1.5},
}

# ── TIMING ────────────────────────────────────────────────────────────────────
# Slip generation window (local time, Eastern US)
GENERATION_HOUR_START   = 8         # 8 AM Eastern
GENERATION_HOUR_END     = 23        # 11 PM Eastern
# Games must start at least this many hours after generation
MIN_HOURS_BEFORE_GAME   = 2

# ── CIRCUIT BREAKER ───────────────────────────────────────────────────────────
CB_MIN_SLIPS            = 5         # Minimum slips before CB can fire
CB_WIN_RATE_THRESHOLD   = 0.40      # Below 40% overall win rate = pause
CB_HALT_HOURS           = 24        # Pause 24 hours when triggered

# ── AI MODEL ──────────────────────────────────────────────────────────────────
AI_MODEL                = "claude-sonnet-4-5"  # Sonnet for sports reasoning
AI_MAX_TOKENS           = 2000

# ── RESEARCH LIMITS ──────────────────────────────────────────────────────────
# Max unique games to research per generation run.
# At ~30k TPM and ~3k tokens/call, 20 games ≈ 2 minutes (no rate limiting).
# Games are sorted by SPORT_PRIORITY then soonest game_start before applying cap.
MAX_RESEARCH_GAMES      = 20

# ── DASHBOARD ─────────────────────────────────────────────────────────────────
DASHBOARD_PORT          = int(os.environ.get("PORT", "8081"))
DASHBOARD_PASSWORD      = os.environ.get("DASHBOARD_PASSWORD", "")

# ── DATABASE ──────────────────────────────────────────────────────────────────
DB_PATH                 = os.environ.get("DB_PATH", "/storage/sports_bot.db")

# ── INVALID COMBO RULES ───────────────────────────────────────────────────────
# Markets that CANNOT be combined in the same slip (same game correlation)
INVALID_COMBO_PAIRS = [
    ("GAME_WINNER", "SPREAD"),      # Same game: winner + spread correlated
    ("SPREAD", "GAME_WINNER"),
]
