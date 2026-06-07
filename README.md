# FinAlly — AI Trading Workstation

FinAlly (Finance Ally) is a visually rich, AI-powered trading workstation. It streams
live market data, lets you trade a simulated $10,000 portfolio, and embeds an LLM chat
assistant that can analyze positions and execute trades on your behalf — a Bloomberg-style
terminal with an AI copilot.

This is the capstone project for an agentic AI coding course, built entirely by coding
agents that coordinate through files in `planning/`.

## Status

Early stage. The full specification lives in [`planning/PLAN.md`](planning/PLAN.md); the
`backend/`, `frontend/`, and `test/` implementations are not built yet.

## Features (planned)

- Live watchlist of 10 default tickers with price-flash animations and sparklines
- Simulated trading — market orders, instant fill, fractional shares
- Portfolio heatmap, P&L chart, and positions table
- AI chat assistant that analyzes the portfolio and auto-executes trades and watchlist changes
- Single Docker container, single port, no login

## Architecture

- **Frontend**: Next.js + TypeScript, built as a static export
- **Backend**: FastAPI (Python, managed with `uv`), serves the API and the static frontend
- **Database**: SQLite, lazily initialized and volume-mounted at `db/finally.db`
- **Real-time data**: Server-Sent Events (SSE) pushing price snapshots at ~500ms
- **Market data**: built-in simulator by default; real data via the Massive API if a key is set
- **AI**: LiteLLM → OpenRouter free model, with automatic fallback to Groq

Everything runs in one container on port 8000. See `planning/PLAN.md` for the full design.

## Running (once implemented)

```bash
docker run -v "$(pwd)/db:/app/db" -p 8000:8000 --env-file .env finally
```

Then open http://localhost:8000.

## License

See [LICENSE](LICENSE).
