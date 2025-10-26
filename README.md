# F1 Bot

This folder contains the F1 Telegram bot source. The bot connects to Telegram via polling and fetches live timing/schedule data.

Important files
- `Appnewapi.py` — main bot entrypoint
- `message_utils.py` — helper utilities for replies
- `requirements.txt` — Python dependencies
- `Token.txt` — (DO NOT COMMIT) contains your bot token, or set `TELEGRAM_TOKEN` env var instead

Files created to help deployment
- `.replit` — run configuration for Replit
- `Procfile` — worker declaration for Heroku (if you use Heroku)
- `.env.example` — example env file showing the expected variable

What to include in the GitHub repo
- Include: all `.py` source files, `requirements.txt`, `README.md`, any deployment files (`Dockerfile`, `Procfile`) and tests.
- Exclude: `Token.txt`, `.env`, virtualenv folders like `.venv/` or `venv/`, caches (`f1_cache/`, `temp_charts/`), logs. Your `.gitignore` already lists these.

Quick local setup (Windows PowerShell)
```powershell
cd "D:\rufet\Documents\F1 bot\F1bot"
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
# provide token for this session (or create Token.txt)
$env:TELEGRAM_TOKEN = Get-Content .\Token.txt
.venv\Scripts\python.exe Appnewapi.py
```

GitHub push (safe)
```powershell
# ensure Token.txt is ignored
git rm --cached Token.txt 2>$null || $null
git add .
git commit -m "Add bot source; Token.txt ignored"
# push to GitHub as usual (use gh CLI or web to create repo)
```

Deploy to Replit (recommended for quick hosting/testing)
1. Create a new Repl and import from GitHub (or create a new Repl and `git clone` your repo).
2. Open the Repl settings → Secrets (lock icon) and add `TELEGRAM_TOKEN` with your bot token.
3. Ensure `.replit` has `run = "python Appnewapi.py"` (this repo includes it).
4. Install requirements once using the Shell: `pip install -r requirements.txt`.
5. Start the Repl. For production/always-on, upgrade to Replit paid plan and enable "Always On" in the Repl settings.

Notes and best practices
- Keep the repo private if you don't want to expose your code or risk leaking the token.
- If the token is ever pushed to a public location, revoke it in BotFather immediately and create a new one.
- Prefer environment variables (TELEGRAM_TOKEN) over `Token.txt` on production hosts.

If you want, I can also add a `Dockerfile` and `docker-compose.yml`, or a `systemd` unit file for VPS deployment. Tell me which one you prefer."