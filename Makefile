# SPY setup signals for manual SPX options execution.
.PHONY: install install-dev run start stop restart status logs tail test clean

PYTHON ?= python
PID_FILE = .bot.pid

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

run:
	$(PYTHON) main.py

start:
	@mkdir -p logs
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot is already running (PID $$(cat $(PID_FILE)))"; \
	else \
		nohup $(PYTHON) main.py > /dev/null 2>&1 & echo $$! > $(PID_FILE); \
		echo "Bot started (PID $$(cat $(PID_FILE))). Logs: logs/"; \
	fi

stop:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		kill $$(cat $(PID_FILE)); \
		rm -f $(PID_FILE); \
		echo "Bot stopped."; \
	else \
		echo "Bot is not running."; \
		rm -f $(PID_FILE); \
	fi

restart: stop
	@sleep 2
	@$(MAKE) start

# Process existence only; use Discord status and logs for data/delivery health.
status:
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "Bot process is running (PID $$(cat $(PID_FILE))); data health is not checked."; \
	else \
		echo "Bot is not running."; \
	fi

logs:
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -100 {} || echo "No log file found."

tail:
	@ls -t logs/bot*.log 2>/dev/null | head -1 | xargs -I{} tail -f {} || echo "No log file found."

test:
	$(PYTHON) -m pytest -q tests

# Do not remove .bot.pid: losing it can orphan a running collector.
clean:
	@find bot core spy_setup tests -type d -name __pycache__ -prune -exec rm -rf {} +
	@rm -rf __pycache__
	@echo "Removed Python caches; process state and evidence untouched."
