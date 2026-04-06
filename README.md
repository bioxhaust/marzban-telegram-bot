# Telegram subscription bot + Marzban

**[Русская версия](README.ru.md)**

I wrote this bot for **python-telegram-bot** to sit in front of **[Marzban](https://github.com/Gozargah/Marzban)**: people join on **invites**, pay with **Crypto Pay** or **Telegram Stars**, and the bot creates/renews the user in Marzban and sends back the **subscription link**. There’s also a small **referral** layer (balance + invite slots).

I’m sharing the code as-is in case the same stack is useful to someone else. UI strings in the bot are mostly Russian.

## What it does

- **Invite-only** access: admin **root** codes and per-user **+1** codes after someone has bought in.
- **Marzban API**: upsert user, extend access, surface subscription URL.
- **Payments**: Crypto Pay (configurable), Stars, optional pay-from-**referral balance**; if you don’t wire a provider, there’s still a **test grant** path (`PAYMENT_PROVIDER`).
- **Referrals**: cut of invitee checkout to inviter (`REFERRAL_COMMISSION_PCT`), invite budget per paid month (`INVITES_PER_SUB_MONTH`).
- **Admin**: rough stats, broadcast, promo codes, VIP invites, optional chat cleanup.
- In-bot buttons for **how referrals work** and **support** (after you’re on the list); support username from `SUPPORT_TELEGRAM_USERNAME`.

## What you need

- **Python 3.9+** and `requirements.txt`.
- A **Marzban** instance you can hit over HTTP with admin API creds.
- A bot token from **@BotFather** and your numeric Telegram id for `ADMIN_TELEGRAM_ID`.

## Run it locally

```bash
cd vpnbot-github
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` at least: `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MARZBAN_*`.

```bash
python bot.py
```

## Commands I actually use

| Command | Who |
|--------|-----|
| `/start` | Anyone |
| `/admin_create_root_invite` [uses] [days] | Admin |
| `/admin_stats`, `/admin_wipe` | Admin |
| `/buy`, `/buy_test` | After invite |

The rest of the flow is in the inline keyboard and `bot.py`.

## systemd / backups

`deploy/vpnbot.service` is the unit I adapted for my server—fix paths, user, venv.  
`backup-vpnbot.sh` + timer files are optional; read the script and `vpnbot-backup.default` before enabling.

## Ops note

Keep `.env`, SQLite files, and anything identifying prod hosts **out of git**—`.gitignore` already drops the obvious stuff. If something leaks, rotate bot token, Marzban password, Crypto Pay app token, etc. You’re responsible for how you run this and what laws apply where you deploy.

## License

MIT — see `LICENSE` (copyright **bioxhaust**). Forks: update the copyright line if you ship your own fork as yours.
