"""
SIKANDAR BOT — Flask API Wrapper
Railway pe yeh file chalti hai aur signals ko website pe bhejti hai
"""

from __future__ import annotations
import os, sys, json, time, math, logging, threading, traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import deque, defaultdict
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Auto-install ───────────────────────────────────────────────────────────────
try:
    import pandas as pd
    import numpy as np
    import requests
    from websocket import WebSocketApp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "websocket-client", "requests", "pandas", "numpy",
                           "flask", "flask-cors"])
    import pandas as pd
    import numpy as np
    import requests
    from websocket import WebSocketApp

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
CLAUDE_API_KEY     = os.getenv("CLAUDE_API_KEY", "")

CONFIG = {
    "WATCHLIST": [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
    ],
    "TOP_PICKS"              : 5,
    "MIN_CONFIDENCE"         : 45,
    "SIGNAL_COOLDOWN"        : 60,
    "INTERVAL"               : "1m",
    "MAX_DAILY_SIGNALS"      : 50,
    "MAX_CONSECUTIVE_LOSSES" : 6,
    "RISK_PER_TRADE_PCT"     : 2.0,
    "BUFFER_SIZE"            : 500,
    "AI_ANALYSIS_INTERVAL"   : 300,
    "NEWS_INTERVAL"          : 600,
    "SCAN_INTERVAL"          : 30,
    "FORCE_SIGNAL_INTERVAL"  : 300,
    "SCORE_THRESHOLD"        : 12,
    "LOG_FILE"               : "sikandar_bot.log",
    "STATS_FILE"             : "sikandar_stats.json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("SIKANDAR_BOT")

# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL STORE — website ke liye signals yahan store honge
# ══════════════════════════════════════════════════════════════════════════════
signal_store: List[Dict] = []
MAX_SIGNALS = 50  # website pe maximum kitne signals dikhayein

def add_signal(signal_dict: Dict):
    """Naya signal store mein add karo"""
    signal_store.insert(0, signal_dict)
    if len(signal_store) > MAX_SIGNALS:
        signal_store.pop()

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self.base    = f"https://api.telegram.org/bot{token}"
        self._enabled = bool(token and chat_id)

    @property
    def enabled(self): return self._enabled

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self._enabled: return False
        try:
            r = requests.post(f"{self.base}/sendMessage", json={
                "chat_id": self.chat_id, "text": text[:4096],
                "parse_mode": parse_mode, "disable_web_page_preview": True,
            }, timeout=30)
            return r.status_code == 200
        except Exception as e:
            log.warning(f"Telegram error: {e}"); return False

# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series):
    fast = ema(series, 12); slow = ema(series, 26)
    line = fast - slow; signal = ema(line, 9)
    return line, signal, line - signal

def bollinger(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = series.rolling(period).mean()
    dev = series.rolling(period).std()
    return mid + std * dev, mid, mid - std * dev

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def stochastic(df: pd.DataFrame, k=14, d=3):
    low_min  = df["low"].rolling(k).min()
    high_max = df["high"].rolling(k).max()
    k_line   = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-10)
    return k_line, k_line.rolling(d).mean()

def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hh = df["high"].rolling(period).max()
    ll = df["low"].rolling(period).min()
    return -100 * (hh - df["close"]) / (hh - ll + 1e-10)

def obv(df: pd.DataFrame) -> pd.Series:
    direction = df["close"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (df["volume"] * direction).cumsum()

def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum()

def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(lambda x: (x - x.mean()).abs().mean())
    return (tp - ma) / (0.015 * md + 1e-10)

def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up   = df["high"].diff(); down = -df["low"].diff()
    pdm  = up.where((up > down) & (up > 0), 0)
    ndm  = down.where((down > up) & (down > 0), 0)
    atr_ = atr(df, period)
    pdi  = 100 * ema(pdm, period) / (atr_ + 1e-10)
    ndi  = 100 * ema(ndm, period) / (atr_ + 1e-10)
    dx   = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-10)
    return ema(dx, period)

# ══════════════════════════════════════════════════════════════════════════════
#  BINANCE DATA MANAGER
# ══════════════════════════════════════════════════════════════════════════════
class BinanceDataManager:
    KLINE_URL = "https://api.binance.com/api/v3/klines"
    WS_BASE   = "wss://stream.binance.com:9443/stream?streams="

    def __init__(self, symbols: List[str], interval: str, callback):
        self.symbols      = symbols
        self.interval     = interval
        self.callback     = callback
        self.candle_data  = {s: deque(maxlen=CONFIG["BUFFER_SIZE"]) for s in symbols}
        self._ws: Optional[WebSocketApp] = None
        self._running     = False

    def _fetch_rest(self, symbol: str):
        try:
            r = requests.get(self.KLINE_URL, params={
                "symbol": symbol, "interval": self.interval, "limit": 200
            }, timeout=15)
            if r.status_code == 200:
                for k in r.json():
                    self.candle_data[symbol].append({
                        "time": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                    })
        except Exception as e:
            log.warning(f"REST fetch {symbol}: {e}")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "data" not in data: return
            d = data["data"]
            if d.get("e") != "kline": return
            k = d["k"]
            sym = k["s"]
            if sym in self.candle_data:
                candle = {
                    "time": k["t"], "open": float(k["o"]), "high": float(k["h"]),
                    "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
                }
                candles = self.candle_data[sym]
                if candles and candles[-1]["time"] == candle["time"]:
                    candles[-1] = candle
                else:
                    candles.append(candle)
                self.callback(sym, candle)
        except Exception as e:
            log.warning(f"WS message error: {e}")

    def _on_error(self, ws, error):
        log.warning(f"WS error: {error}")

    def _on_close(self, ws, *args):
        if self._running:
            log.info("WS closed — reconnecting in 5s")
            time.sleep(5); self._start_ws()

    def _start_ws(self):
        streams = "/".join(f"{s.lower()}@kline_{self.interval}" for s in self.symbols)
        url = self.WS_BASE + streams
        self._ws = WebSocketApp(url, on_message=self._on_message,
                                on_error=self._on_error, on_close=self._on_close)
        t = threading.Thread(target=self._ws.run_forever, daemon=True)
        t.start()

    def start(self):
        self._running = True
        log.info("Fetching initial candle data via REST...")
        for sym in self.symbols:
            self._fetch_rest(sym)
            time.sleep(0.2)
        log.info("Starting WebSocket stream...")
        self._start_ws()

    def stop(self):
        self._running = False
        if self._ws:
            try: self._ws.close()
            except: pass

    def get_df(self, symbol: str) -> Optional[pd.DataFrame]:
        candles = list(self.candle_data.get(symbol, []))
        if len(candles) < 50: return None
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df.set_index("time")

# ══════════════════════════════════════════════════════════════════════════════
#  RISK MANAGER
# ══════════════════════════════════════════════════════════════════════════════
class RiskManager:
    def __init__(self):
        self.daily_signals     = 0
        self.consecutive_losses = 0
        self.last_reset        = datetime.now().date()
        self._lock             = threading.Lock()

    def _check_reset(self):
        today = datetime.now().date()
        if today != self.last_reset:
            self.daily_signals = 0
            self.consecutive_losses = 0
            self.last_reset = today

    def can_trade(self) -> Tuple[bool, str]:
        with self._lock:
            self._check_reset()
            if self.daily_signals >= CONFIG["MAX_DAILY_SIGNALS"]:
                return False, f"Daily limit reached ({CONFIG['MAX_DAILY_SIGNALS']})"
            if self.consecutive_losses >= CONFIG["MAX_CONSECUTIVE_LOSSES"]:
                return False, f"Consecutive loss limit ({CONFIG['MAX_CONSECUTIVE_LOSSES']})"
            return True, "OK"

    def record_signal(self):
        with self._lock:
            self._check_reset()
            self.daily_signals += 1

    def summary(self) -> str:
        return (f"Signals: {self.daily_signals}/{CONFIG['MAX_DAILY_SIGNALS']} | "
                f"Losses: {self.consecutive_losses}/{CONFIG['MAX_CONSECUTIVE_LOSSES']}")

# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
class AssetScanner:
    def scan_asset(self, symbol: str, df: pd.DataFrame,
                   fear_greed: Dict, force: bool = False) -> Optional[Dict]:
        try:
            close  = df["close"]; volume = df["volume"]
            ema9   = ema(close, 9);  ema21  = ema(close, 21)
            ema50  = ema(close, 50); ema200 = ema(close, 200)
            rsi14  = rsi(close, 14)
            macd_l, macd_s, macd_h = macd(close)
            bb_up, bb_mid, bb_low   = bollinger(close)
            atr14  = atr(df, 14)
            stoch_k, stoch_d = stochastic(df)
            willy  = williams_r(df)
            obv_   = obv(df); vwap_  = vwap(df)
            cci20  = cci(df); adx14  = adx(df)

            last = -1
            price   = close.iloc[last]
            rsi_val = rsi14.iloc[last]
            macd_val= macd_l.iloc[last]; macd_sig= macd_s.iloc[last]
            macd_hist=macd_h.iloc[last]
            bb_u    = bb_up.iloc[last]; bb_l = bb_low.iloc[last]
            stk     = stoch_k.iloc[last]; std_ = stoch_d.iloc[last]
            wr      = willy.iloc[last]
            obv_d   = obv_.diff().iloc[last]
            cci_val = cci20.iloc[last]
            adx_val = adx14.iloc[last]
            atr_val = atr14.iloc[last]
            vol_avg = volume.rolling(20).mean().iloc[last]
            vol_cur = volume.iloc[last]

            buy_score = 0; sell_score = 0

            # Trend
            if ema9.iloc[last] > ema21.iloc[last] > ema50.iloc[last]:  buy_score  += 3
            if ema9.iloc[last] < ema21.iloc[last] < ema50.iloc[last]:  sell_score += 3
            if price > ema200.iloc[last]:                               buy_score  += 2
            if price < ema200.iloc[last]:                               sell_score += 2

            # Momentum
            if rsi_val < 30:  buy_score  += 3
            elif rsi_val < 45: buy_score += 1
            if rsi_val > 70:  sell_score += 3
            elif rsi_val > 55: sell_score += 1

            # MACD
            if macd_val > macd_sig and macd_hist > 0: buy_score  += 2
            if macd_val < macd_sig and macd_hist < 0: sell_score += 2

            # Bollinger
            if price <= bb_l: buy_score  += 2
            if price >= bb_u: sell_score += 2

            # Stochastic
            if stk < 20 and std_ < 20: buy_score  += 2
            if stk > 80 and std_ > 80: sell_score += 2

            # Williams %R
            if wr < -80: buy_score  += 1
            if wr > -20: sell_score += 1

            # OBV
            if obv_d > 0: buy_score  += 1
            if obv_d < 0: sell_score += 1

            # VWAP
            if price > vwap_.iloc[last]: buy_score  += 1
            if price < vwap_.iloc[last]: sell_score += 1

            # CCI
            if cci_val < -100: buy_score  += 1
            if cci_val > 100:  sell_score += 1

            # Volume confirmation
            if vol_cur > vol_avg * 1.5: 
                if buy_score > sell_score:  buy_score  += 1
                else:                       sell_score += 1

            MAX_SCORE = 23
            total = buy_score + sell_score
            if total == 0: return None

            if buy_score > sell_score:
                direction = "BUY"; score = buy_score
            else:
                direction = "SELL"; score = sell_score

            confidence = min(95, (score / MAX_SCORE) * 100)
            threshold  = CONFIG["SCORE_THRESHOLD"] if not force else 5
            min_conf   = CONFIG["MIN_CONFIDENCE"] if not force else 30

            if score < threshold or confidence < min_conf:
                return None

            sl_pct  = atr_val / price * 1.5
            tp1_pct = sl_pct * 1.5
            tp2_pct = sl_pct * 3.0

            if direction == "BUY":
                sl   = price * (1 - sl_pct)
                tp1  = price * (1 + tp1_pct)
                tp2  = price * (1 + tp2_pct)
            else:
                sl   = price * (1 + sl_pct)
                tp1  = price * (1 - tp1_pct)
                tp2  = price * (1 - tp2_pct)

            patterns = []
            if rsi_val < 30:     patterns.append("Oversold RSI")
            if rsi_val > 70:     patterns.append("Overbought RSI")
            if price <= bb_l:    patterns.append("BB Lower Touch")
            if price >= bb_u:    patterns.append("BB Upper Touch")
            if stk < 20:         patterns.append("Stoch Oversold")
            if stk > 80:         patterns.append("Stoch Overbought")
            if macd_hist > 0 and macd_val > macd_sig: patterns.append("MACD Bullish")
            if macd_hist < 0 and macd_val < macd_sig: patterns.append("MACD Bearish")
            if adx_val > 25:     patterns.append(f"Strong Trend ADX={adx_val:.0f}")

            return {
                "symbol"    : symbol,
                "direction" : direction,
                "confidence": round(confidence, 1),
                "score"     : round(score, 1),
                "price"     : round(price, 6),
                "sl"        : round(sl, 6),
                "tp1"       : round(tp1, 6),
                "tp2"       : round(tp2, 6),
                "rsi"       : round(rsi_val, 1),
                "patterns"  : patterns[:4],
                "atr"       : round(atr_val, 6),
                "adx"       : round(adx_val, 1),
                "vol_ratio" : round(vol_cur / (vol_avg + 1e-10), 2),
            }
        except Exception as e:
            log.warning(f"scan_asset {symbol}: {e}")
            return None

    def get_top_picks(self, asset_data: Dict, fear_greed: Dict, force=False):
        results = []
        for symbol, df in asset_data.items():
            r = self.scan_asset(symbol, df, fear_greed, force=force)
            if r and r["direction"] != "WAIT":
                results.append(r)
        results.sort(key=lambda x: x["confidence"], reverse=True)
        return results[:CONFIG["TOP_PICKS"]]

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN BOT
# ══════════════════════════════════════════════════════════════════════════════
class SikandarBot:
    def __init__(self):
        self.telegram        = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.risk_mgr        = RiskManager()
        self.scanner         = AssetScanner()
        self.data_mgr        = None
        self.fear_greed      = {"value": 50, "label": "Neutral"}
        self.last_signal_time= {}
        self.last_scan_time  = datetime.now() - timedelta(seconds=60)
        self.last_force_signal = datetime.now() - timedelta(seconds=300)
        self.last_news_clear = datetime.now()
        self._running        = False

    def _get_asset_data(self):
        data = {}
        for sym in CONFIG["WATCHLIST"]:
            df = self.data_mgr.get_df(sym)
            if df is not None:
                data[sym] = df
        return data

    def _emit_signal(self, pick: Dict, forced: bool = False):
        sym = pick["symbol"]
        direction = pick["direction"]
        confidence = pick["confidence"]
        price = pick["price"]

        emoji = "🟢" if direction == "BUY" else "🔴"
        forced_tag = " ⚡FORCED" if forced else ""

        signal_dict = {
            "id"        : int(time.time() * 1000),
            "time"      : datetime.now().strftime("%H:%M:%S"),
            "date"      : datetime.now().strftime("%Y-%m-%d"),
            "symbol"    : sym,
            "direction" : direction,
            "confidence": confidence,
            "score"     : pick["score"],
            "price"     : price,
            "sl"        : pick["sl"],
            "tp1"       : pick["tp1"],
            "tp2"       : pick["tp2"],
            "rsi"       : pick["rsi"],
            "adx"       : pick["adx"],
            "vol_ratio" : pick["vol_ratio"],
            "patterns"  : pick["patterns"],
            "forced"    : forced,
        }

        # Website ke signal store mein add karo
        add_signal(signal_dict)

        # Telegram alert
        msg = (
            f"{emoji} <b>{direction}{forced_tag} — {sym}</b>\n"
            f"💰 Price: <code>{price}</code>\n"
            f"🎯 Confidence: {confidence}%\n"
            f"🛑 Stop Loss: <code>{pick['sl']}</code>\n"
            f"✅ TP1: <code>{pick['tp1']}</code>\n"
            f"✅ TP2: <code>{pick['tp2']}</code>\n"
            f"📊 RSI: {pick['rsi']} | ADX: {pick['adx']}\n"
            f"📈 Volume: {pick['vol_ratio']}x avg\n"
            f"🔍 {', '.join(pick['patterns']) if pick['patterns'] else 'N/A'}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )

        if self.telegram.enabled:
            self.telegram.send(msg)

        self.risk_mgr.record_signal()
        self.last_signal_time[sym] = datetime.now()
        log.info(f"Signal: {sym} {direction} {confidence}%")

    def _run_scan(self, force: bool = False):
        asset_data  = self._get_asset_data()
        if len(asset_data) < 3:
            return 0

        top_picks = self.scanner.get_top_picks(asset_data, self.fear_greed, force=force)
        signals_sent = 0

        for pick in top_picks:
            sym = pick["symbol"]
            ok, why = self.risk_mgr.can_trade()
            if not ok:
                break

            last = self.last_signal_time.get(sym)
            if last and (datetime.now() - last).seconds < CONFIG["SIGNAL_COOLDOWN"]:
                if not force:
                    continue

            self._emit_signal(pick, forced=force)
            signals_sent += 1
            if signals_sent >= 2:
                break

        if signals_sent == 0 and force:
            all_results = []
            for symbol, df in asset_data.items():
                try:
                    result = self.scanner.scan_asset(symbol, df, self.fear_greed, force=True)
                    if result and result["direction"] != "WAIT":
                        all_results.append(result)
                except Exception:
                    pass
            if all_results:
                all_results.sort(key=lambda x: x["score"], reverse=True)
                self._emit_signal(all_results[0], forced=True)

        return signals_sent

    def _main_loop(self):
        while self._running:
            try:
                time.sleep(1)
                scan_due = (datetime.now() - self.last_scan_time).total_seconds() >= CONFIG["SCAN_INTERVAL"]
                if scan_due:
                    self.last_scan_time = datetime.now()
                    self._run_scan(force=False)

                force_due = (datetime.now() - self.last_force_signal).total_seconds() >= CONFIG["FORCE_SIGNAL_INTERVAL"]
                if force_due:
                    self.last_force_signal = datetime.now()
                    self._run_scan(force=True)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Main loop error: {e}")

    def start_background(self):
        self.data_mgr = BinanceDataManager(
            CONFIG["WATCHLIST"], CONFIG["INTERVAL"], lambda s, c: None
        )
        self.data_mgr.start()

        # Data load hone ka wait karo
        for _ in range(30):
            ready = sum(1 for s in CONFIG["WATCHLIST"]
                        if len(self.data_mgr.candle_data.get(s, [])) >= 50)
            if ready >= 5:
                break
            time.sleep(1)

        if self.telegram.enabled:
            self.telegram.send(
                f"✅ <b>SIKANDAR Bot Online!</b>\n"
                f"🌐 Web Dashboard Active\n"
                f"📊 Monitoring {len(CONFIG['WATCHLIST'])} assets\n"
                f"⏱️ Scan every {CONFIG['SCAN_INTERVAL']}s"
            )

        self._running = True
        t = threading.Thread(target=self._main_loop, daemon=True)
        t.start()
        log.info("Bot started in background thread")

# ══════════════════════════════════════════════════════════════════════════════
#  FLASK API
# ══════════════════════════════════════════════════════════════════════════════
app  = Flask(__name__)
CORS(app)  # WordPress website ko allow karo
bot  = SikandarBot()

@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "bot"   : "SIKANDAR AI Trading Bot",
        "assets": len(CONFIG["WATCHLIST"]),
        "time"  : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/api/signals")
def get_signals():
    """Website se yeh URL call karega aur signals milenge"""
    limit = min(int(request.args.get("limit", 20)), 50)
    return jsonify({
        "status" : "ok",
        "count"  : len(signal_store[:limit]),
        "signals": signal_store[:limit],
        "updated": datetime.now().strftime("%H:%M:%S"),
    })

@app.route("/api/status")
def get_status():
    """Bot ka current status"""
    asset_data = bot._get_asset_data() if bot.data_mgr else {}
    return jsonify({
        "status"        : "online",
        "assets_ready"  : len(asset_data),
        "total_assets"  : len(CONFIG["WATCHLIST"]),
        "signals_today" : bot.risk_mgr.daily_signals,
        "last_scan"     : bot.last_scan_time.strftime("%H:%M:%S"),
        "time"          : datetime.now().strftime("%H:%M:%S"),
    })

@app.route("/api/watchlist")
def get_watchlist():
    return jsonify({"watchlist": CONFIG["WATCHLIST"]})

# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("Starting SIKANDAR Bot...")
    bot.start_background()

    port = int(os.environ.get("PORT", 5000))
    log.info(f"Flask API starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
