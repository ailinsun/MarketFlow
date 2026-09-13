PYTHON ?= python3
.PHONY: test check

test:
	$(PYTHON) -B checks/run_tests.py

check:
	$(PYTHON) -B checks/check_public.py
