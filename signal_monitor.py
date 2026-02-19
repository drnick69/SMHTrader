#!/usr/bin/env python3
"""
SMH Asymmetric Signal Monitor
Locked params: BB(12,σ1.5) Trig:0.5 Mom(ROC25) Flip:3% Def:90% Agg:170% Stop:4×ATR PPI:1
Runs daily via GitHub Actions. Emails on regime change.
"""

import json, os, sys, smtplib, math
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from pathlib import Path

# ── Locked Parameters ──────────────────────────────────
BB_P    = 12
BB_SD   = 1.5
BB_TRIG = 0.5
MOM_P   = 25
MOM_FLIP= 3.0
DEF_PCT = 90
AGG_PCT = 170
STOP_X  = 4
PPI_WT  = 1.0

STATE_FILE = Path(__file__).parent / "state.json"

# ── Data Fetching ──────────────────────────────────────
def fetch_yahoo(symbol, days=120):
    """Fetch daily OHLC from Yahoo Finance v8 (no API key needed)."""
    import urllib.request, urllib.error
    end = int(datetime.now().timestamp())
    start = int((datetime.now() - timedelta(days=days)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={start}&period2={end}&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR fetching {symbol}: {e}")
        return None
    result = data.get("chart", {}).get("result", [])
    if not result:
        print(f"ERROR: no chart data for {symbol}")
        return None
    r = result[0]
    timestamps = r.get("timestamp", [])
    q = r.get("indicators", {}).get("quote", [{}])[0]
    adj = r.get("indicators", {}).get("adjclose", [{}])
    adj_close = adj[0].get("adjclose", q.get("close", [])) if adj else q.get("close", [])
    rows = []
    for i, ts in enumerate(timestamps):
        dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        o = q.get("open", [None]*len(timestamps))[i]
        h = q.get("high", [None]*len(timestamps))[i]
        l = q.get("low", [None]*len(timestamps))[i]
        c = adj_close[i] if i < len(adj_close) else None
        if c is not None and c > 0:
            rows.append({"date": dt, "open": o or c, "high": h or c, "low": l or c, "close": c})
    return rows

def fetch_fred_ppi(api_key):
    """Fetch PCU33443344 (Semi PPI) from FRED."""
    import urllib.request
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id=PCU33443344&sort_order=desc&limit=24&api_key={api_key}&file_type=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR fetching PPI: {e}")
        return None
    rows = []
    for obs in data.get("observations", []):
        try:
            val = float(obs["value"])
            rows.append({"date": obs["date"], "close": val})
        except (ValueError, KeyError):
            continue
    rows.sort(key=lambda r: r["date"])
    return rows

# ── Indicators ─────────────────────────────────────────
def sma(arr, p):
    r = [None]*len(arr)
    for i in range(p-1, len(arr)):
        vals = arr[i-p+1:i+1]
        if None in vals: continue
        r[i] = sum(vals)/p
    return r

def stdev(arr, p):
    m = sma(arr, p)
    r = [None]*len(arr)
    for i in range(p-1, len(arr)):
        if m[i] is None: continue
        vals = arr[i-p+1:i+1]
        if None in vals: continue
        r[i] = math.sqrt(sum((v - m[i])**2 for v in vals) / p)
    return r

def atr(highs, lows, closes, p):
    tr = [None]*len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = (highs[i] - lows[i]) if highs[i] and lows[i] else None
            continue
        if highs[i] is None or lows[i] is None or closes[i-1] is None: continue
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    return sma(tr, p)

def roc(arr, p):
    r = [None]*len(arr)
    for i in range(p, len(arr)):
        if arr[i] is None or arr[i-p] is None or arr[i-p] == 0: continue
        r[i] = ((arr[i] - arr[i-p]) / arr[i-p]) * 100
    return r

# ── Signal Computation ─────────────────────────────────
def compute_signal(smh_rows, qqq_rows=None, ppi_rows=None):
    """Compute current regime signal from market data."""
    # Build aligned series
    if qqq_rows:
        qqq_map = {r["date"]: r["close"] for r in qqq_rows}
        data = []
        for r in smh_rows:
            qc = qqq_map.get(r["date"])
            if qc and qc > 0:
                data.append({**r, "qqq": qc, "ratio": r["close"]/qc})
    else:
        data = [{**r, "qqq": None, "ratio": None} for r in smh_rows]

    if len(data) < 50:
        return None, "Not enough data"

    closes = [d["close"] for d in data]
    highs  = [d["high"] for d in data]
    lows   = [d["low"] for d in data]
    bb_series = [d["ratio"] for d in data] if qqq_rows else closes

    bb_m  = sma(bb_series, BB_P)
    bb_sd = stdev(bb_series, BB_P)
    atr14 = atr(highs, lows, closes, 14)
    mom   = roc(closes, MOM_P)

    # PPI momentum (3-month change)
    ppi_tilt = 0.0
    if ppi_rows and len(ppi_rows) >= 4:
        sorted_ppi = sorted(ppi_rows, key=lambda r: r["date"])
        latest = sorted_ppi[-1]["close"]
        three_mo_ago = sorted_ppi[-4]["close"] if len(sorted_ppi) >= 4 else sorted_ppi[0]["close"]
        if three_mo_ago > 0:
            ppi_mom = ((latest - three_mo_ago) / three_mo_ago) * 100
            ppi_tilt = max(-1.0, min(1.0, ppi_mom / 3.0))

    # ATR percentiles for adaptive bands
    atr_vals = sorted([v for v in atr14 if v is not None])
    p75 = atr_vals[int(len(atr_vals)*0.75)] if atr_vals else 999
    p25 = atr_vals[int(len(atr_vals)*0.25)] if atr_vals else 0

    idx = len(data) - 1
    if bb_m[idx] is None or bb_sd[idx] is None or atr14[idx] is None or mom[idx] is None:
        return None, "Indicators not ready"

    ca = atr14[idx]
    esd = BB_SD
    if ca > p75: esd = BB_SD + 0.5
    elif ca < p25: esd = max(1.0, BB_SD - 0.5)

    upper = bb_m[idx] + esd * bb_sd[idx]
    lower = bb_m[idx] - esd * bb_sd[idx]
    rng = upper - lower
    bb_z = (bb_series[idx] - bb_m[idx]) / (rng/2) if rng > 0 else 0
    mom_v = mom[idx]

    agg_trig = -(BB_TRIG - ppi_tilt * PPI_WT * 0.3)
    def_trig = BB_TRIG + ppi_tilt * PPI_WT * 0.3

    if bb_z < agg_trig and mom_v > MOM_FLIP:
        regime, target = "AGG", AGG_PCT
    elif bb_z > def_trig and mom_v < -MOM_FLIP:
        regime, target = "DEF", DEF_PCT
    else:
        regime, target = "HOLD", 100

    stop_level = None
    if regime == "AGG" and STOP_X > 0:
        stop_level = round(closes[idx] - STOP_X * ca, 2)

    return {
        "regime": regime,
        "target": target,
        "price": round(closes[idx], 2),
        "date": data[idx]["date"],
        "bb_z": round(bb_z, 3),
        "mom": round(mom_v, 2),
        "ppi_tilt": round(ppi_tilt, 2),
        "agg_trig": round(agg_trig, 2),
        "def_trig": round(def_trig, 2),
        "atr": round(ca, 2),
        "stop_level": stop_level,
    }, None

# ── State Management ───────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"regime": None, "date": None, "price": None}

def save_state(signal):
    STATE_FILE.write_text(json.dumps({
        "regime": signal["regime"],
        "date": signal["date"],
        "price": signal["price"],
        "target": signal["target"],
        "bb_z": signal["bb_z"],
        "mom": signal["mom"],
        "updated": datetime.now().isoformat(),
    }, indent=2))

# ── Email Alert ────────────────────────────────────────
def send_email(signal, prev_regime):
    sender   = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_addr  = os.environ.get("ALERT_EMAIL", sender)

    if not sender or not password:
        print("WARNING: Gmail credentials not set, skipping email")
        return False

    emoji = {"AGG": "🔥", "DEF": "🛡", "HOLD": "📊"}.get(signal["regime"], "⚠️")
    subject = f"{emoji} SMH Signal: {prev_regime or 'INIT'} → {signal['regime']} ({signal['target']}%)"

    body = f"""SMH Asymmetric Signal Change
{'='*40}

Regime:  {prev_regime or 'NONE'} → {signal['regime']}
Target:  {signal['target']}% allocation
Price:   ${signal['price']}
Date:    {signal['date']}

Signal Details:
  BB Z-Score:  {signal['bb_z']}  (Agg trigger: {signal['agg_trig']}, Def trigger: {signal['def_trig']})
  Momentum:    {signal['mom']}%  (Flip: ±{MOM_FLIP}%)
  PPI Tilt:    {signal['ppi_tilt']}
  ATR(14):     ${signal['atr']}
  Stop Level:  {'$' + str(signal['stop_level']) if signal['stop_level'] else 'N/A (not in AGG)'}

Action Required:
"""
    if signal["regime"] == "AGG":
        body += f"  → INCREASE position to {signal['target']}% of portfolio value\n"
        body += f"  → Set stop-loss at ${signal['stop_level']}\n"
    elif signal["regime"] == "DEF":
        body += f"  → TRIM position to {signal['target']}% of portfolio value\n"
    else:
        body += f"  → HOLD at 100% baseline. No action needed.\n"

    body += f"""
Locked Parameters:
  BB(12, σ1.5) Trig:0.5 Mom(ROC25) Flip:3%
  Def:90% Base:100% Agg:170% Stop:4×ATR PPI:1

---
SMH Asymmetric Signal Monitor (automated)
"""

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"Email sent: {subject}")
        return True
    except Exception as e:
        print(f"ERROR sending email: {e}")
        return False

# ── SMS Alert (optional Twilio) ────────────────────────
def send_sms(signal, prev_regime):
    acct_sid   = os.environ.get("TWILIO_SID", "")
    auth_token = os.environ.get("TWILIO_TOKEN", "")
    from_num   = os.environ.get("TWILIO_FROM", "")
    to_num     = os.environ.get("TWILIO_TO", "")

    if not all([acct_sid, auth_token, from_num, to_num]):
        return False

    emoji = {"AGG": "🔥", "DEF": "🛡", "HOLD": "📊"}.get(signal["regime"], "⚠️")
    body = (
        f"{emoji} SMH: {prev_regime}→{signal['regime']} "
        f"Target:{signal['target']}% "
        f"Price:${signal['price']} "
        f"BBz:{signal['bb_z']} Mom:{signal['mom']}%"
    )
    if signal["stop_level"]:
        body += f" Stop:${signal['stop_level']}"

    import urllib.request, urllib.parse, base64
    url = f"https://api.twilio.com/2010-04-01/Accounts/{acct_sid}/Messages.json"
    data = urllib.parse.urlencode({"To": to_num, "From": from_num, "Body": body}).encode()
    req = urllib.request.Request(url, data=data)
    cred = base64.b64encode(f"{acct_sid}:{auth_token}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"SMS sent: {resp.status}")
        return True
    except Exception as e:
        print(f"SMS error: {e}")
        return False

# ── Main ───────────────────────────────────────────────
def main():
    fred_key = os.environ.get("FRED_API_KEY", "")
    force    = "--force" in sys.argv
    daily    = "--daily" in sys.argv  # send daily status even without change

    print(f"[{datetime.now().isoformat()}] SMH Signal Monitor starting...")

    # Fetch data
    print("Fetching SMH...")
    smh = fetch_yahoo("SMH", days=120)
    if not smh:
        print("FATAL: could not fetch SMH"); sys.exit(1)
    print(f"  SMH: {len(smh)} bars, latest {smh[-1]['date']} @ ${smh[-1]['close']:.2f}")

    print("Fetching QQQ...")
    qqq = fetch_yahoo("QQQ", days=120)
    if qqq:
        print(f"  QQQ: {len(qqq)} bars")

    ppi = None
    if fred_key:
        print("Fetching Semi PPI...")
        ppi = fetch_fred_ppi(fred_key)
        if ppi:
            print(f"  PPI: {len(ppi)} readings, latest {ppi[-1]['date']}: {ppi[-1]['close']}")

    # Compute signal
    signal, err = compute_signal(smh, qqq, ppi)
    if err:
        print(f"ERROR computing signal: {err}"); sys.exit(1)

    print(f"\nSIGNAL: {signal['regime']} → {signal['target']}%")
    print(f"  Price: ${signal['price']}  BBz: {signal['bb_z']}  Mom: {signal['mom']}%  PPI: {signal['ppi_tilt']}")
    if signal["stop_level"]:
        print(f"  Stop: ${signal['stop_level']}")

    # Check for regime change
    prev = load_state()
    changed = prev["regime"] != signal["regime"]

    if changed:
        print(f"\n*** REGIME CHANGE: {prev['regime'] or 'INIT'} → {signal['regime']} ***")
        send_email(signal, prev.get("regime", "INIT"))
        send_sms(signal, prev.get("regime", "INIT"))
    elif daily:
        print(f"\nNo change (still {signal['regime']}), sending daily status...")
        send_email(signal, signal["regime"])
    elif force:
        print(f"\nForced alert (no change, still {signal['regime']})")
        send_email(signal, signal["regime"])
    else:
        print(f"\nNo change (still {signal['regime']}). No alert sent.")

    save_state(signal)
    print("State saved. Done.")

if __name__ == "__main__":
    main()
