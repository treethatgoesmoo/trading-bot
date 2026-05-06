# ── Trading Bot Makefile ─────────────────────────────────────

.PHONY: install run start stop restart status backtest logs clean

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
		nohup python main.py >> logs/bot.log 2>&1 & echo $$! > $(PID_FILE); \
		echo "Bot started (PID $$(cat $(PID_FILE))). Logs: logs/bot.log"; \
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

# Show today's log
logs:
	@if [ -f logs/bot.log ]; then tail -100 logs/bot.log; else echo "No log file found."; fi

# Follow logs in real time
tail:
	@tail -f logs/bot.log

# Remove cached files
clean:
	rm -rf __pycache__ .bot.pid
	@echo "Cleaned."
