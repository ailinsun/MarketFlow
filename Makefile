PYTHON ?= python3

.PHONY: verify check test demo history clean help

help:
	@echo "make verify   every gate and self-test, offline, from a clean checkout"
	@echo "make demo     run the end-to-end demonstration on synthetic data"
	@echo "make check    publication gate over the working tree"
	@echo "make history  publication gate over every reachable commit and blob"
	@echo "make test     offline test runner (network denied by an audit hook)"

# The single command CI runs and the one to run before trusting anything here.
verify: check test

check:
	$(PYTHON) -B checks/check_public.py

history:
	$(PYTHON) -B checks/check_public.py --history

test:
	$(PYTHON) -B checks/run_tests.py

demo:
	$(PYTHON) -B examples/portfolio_risk_demo.py

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info
