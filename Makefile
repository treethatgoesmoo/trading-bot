# ── Trading Bot Makefile ─────────────────────────────────────

.PHONY: install run backtest stop status logs clean

# Install dependencies
install:
	pip install -r requirements.txt

# Run the bot (foreground)
run:
	python main.py

# Run the bot in the background (Windows-compatible)
start:
	@echo Starting bot in background...
	start /B python main.py > logs/bot.log 2>&1
	@echo Bot started. Logs: logs/bot.log

# Run backtest (usage: make backtest DATE=2026-04-13 TICKER=SPY)
DATE ?= 2026-04-13
TICKER ?= SPY
VERBOSE ?=
backtest:
	python backtest.py $(DATE) $(TICKER) $(if $(VERBOSE),--verbose)

# Show recent logs
logs:
	@if exist logs\bot.log (type logs\bot.log | more) else (echo No log file found.)

# Kill running bot processes
stop:
	@echo Stopping bot...
	@taskkill /F /IM python.exe /FI "WINDOWTITLE eq *main.py*" 2>nul || echo No bot process found.

# Remove cached files
clean:
	@if exist __pycache__ rmdir /S /Q __pycache__
	@if exist logs\bot.log del logs\bot.log
	@echo Cleaned.
