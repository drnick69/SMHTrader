#!/usr/bin/env python3
"""
SMH + SOXL Signal Monitor
─────────────────────────
SMH Asymmetric: BB(12,σ1.5) Trig:0.5 Mom(ROC25) Flip:3% Def:90% Agg:170% Stop:4×ATR PPI:1
SOXL Sniper:    BB(15,σ1.5) Trig:0.5 Mom(ROC15) Flip:2% Tier:cons(25/50/80) Prof:20% ExZ:0 Cool:5d

Runs daily via GitHub Actions. Emails on regime change for either strategy.
"""

import json, os, sys, smtplib, math
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from pathlib import Path

# ═══════════════════════════════════════════════════════
# LOCKED PARAMETERS
# ═══════════════════════════════════════════════════════

# SMH Asymmetric
SMH_BB_P     = 12
SMH_BB_SD    = 1.5
SMH_BB_TRIG  = 0.5
SMH_MOM_P    = 25
SMH_MOM_FLIP = 3.0
SMH_DEF_PCT  = 90
SMH_AGG_PCT  = 170
SMH_STOP_X   = 4
SMH_PPI_WT   = 1.0

# SOXL Sniper
SOX_BB_P     = 15
SOX_BB_SD    = 1.5
SOX_BB_TRIG  = 0.5
SOX_MOM_P    = 15
SOX_MOM_FLIP = 2.0
SOX_T1_PCT   = 25
SOX_T2_PCT   = 50
SOX_T3_PCT   = 80
SOX_T2_MULT  = 1.5
SOX_T3_MULT  = 2.0
SOX_PROFIT   = 20.0
SOX_EXIT_Z   = 0.0
SOX_COOL     = 5

STATE_FILE = Path(__file__).parent / "state.json"

# ═══════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════
def fetch_yahoo(symbol, days=120):
    import urllib.request
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

# ═══════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════
def ind_sma(arr, p):
    r = [None]*len(arr)
    for i in range(p-1, len(arr)):
        vals = arr[i-p+1:i+1]
        if None in vals: continue
        r[i] = sum(vals)/p
    return r

def ind_stdev(arr, p):
    m = ind_sma(arr, p)
    r = [None]*len(arr)
    for i in range(p-1, len(arr)):
        if m[i] is None: continue
        vals = arr[i-p+1:i+1]
        if None in vals: continue
        r[i] = math.sqrt(sum((v - m[i])**2 for v in vals) / p)
    return r

def ind_atr(highs, lows, closes, p):
    tr = [None]*len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = (highs[i] - lows[i]) if highs[i] and lows[i] else None
            continue
        if highs[i] is None or lows[i] is None or closes[i-1] is None: continue
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    return ind_sma(tr, p)

def ind_roc(arr, p):
    r = [None]*len(arr)
    for i in range(p, len(arr)):
        if arr[i] is None or arr[i-p] is None or arr[i-p] == 0: continue
        r[i] = ((arr[i] - arr[i-p]) / arr[i-p]) * 100
    return r

# ═══════════════════════════════════════════════════════
# SMH SIGNAL
# ═══════════════════════════════════════════════════════
def compute_smh_signal(smh, qqq=None, ppi=None):
    hasQQQ = qqq and len(qqq) > 0
    if hasQQQ:
        qqq_map = {r["date"]: r["close"] for r in qqq}
        data = []
        for r in smh:
            qc = qqq_map.get(r["date"])
            if qc and qc > 0:
                data.append({**r, "ratio": r["close"]/qc})
    else:
        data = [{**r, "ratio": None} for r in smh]
    if len(data) < 50: return None, "Not enough data"

    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    bb_series = [d["ratio"] for d in data] if hasQQQ else closes

    bb_m = ind_sma(bb_series, SMH_BB_P)
    bb_sd = ind_stdev(bb_series, SMH_BB_P)
    atr14 = ind_atr(highs, lows, closes, 14)
    mom = ind_roc(closes, SMH_MOM_P)

    ppi_tilt = 0.0
    if ppi and len(ppi) >= 4:
        sorted_ppi = sorted(ppi, key=lambda r: r["date"])
        latest = sorted_ppi[-1]["close"]
        three_mo = sorted_ppi[-4]["close"] if len(sorted_ppi) >= 4 else sorted_ppi[0]["close"]
        if three_mo > 0:
            ppi_mom = ((latest - three_mo) / three_mo) * 100
            ppi_tilt = max(-1.0, min(1.0, ppi_mom / 3.0))

    atr_vals = sorted([v for v in atr14 if v is not None])
    p75 = atr_vals[int(len(atr_vals)*0.75)] if atr_vals else 999
    p25 = atr_vals[int(len(atr_vals)*0.25)] if atr_vals else 0

    idx = len(data) - 1
    if bb_m[idx] is None or bb_sd[idx] is None or atr14[idx] is None or mom[idx] is None:
        return None, "Indicators not ready"

    ca = atr14[idx]
    esd = SMH_BB_SD
    if ca > p75: esd = SMH_BB_SD + 0.5
    elif ca < p25: esd = max(1.0, SMH_BB_SD - 0.5)

    rng = 2 * esd * bb_sd[idx] if bb_sd[idx] > 0 else 1
    bb_z = (bb_series[idx] - bb_m[idx]) / (rng/2) if rng > 0 else 0
    mom_v = mom[idx]

    agg_trig = -(SMH_BB_TRIG - ppi_tilt * SMH_PPI_WT * 0.3)
    def_trig = SMH_BB_TRIG + ppi_tilt * SMH_PPI_WT * 0.3

    if bb_z < agg_trig and mom_v > SMH_MOM_FLIP:
        regime, target = "AGG", SMH_AGG_PCT
    elif bb_z > def_trig and mom_v < -SMH_MOM_FLIP:
        regime, target = "DEF", SMH_DEF_PCT
    else:
        regime, target = "HOLD", 100

    stop_level = None
    if regime == "AGG" and SMH_STOP_X > 0:
        stop_level = round(closes[idx] - SMH_STOP_X * ca, 2)

    return {
        "regime": regime, "target": target, "price": round(closes[idx], 2),
        "date": data[idx]["date"], "bb_z": round(bb_z, 3), "mom": round(mom_v, 2),
        "ppi_tilt": round(ppi_tilt, 2), "agg_trig": round(agg_trig, 2),
        "def_trig": round(def_trig, 2), "atr": round(ca, 2), "stop_level": stop_level,
    }, None

# ═══════════════════════════════════════════════════════
# SOXL SIGNAL
# ═══════════════════════════════════════════════════════
def compute_soxl_signal(smh, soxl, qqq=None, prev_state=None):
    hasQQQ = qqq and len(qqq) > 0
    soxl_map = {r["date"]: r for r in soxl}
    if hasQQQ:
        qqq_map = {r["date"]: r["close"] for r in qqq}
        data = []
        for r in smh:
            sx = soxl_map.get(r["date"])
            qc = qqq_map.get(r["date"])
            if sx and qc and qc > 0:
                data.append({**r, "soxl": sx["close"], "ratio": r["close"]/qc})
    else:
        data = []
        for r in smh:
            sx = soxl_map.get(r["date"])
            if sx:
                data.append({**r, "soxl": sx["close"], "ratio": None})
    if len(data) < 50: return None, "Not enough SOXL data"

    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    bb_series = [d["ratio"] for d in data] if hasQQQ else closes

    bb_m = ind_sma(bb_series, SOX_BB_P)
    bb_sd = ind_stdev(bb_series, SOX_BB_P)
    atr14 = ind_atr(highs, lows, closes, 14)
    mom = ind_roc(closes, SOX_MOM_P)

    atr_vals = sorted([v for v in atr14 if v is not None])
    p75 = atr_vals[int(len(atr_vals)*0.75)] if atr_vals else 999
    p25 = atr_vals[int(len(atr_vals)*0.25)] if atr_vals else 0

    idx = len(data) - 1
    if bb_m[idx] is None or bb_sd[idx] is None or atr14[idx] is None or mom[idx] is None:
        return None, "SOXL indicators not ready"

    ca = atr14[idx]
    esd = SOX_BB_SD
    if ca > p75: esd = SOX_BB_SD + 0.5
    elif ca < p25: esd = max(1.0, SOX_BB_SD - 0.5)

    rng = 2 * esd * bb_sd[idx] if bb_sd[idx] > 0 else 1
    bb_z = (bb_series[idx] - bb_m[idx]) / (rng/2) if rng > 0 else 0
    mom_v = mom[idx]
    agg_trig = -SOX_BB_TRIG

    # Determine tier depth
    tier = None
    if bb_z < agg_trig * SOX_T3_MULT:
        tier = "T3"
    elif bb_z < agg_trig * SOX_T2_MULT:
        tier = "T2"
    elif bb_z < agg_trig:
        tier = "T1"

    # State machine
    ps = prev_state or {"state": "CASH", "cooldown": 0, "entry_price": 0}
    cur_state = ps.get("state", "CASH")
    cooldown = ps.get("cooldown", 0)
    entry_price = ps.get("entry_price", 0)
    soxl_price = data[idx]["soxl"]
    smh_price = data[idx]["close"]

    action = None
    new_state = cur_state

    if cur_state in ("T1", "T2", "T3"):
        pnl = ((soxl_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        if SOX_PROFIT > 0 and pnl >= SOX_PROFIT:
            action = f"🎯 EXIT — PROFIT TARGET HIT ({pnl:.1f}% >= {SOX_PROFIT}%). Sell all SOXL."
            new_state = "COOL"
            cooldown = SOX_COOL
        elif bb_z > SOX_EXIT_Z:
            action = f"📊 EXIT — BB NORMALIZED (Z={bb_z:.2f} > {SOX_EXIT_Z}). Sell all SOXL. P&L: {pnl:.1f}%"
            new_state = "COOL"
            cooldown = SOX_COOL
        else:
            action = f"🔥 HOLD position. P&L: {pnl:.1f}% | Entry: ${entry_price:.2f}"
            if cur_state == "T1" and tier in ("T2", "T3"):
                action += f" | TIER UP to {tier} available (Z={bb_z:.2f})"
            elif cur_state == "T2" and tier == "T3":
                action += f" | TIER UP to T3 available (Z={bb_z:.2f})"
    elif cur_state == "COOL":
        cooldown -= 1
        if cooldown <= 0:
            new_state = "CASH"
            action = "✅ Cooldown complete. Back to CASH."
        else:
            action = f"⏳ Cooldown: {cooldown} trading days remaining."
    else:  # CASH
        if bb_z < agg_trig and mom_v > SOX_MOM_FLIP:
            deploy_tier = tier or "T1"
            pct = {"T1": SOX_T1_PCT, "T2": SOX_T2_PCT, "T3": SOX_T3_PCT}[deploy_tier]
            action = f"🎯 ENTRY SIGNAL — Deploy {pct}% into SOXL at ${soxl_price:.2f} ({deploy_tier})"
            new_state = deploy_tier
            entry_price = soxl_price
        elif bb_z < agg_trig:
            action = f"⚠️ BB oversold (Z={bb_z:.2f}) but momentum not confirmed ({mom_v:.1f}% <= {SOX_MOM_FLIP}%). Watch."
        else:
            action = "💤 No signal. Stay in cash."

    return {
        "state": new_state, "prev_state": cur_state, "action": action,
        "smh_price": round(smh_price, 2), "soxl_price": round(soxl_price, 2),
        "date": data[idx]["date"], "bb_z": round(bb_z, 3), "mom": round(mom_v, 2),
        "agg_trig": round(agg_trig, 2), "tier": tier,
        "cooldown": cooldown, "entry_price": round(entry_price, 2),
        "changed": new_state != cur_state,
    }, None

# ═══════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"smh": {"regime": None}, "soxl": {"state": "CASH", "cooldown": 0, "entry_price": 0}}

def save_state(smh_sig, soxl_sig):
    STATE_FILE.write_text(json.dumps({
        "smh": {
            "regime": smh_sig["regime"], "date": smh_sig["date"],
            "price": smh_sig["price"], "target": smh_sig["target"],
        },
        "soxl": {
            "state": soxl_sig["state"], "date": soxl_sig["date"],
            "cooldown": soxl_sig["cooldown"], "entry_price": soxl_sig["entry_price"],
        },
        "updated": datetime.now().isoformat(),
    }, indent=2))

# ═══════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════
def send_email(smh_sig, soxl_sig, smh_changed, soxl_changed, force_daily=False):
    sender = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    to_addr = os.environ.get("ALERT_EMAIL", sender)
    if not sender or not password:
        print("WARNING: Gmail credentials not set")
        return False

    parts = []
    if smh_changed:
        parts.append(f"SMH→{smh_sig['regime']}({smh_sig['target']}%)")
    if soxl_changed:
        parts.append(f"SOXL→{soxl_sig['state']}")

    if parts:
        subject = "🚨 " + " | ".join(parts)
    elif force_daily:
        subject = f"📊 Daily: SMH {smh_sig['regime']} | SOXL {soxl_sig['state']}"
    else:
        return False

    body = f"""Signal Monitor — {smh_sig['date']}
{'='*55}

━━━ SMH ASYMMETRIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Regime:     {smh_sig['regime']} → {smh_sig['target']}% allocation
  SMH Price:  ${smh_sig['price']}
  BB Z-Score: {smh_sig['bb_z']}  (Agg trigger: {smh_sig['agg_trig']}, Def trigger: {smh_sig['def_trig']})
  Momentum:   {smh_sig['mom']}%  (Flip: ±{SMH_MOM_FLIP}%)
  PPI Tilt:   {smh_sig['ppi_tilt']}
  ATR(14):    ${smh_sig['atr']}
"""
    if smh_sig["regime"] == "AGG":
        body += f"""  Stop Loss:  ${smh_sig['stop_level']}

  ▶ ACTION: INCREASE SMH to {smh_sig['target']}% allocation.
    Stop at ${smh_sig['stop_level']}.
"""
    elif smh_sig["regime"] == "DEF":
        body += f"""
  ▶ ACTION: TRIM SMH to {smh_sig['target']}% allocation.
"""
    else:
        body += f"""
  ▶ No action. Hold SMH at 100%.
"""

    body += f"""
━━━ SOXL SNIPER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  State:      {soxl_sig['prev_state']} → {soxl_sig['state']}
  SMH:        ${soxl_sig['smh_price']}  |  SOXL: ${soxl_sig['soxl_price']}
  BB Z-Score: {soxl_sig['bb_z']}  (Entry trigger: {soxl_sig['agg_trig']})
  Momentum:   {soxl_sig['mom']}%  (Flip: ±{SOX_MOM_FLIP}%)
  Cooldown:   {soxl_sig['cooldown']}d remaining

  ▶ {soxl_sig['action']}

━━━ LOCKED PARAMS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SMH:  BB(12,σ1.5) Trig:0.5 Mom(ROC25) Flip:3%
        Def:90% Agg:170% Stop:4×ATR PPI:1
  SOXL: BB(15,σ1.5) Trig:0.5 Mom(ROC15) Flip:2%
        Tier:cons(25/50/80) Prof:20% ExZ:0 Cool:5d
───────────────────────────────────────────────────────
Automated Signal Monitor · github.com/drnick69/SMHTrader
"""

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr
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

# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
def main():
    fred_key = os.environ.get("FRED_API_KEY", "")
    force = "--force" in sys.argv
    daily = "--daily" in sys.argv

    print(f"[{datetime.now().isoformat()}] Signal Monitor starting...")
    print(f"  Mode: {'FORCE' if force else 'DAILY' if daily else 'CHANGE-ONLY'}")

    # Fetch all data
    print("Fetching SMH...")
    smh = fetch_yahoo("SMH", days=120)
    if not smh: print("FATAL: no SMH data"); sys.exit(1)
    print(f"  SMH: {len(smh)} bars, latest {smh[-1]['date']} @ ${smh[-1]['close']:.2f}")

    print("Fetching SOXL...")
    soxl = fetch_yahoo("SOXL", days=120)
    if not soxl: print("FATAL: no SOXL data"); sys.exit(1)
    print(f"  SOXL: {len(soxl)} bars, latest {soxl[-1]['date']} @ ${soxl[-1]['close']:.2f}")

    print("Fetching QQQ...")
    qqq = fetch_yahoo("QQQ", days=120)
    if qqq: print(f"  QQQ: {len(qqq)} bars")

    ppi = None
    if fred_key:
        print("Fetching Semi PPI...")
        ppi = fetch_fred_ppi(fred_key)
        if ppi: print(f"  PPI: {len(ppi)} readings, latest {ppi[-1]['date']}")
    else:
        print("  No FRED key — PPI skipped")

    # Compute signals
    smh_sig, smh_err = compute_smh_signal(smh, qqq, ppi)
    if smh_err: print(f"SMH signal error: {smh_err}"); sys.exit(1)

    prev = load_state()
    soxl_sig, sox_err = compute_soxl_signal(smh, soxl, qqq, prev.get("soxl"))
    if sox_err: print(f"SOXL signal error: {sox_err}"); sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  SMH:  {smh_sig['regime']} → {smh_sig['target']}%  |  BBz:{smh_sig['bb_z']}  Mom:{smh_sig['mom']}%")
    print(f"  SOXL: {soxl_sig['prev_state']} → {soxl_sig['state']}  |  BBz:{soxl_sig['bb_z']}  Mom:{soxl_sig['mom']}%")
    print(f"  SOXL: {soxl_sig['action']}")
    print(f"{'='*50}")

    smh_changed = prev.get("smh", {}).get("regime") != smh_sig["regime"]
    soxl_changed = soxl_sig["changed"]

    if smh_changed: print(f"\n*** SMH REGIME CHANGE: {prev.get('smh',{}).get('regime','?')} → {smh_sig['regime']} ***")
    if soxl_changed: print(f"*** SOXL STATE CHANGE: {soxl_sig['prev_state']} → {soxl_sig['state']} ***")

    if smh_changed or soxl_changed or force:
        send_email(smh_sig, soxl_sig, smh_changed or force, soxl_changed or force, False)
    elif daily:
        send_email(smh_sig, soxl_sig, False, False, True)
    else:
        print("No changes. No email sent.")

    save_state(smh_sig, soxl_sig)
    print("State saved. Done.")

if __name__ == "__main__":
    main()
