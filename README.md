# Albayda Court Telegram Bot

Public deployment mirror for the Albayda Court Telegram bot.

## Configure GitHub Actions

In the repository, open **Settings → Secrets and variables → Actions** and add:

- `TELEGRAM_BOT_TOKEN`: the Telegram bot token from BotFather.
- `GOOGLE_DRIVE_FILE_ID`: the ID of the court records spreadsheet export. The file must be shared so the bot can download it.

Never commit either value to the repository. The records file ID is deliberately read from the environment and is not included in this public source.

The workflow runs the test suite before starting the bot. Scheduled runs start every five hours and are limited to just under six hours by GitHub Actions. If GitHub blocks Actions at the account level because of a billing restriction, making this repository public will not remove that restriction.

## Local development

Install the requirements with `python -m pip install -r requirements.txt`, copy `.env.example` to `.env`, set both environment values, then run `python bot.py`.
