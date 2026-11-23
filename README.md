# MRVora Mining

A Telegram mining game bot built with Python. It includes mining, balance
tracking, daily rewards, referrals, a top-ten leaderboard, and five in-game VIP
levels. Balances are game points only; the bot does not handle real-money
payments, cryptocurrency, deposits, or withdrawals.

## Start the bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and add its token to
   Replit Secrets as `TELEGRAM_BOT_TOKEN`.
2. Install the dependency with `pip install -r requirements.txt`.
3. Start it with `python -m mrvora_mining`.
4. Open your bot in Telegram and send `/start`.

In Replit, the `MRVora Mining Bot` workflow runs the bot process.

## Game rules

- Mine: 10 coins, once every 60 seconds.
- Daily reward: 100 coins, once per UTC calendar day.
- Referral: 50 coins to the inviter when a new user starts through their link.
- Leaderboard: the ten users with the highest balances.
- VIP upgrades are purchased sequentially with in-game coins. The upgrade costs
  are 500, 1,500, 4,000, 10,000, and 25,000 coins; VIP mining rewards are 11–15
  coins and daily rewards are 110, 125, 150, 175, and 200 coins.

User balances, claim timestamps, VIP levels, and VIP purchase history are stored
in `mrvora_mining.sqlite3`. Set `MRVORA_DB_PATH` to use a different SQLite
database location. Reward values, VIP tiers, and the mining cooldown are defined
in `mrvora_mining/database.py`.