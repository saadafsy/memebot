# Claude Code Prompt (Verbatim from Jawad)

> Add a LIVE executor (live_trader.py) using Jupiter Swap V2: get order, sign, execute. Key and API key from environment variables only. Check a sell route before every buy. Slippage and fees from settings. Log signatures and actual fills. Test on tiny amounts first.

## Files Included in `/workspace/handoff/`
- `README.md` — full architecture & operating rules (copied from `/workspace/sim-bot/README.md`)
- `settings.json` — current verified paper settings (copied from `/workspace/sim-bot/settings.json`)
- `INTERFACE_CONTRACT.md` — exact `open_position(...)` / `close_position(...)` plug-in contract
- `TRADES_SCHEMA.md` — exact 23-column `trades.csv` schema + supporting CSV/JSON schemas
- `CLAUDE_CODE_PROMPT.md` — this prompt
- `MANIFEST_SHA256.txt` — raw `sha256sum` verification of all handoff and baseline files
