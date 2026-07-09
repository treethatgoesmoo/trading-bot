# ── Trading Bot Makefile ─────────────────────────────────────
# `make` or `make help` lists all commands.

.PHONY: help install run start stop restart status backtest backtest-swing logs tail clean

.DEFAULT_GOAL := help

PID_FILE = .bot.pid

help: ## Show this help
	@echo "Trading bot commands:"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install dependencies (pip install -r requirements.txt)
	pip install -r requirements.txt

run: ## Run the bot in the foreground
	python main.py

start: ## Run the bot in the background (PID in .bot.pid)
	@mkdir -p logs
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot is already running (PID $$(cat $(PID_FILE)))"; \
	else \
		nohup python main.py > /dev/null 2>&1 & echo $$! > $(PID_FILE); \
		echo "Bot started (PID $$(cat $(PID_FILE))). Logs: logs/"; \
	fi

stop: ## Stop the background bot
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)); \
		rm -f $(PID_FILE); \
		echo "Bot stopped."; \
	else \
		echo "Bot is not running."; \
		rm -f $(PID_FILE); \
	fi

restart: stop ## Restart the background bot
	@sleep 2
	@$(MAKE) start

status: ## Show whether the bot is running
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot is running (PID $$(cat $(PID_FILE)))"; \
	else \
		echo "Bot is not running."; \
	fi

DATE ?= 2026-04-13
TICKER ?= SPY
VERBOSE ?=
backtest: ## Replay one 0DTE day (make backtest DATE=2026-04-13 TICKER=SPY [VERBOSE=1])
	python backtest.py $(DATE) $(TICKER) $(if $(VERBOSE),--verbose)

START ?= 2023-01-01
backtest-swing: ## Backtest swing strategy (make backtest-swing START=2024-01-01 [TICKERS=TSLA,AMD] [VERBOSE=1] [SWEEP=1])
	python backtest_swing.py --start $(START) $(if $(TICKERS),--tickers $(TICKERS)) $(if $(SWEEP),--sweep) $(if $(VERBOSE),--verbose)

logs: ## Show the last 100 lines of today's log
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -100 {} || echo "No log file found."

tail: ## Follow the latest log in real time
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -f {} || echo "No log file found."

clean: ## Remove __pycache__ dirs and the PID file
	rm -rf __pycache__ core/__pycache__ zero_dte/__pycache__ swing/__pycache__ bot/__pycache__ .bot.pid
	@echo "Cleaned."
