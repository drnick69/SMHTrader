# SMH Asymmetric Signal Monitor

Automated daily signal checker for the SMH asymmetric strategy.  
Runs via GitHub Actions (free), emails you when the regime changes.

**Locked Parameters:**  
BB(12,σ1.5) Trig:0.5 Mom(ROC25) Flip:3% Def:90% Base:100% Agg:170% Stop:4×ATR PPI:1

## How It Works

1. Runs M-F at 6pm ET (after market close)
2. Fetches SMH + QQQ prices from Yahoo Finance (free, no key)
3. Fetches Semi PPI from FRED (free key)
4. Computes BB Z-score, momentum, PPI tilt → regime signal
5. If regime changed from last check → emails you with action required
6. Saves state to `state.json` (committed to repo for persistence)

## Setup (10 minutes)

### 1. Create GitHub repo
- Create a new **private** repo on GitHub
- Push this folder to it:
  ```bash
  cd smh-signal-monitor
  git init
  git add .
  git commit -m "init"
  git remote add origin git@github.com:YOUR_USER/smh-signal.git
  git push -u origin main
  ```

### 2. Get a FRED API key (free)
- Go to https://fred.stlouisfed.org/docs/api/api_key.html
- Sign up → get key (instant)

### 3. Set up Gmail app password (free)
- Go to https://myaccount.google.com/apppasswords
- (Requires 2FA enabled on your Google account)
- Generate an app password for "Mail"
- Save the 16-character password

### 4. Add secrets to GitHub repo
Go to your repo → Settings → Secrets and variables → Actions → New repository secret

| Secret Name          | Value                              |
|----------------------|------------------------------------|
| `GMAIL_ADDRESS`      | your.email@gmail.com               |
| `GMAIL_APP_PASSWORD` | abcd efgh ijkl mnop (app password) |
| `ALERT_EMAIL`        | where to send alerts (can be same) |
| `FRED_API_KEY`       | your FRED API key                  |

**Optional SMS (Twilio free trial):**

| Secret Name     | Value                    |
|-----------------|--------------------------|
| `TWILIO_SID`    | Your Account SID         |
| `TWILIO_TOKEN`  | Your Auth Token          |
| `TWILIO_FROM`   | Your Twilio phone number |
| `TWILIO_TO`     | Your cell phone number   |

### 5. Enable Actions
- Go to repo → Actions tab → Enable workflows
- The workflow will now run automatically M-F at 6pm ET
- You can also click "Run workflow" manually to test

## Testing

Run locally to verify:
```bash
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="your-app-password"
export FRED_API_KEY="your-fred-key"
python signal_monitor.py --force
```

`--force` sends an email even if regime hasn't changed.  
`--daily` sends a status email every run (not just on changes).

## What the emails look like

**Subject:**  
🔥 SMH Signal: HOLD → AGG (170%)

**Body:**  
Regime change, target allocation, price, all indicator values,  
specific action to take (buy/sell/hold), and stop-loss level if in AGG.

## Cost

$0. GitHub Actions free tier = 2000 min/month. This uses ~1 min/day = 30 min/month.
Yahoo Finance API = free. FRED API = free. Gmail SMTP = free.
