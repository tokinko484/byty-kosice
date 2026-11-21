# Byty Košice

Súkromný prehľad prenájmov 1,5–2 izbových bytov v Košiciach do 680 €.

- `scraper/scrape.py` – zber z Bazoša, Nehnuteľností a TopReality, Telegram upozornenia
- `docs/` – web (GitHub Pages)
- `.github/workflows/scrape.yml` – beh každých 15 min

Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (viac ID oddelených čiarkou), voliteľne `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `MAIL_TO`.
