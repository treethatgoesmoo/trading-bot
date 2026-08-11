# ── Trading Bot Makefile ─────────────────────────────────────

.PHONY: install run start stop restart status logs clean

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

# (make backtest removed 2026-08-12 — backtest.py graded with lookahead
# bias; use backfill.py --bias-report / backtest_filters.py instead)
VERBOSE ?=

# Backtest the swing options strategy (usage: make backtest-swing START=2023-01-01)
START ?= 2023-01-01
backtest-swing:
	python backtest_swing.py --start $(START) $(if $(VERBOSE),--verbose)

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
