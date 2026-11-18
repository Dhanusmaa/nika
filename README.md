# Loki (TVA)

Telegram bot that monitors a configured source chat for POT / GUESS messages and forwards matching notifications to a destination chat.

## Deploy to Koyeb

[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&builder=buildpack&repository=github.com/Dhanusmaa/mazepot&branch=main&name=loki-tva&service_type=web&ports=8000%3Bhttp%3B%2F&run_command=python%20bot.py&env%5BPORT%5D=8000&env%5BAPI_ID%5D=&env%5BAPI_HASH%5D=&env%5BBOT_TOKEN%5D=&env%5BTELETHON_SESSION%5D=&env%5BSOURCE_CHAT_ID%5D=-1003707950453&env%5BGUESS_USER_ID%5D=8790267038&env%5BSOURCE_USERNAME%5D=maze&env%5BOWNER_ID%5D=5250696460)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Dhanusmaa/mazepot)

> **Private repository:** Koyeb must have access to this GitHub repository before deployment.
>
> **MongoDB:** Set `MONGODB_URI` in Koyeb. Without it, approved users and the last Pot answer cannot persist across restarts.

### Render deployment

Use the button above for a one-click Render deployment. Render reads the repository's `render.yaml` Blueprint to configure the Python web service. urlRender Deploy Button documentationturn0search0

**Environment variables required on Render:**

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `TELETHON_SESSION`
- `MONGODB_URI`
- `SOURCE_CHAT_ID` — already defaults to `-1003707950453`
- `SOURCE_USERNAME` — already defaults to `mazes`
- `OWNER_ID` — already defaults to `5250696460`

The four Telegram/database secrets must be entered in Render after clicking the deploy button. Never commit their values to GitHub.

**Important:** Render's free Web Services can spin down after inactivity, so Render Free is **not suitable for reliable 24/7 Telegram monitoring**. For continuous operation, keep using Koyeb or use a Render service/plan that stays running. citeturn0search2

### Required environment variables

Set these in Koyeb before starting the service:

| Variable | Required | Description |
|---|---|---|
| `API_ID` | Yes | Telegram API ID |
| `API_HASH` | Yes | Telegram API hash |
| `BOT_TOKEN` | Yes | Telegram bot token |
| `TELETHON_SESSION` | Yes | Telethon StringSession for the user account |
| `SOURCE_CHAT_ID` | No | Source chat ID |
| `GUESS_USER_ID` | No | Telegram user ID whose messages are monitored |
| `SOURCE_USERNAME` | No | Public username used to create message links |
| `OWNER_ID` | No | Telegram user ID allowed to manage approved recipients; defaults to `5250696460` |
| `MONGODB_URI` | Yes for persistence | MongoDB connection URI used for approved users and last Pot answer |

The Deploy button pre-fills the non-secret defaults and configures port `8000` for the web service. Telegram credentials are left blank for you to enter securely in Koyeb.

## What it does

1. Logs in to the Telegram user account using `TELETHON_SESSION`.
2. Watches the configured source chat.
3. Detects POT, CLOSEST GUESS, and RAIN messages.
4. For POT and CLOSEST GUESS, sends the owner a pending message with a suggested number and a SEND button.
5. Only after SEND is pressed, replies to the exact game message in the source chat with that number.
6. Saves the last Pot and Guess answers in MongoDB so each game can use its own previous answer.
7. For RAIN, sends the owner a JOIN RAIN confirmation button; confirming it makes the monitored Telegram user account click the original Grab button automatically.
8. Runs continuously while the Koyeb instance is running.

## Local setup

```bash
python -m pip install -r requirements.txt
python bot.py
```

Set the required environment variables before running.

## Approved DM recipients

The owner can manage notification recipients from the bot:

- `/approve <user_id>` — approve a user for POT DMs
- Reply to a user's message with `/approve` — approve that user
- `/unapprove <user_id>` — stop sending that user POT DMs
- `/approved` — list approved users

Approved users must have started a private chat with the bot before the bot can DM them.

**Note:** the current approved-user list is kept in memory and resets when the Koyeb service restarts.

## Koyeb settings

The repository uses a lightweight **Web Service** so it can run on Koyeb's Free Instance. The HTTP server is only a health endpoint; Telegram monitoring still runs in the same process.

- **Source:** GitHub
- **Branch:** `main`
- **Builder:** Dockerfile
- **Service type:** Web
- **Port:** `8000`
- **Route:** `/`
- **Health endpoint:** `/health`
- **Run command:** `python bot.py`

Koyeb supports GitHub-based Web Services with exposed HTTP ports and routes.