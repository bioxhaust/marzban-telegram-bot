# Telegram VPN subscription bot + Marzban

Telegram-бот на **python-telegram-bot**: инвайты, оплата (**Crypto Pay**, **Telegram Stars**), реферальная программа, выдача доступа через **[Marzban](https://github.com/Gozargah/Marzban)** API.

Собрано как учебный/портфолио-проект. Перед публикацией форка замените данные в `LICENSE` и убедитесь, что в коде нет ваших прод-секретов.

## Возможности

- Вход в «список» только по **инвайт-коду** (root-коды от админа и пользовательские «Пустить +1»).
- **Marzban**: создание/продление пользователя, ссылка подписки в ответе.
- **Оплата**: Crypto Pay (XTR/фиат по настройке), Stars, списание с **реф. баланса**; без настроенной кассы — тестовая выдача (см. `PAYMENT_PROVIDER`).
- **Рефералка**: процент с оплаты приглашённого на баланс пригласившего (`REFERRAL_COMMISSION_PCT`), инвайт-слоты за месяцы подписки (`INVITES_PER_SUB_MONTH`).
- Админ: сводка, рассылка, промокоды, VIP-инвайты, мягкое удаление истории чата.
- Кнопки **«Как работает рефералка»** и **«Поддержка»** (после входа в список; поддержка — `SUPPORT_TELEGRAM_USERNAME`).

В репозитории остаются **legacy**-файлы **FreeKassa** (`freekassa.py`, `freekassa_webhook.py`, unit в `deploy/`) для справки; основной сценарий бота на них не опирается.

## Требования

- Python **3.9+** (проверено с зависимостями из `requirements.txt`).
- Развёрнутая панель **Marzban** с доступным HTTP API и учёткой админа.
- Токен бота от **@BotFather**, ваш числовой **Telegram user id** для `ADMIN_TELEGRAM_ID`.

## Быстрый старт

```bash
cd vpnbot-github
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env` (минимум: `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `MARZBAN_*`).

```bash
python bot.py
```

## Полезные команды

| Команда | Кто |
|--------|-----|
| `/start` | Все |
| `/admin_create_root_invite` [uses] [days] | Админ |
| `/admin_stats`, `/admin_wipe` | Админ |
| `/buy`, `/buy_test` | После инвайта |

Подробнее о flow см. комментарии в `bot.py` и сообщения в UI.

## systemd и бэкапы

В `deploy/`:

- `vpnbot.service` — пример юнита для бота (поправьте `WorkingDirectory`, `User`, путь к venv).
- `backup-vpnbot.sh`, `vpnbot-backup.service`, `vpnbot-backup.timer` — опциональный бэкап SQLite и артефактов (читайте скрипт и `vpnbot-backup.default`).

## Безопасность

- **Не коммитьте** `.env`, базы `*.db`, экспорты чатов и прод-IP в публичный репозиторий.
- После любых утечек — **ротация** `BOT_TOKEN`, паролей Marzban, Crypto Pay API, ключей панели.
- Убедитесь, что политика вашего сервиса и юрисдикции допускает способ оказания услуг, который вы автоматизируете.

## Публикация на GitHub

```bash
cd vpnbot-github
git init
git add .
git commit -m "Initial commit: Telegram Marzban subscription bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

Создай пустой репозиторий на GitHub заранее. Не пушь `.env` и `*.db` — они в `.gitignore`.

## Лицензия

MIT — см. `LICENSE`. Укажите своё имя в строке copyright.
