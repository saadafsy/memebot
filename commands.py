"""commands.py: the Telegram commands from Prompt C (/status /positions /pnl /closed plus inline
Start/Stop/Status buttons). They live in memebot/telegram_bot.py and start together with the bot:

    python commands.py [--data DIR]        # same as: python -m memebot run --no-panel

Needs ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID. Only that chat is answered.
"""
import sys

from memebot.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["run", "--no-panel", *sys.argv[1:]]))
