# Telegram-бот подписки + Marzban

**[English version](README.md)**

Это бот на **python-telegram-bot**, который я написал под **[Marzban](https://github.com/Gozargah/Marzban)**: люди заходят по **инвайтам**, платят через **Crypto Pay** или **Telegram Stars**, бот создаёт/продлевает юзера в Marzban и отдаёт **ссылку подписки**. Плюс простая **рефералка** (баланс и слоты на инвайты).

Код выложил как есть — мало ли кому пригодится такой же стек. Тексты в боте в основном на русском.

## Что умеет

- Доступ **только по инвайту**: **root**-коды от админа и **«Пустить +1»** после своей оплаты.
- **Marzban по API**: завести/продлить пользователя, отдать URL подписки.
- **Оплата**: Crypto Pay (как настроишь), Stars, опционально списание с **реф. баланса**; если кассу не подключать, остаётся **тестовая выдача** (`PAYMENT_PROVIDER`).
- **Рефералка**: доля с оплаты приглашённого пригласившему (`REFERRAL_COMMISSION_PCT`), инвайты за месяцы тарифа (`INVITES_PER_SUB_MONTH`).
- **Админка**: сводка, рассылка, промокоды, VIP-инвайты, подчистка истории в чате.
- Кнопки **«Как работает рефералка»** и **поддержка** (после входа в список); логин поддержки в `.env`: `SUPPORT_TELEGRAM_USERNAME`.

`freekassa.py`, `freekassa_webhook.py` и старый unit в `deploy/` — **хвост** от другой схемы, в рабочем сценарии бота не участвуют, просто не выкидывал из репозитория.

## Что нужно

- **Python 3.9+** и зависимости из `requirements.txt`.
- Поднятый **Marzban** с доступным HTTP API и админскими кредами.
- Токен бота от **@BotFather** и твой числовой Telegram id в `ADMIN_TELEGRAM_ID`.

## Запуск у себя

```bash
cd vpnbot-github
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Минимум в `.env`: `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MARZBAN_*`.

```bash
python bot.py
```

## Команды, которыми сам пользуюсь

| Команда | Кто |
|--------|-----|
| `/start` | Все |
| `/admin_create_root_invite` [uses] [days] | Админ |
| `/admin_stats`, `/admin_wipe` | Админ |
| `/buy`, `/buy_test` | После инвайта |

Остальное — через кнопки и логику в `bot.py`.

## systemd и бэкапы

`deploy/vpnbot.service` — то, что я подкрутил под свой сервер: пути, пользователь, venv поправь под себя.  
Скрипт `backup-vpnbot.sh` и timer-файлы — по желанию, сначала прочитай скрипт и `vpnbot-backup.default`.

## По-бытовому про безопасность

`.env`, базы и прод-хосты в git не совать — в `.gitignore` уже отсекается очевидное. Если что-то утекло — меняй токен бота, пароль Marzban, токен Crypto Pay и т.д. Как ты это используешь и что у тебя по закону — твоя зона ответственности.

## Лицензия

MIT, текст в `LICENSE` (copyright **bioxhaust**). Свой форк — поменяй строку copyright под себя.
