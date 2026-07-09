# ── Trading Bot Makefile ─────────────────────────────────────

.PHONY: install run start stop restart status backtest backtest-swing logs tail clean

PID_FILE = .bot.pid

# Install dependencies
install:
	pip install -r requirements.txt

# Run the bot (foreground)
run:
	python main.py

# Run the bot in the background
start:
	@mkdir -p logs
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot is already running (PID $$(cat $(PID_FILE)))"; \
	else \
		nohup python main.py > /dev/null 2>&1 & echo $$! > $(PID_FILE); \
		echo "Bot started (PID $$(cat $(PID_FILE))). Logs: logs/"; \
	fi

# Stop the bot
stop:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)); \
		rm -f $(PID_FILE); \
		echo "Bot stopped."; \
	else \
		echo "Bot is not running."; \
		rm -f $(PID_FILE); \
	fi

# Restart the bot
restart: stop
	@sleep 2
	@$(MAKE) start

# Show bot status
status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot is running (PID $$(cat $(PID_FILE)))"; \
	else \
		echo "Bot is not running."; \
	fi

# Run backtest (usage: make backtest DATE=2026-04-13 TICKER=SPY)
DATE ?= 2026-04-13
TICKER ?= SPY
VERBOSE ?=
backtest:
	python backtest.py $(DATE) $(TICKER) $(if $(VERBOSE),--verbose)

# Backtest the swing options strategy
# Usage: make backtest-swing START=2024-01-01 [TICKERS=TSLA,AMD] [VERBOSE=1] [SWEEP=1]
START ?= 2023-01-01
backtest-swing:
	python backtest_swing.py --start $(START) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(SWEEP),--sweep) $(if $(VERBOSE),--verbose)

# Show today's log
logs:
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -100 {} || echo "No log file found."

# Follow logs in real time
tail:
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -f {} || echo "No log file found."

# Remove cached files
clean:
	rm -rf __pycache__ core/__pycache__ zero_dte/__pycache__ swing/__pycache__ bot/__pycache__ .bot.pid
	@echo "Cleaned."
