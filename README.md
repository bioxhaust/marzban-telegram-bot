# Telegram VPN subscription bot + Marzban

**[Русская версия](README.ru.md)**

A Telegram bot built with **python-telegram-bot**: invite-only access, payments (**Crypto Pay**, **Telegram Stars**), a simple referral program, and subscription provisioning via the **[Marzban](https://github.com/Gozargah/Marzban)** API.

Intended as a learning / portfolio project. Before publishing a fork, update `LICENSE` and double-check that no production secrets are committed.

## Features

- **Invite-only** “allowlist” (admin **root** invites and per-user **+1** invites after purchase).
- **Marzban**: create/extend user, return subscription URL.
- **Payments**: Crypto Pay (XTR/fiat depending on config), Stars, pay from **referral balance**; if no payment backend is configured, a **test grant** path exists (see `PAYMENT_PROVIDER`).
- **Referrals**: commission from invitee payments to inviter balance (`REFERRAL_COMMISSION_PCT`), invite slots per paid month (`INVITES_PER_SUB_MONTH`).
- **Admin**: stats dashboard, broadcast, promo codes, VIP invites, optional chat cleanup.
- UI buttons: **referral explainer** and **support** (after allowlist entry; support username via `SUPPORT_TELEGRAM_USERNAME`).

**Legacy FreeKassa** files (`freekassa.py`, `freekassa_webhook.py`, related systemd unit under `deploy/`) are kept for reference; the main bot flow does not depend on them.

## Requirements

- Python **3.9+** (with dependencies from `requirements.txt`).
- A running **Marzban** panel with HTTP API access and admin credentials.
- Bot token from **@BotFather**, numeric Telegram user id for `ADMIN_TELEGRAM_ID`.

## Quick start

```bash
cd vpnbot-github
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` (minimum: `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MARZBAN_*`).

```bash
python bot.py
```

## Useful commands

| Command | Who |
|--------|-----|
| `/start` | Everyone |
| `/admin_create_root_invite` [uses] [days] | Admin |
| `/admin_stats`, `/admin_wipe` | Admin |
| `/buy`, `/buy_test` | After invite |

For flow details, see `bot.py` and in-bot messages.

## systemd & backups

Under `deploy/`:

- `vpnbot.service` — example unit (adjust `WorkingDirectory`, `User`, venv path).
- `backup-vpnbot.sh`, `vpnbot-backup.service`, `vpnbot-backup.timer` — optional SQLite/artifact backup (read the script and `vpnbot-backup.default`).

## Security

- **Do not commit** `.env`, `*.db` dumps, chat exports, or production IPs.
- Rotate `BOT_TOKEN`, Marzban passwords, Crypto Pay tokens, and panel keys after any leak.
- Ensure your use case complies with applicable law and platform policies.

## Publishing to GitHub

Create an **empty** repository on GitHub first.

If this folder **already has** `.git` and a first commit:

```bash
cd vpnbot-github
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

If you are starting **from scratch**:

```bash
cd vpnbot-github
git init
git add .
git commit -m "Initial commit: Telegram Marzban subscription bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

`.env` and `*.db` are listed in `.gitignore`.

## License

MIT — see `LICENSE`. Put your name in the copyright line.
