"""app.py: the local Start/Stop app from Prompt B. Same as `python -m memebot run`.

    python app.py [--data DIR] [--no-telegram]

Opens the panel at http://127.0.0.1:8787 (Start/Stop, Pause everything, status, recent trades,
read-only settings). It is built with FastAPI instead of Flask so that the paper engine, the panel and
the Telegram bot run in one program and never fight over the same files.
"""
import sys

from memebot.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["run", *sys.argv[1:]]))
