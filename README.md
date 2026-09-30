# MAXO DPT Bot — Railway

Professional Telegram bot wrapper around the official DPT Shell CLI.

## Railway Variables

Set only:

- `BOT_TOKEN` = Telegram bot token
- `ADMIN_ID` = your numeric Telegram ID

Optional:

- `DPT_VERSION` = `2.19.0`
- `MAX_DPT_SECONDS` = `7200`

The Dockerfile downloads the official DPT Shell release automatically during the image build.

## Limits

The standard Telegram cloud Bot API currently allows bots to download files with `getFile` up to 20 MB, while `sendDocument` allows sending files up to 50 MB. This bot therefore rejects input APKs above 20 MB and output APKs above 50 MB.

## Storage

Each job is written to its own temporary directory and is deleted in a `finally` block after completion or failure. The SQLite database stores only user IDs/timestamps, not uploaded APK bytes. Railway's normal service filesystem is ephemeral; no uploaded job files are intended to persist.

## Commands

- `/start`
- `/dpt` — reply to an APK
- `/admin` — admin panel

Admin panel:

- service on/off
- user list
- broadcast
- return to main menu

## DPT command

The worker executes the documented basic CLI form:

`java -jar dpt.jar -f INPUT.apk -o OUTPUT_DIR`
