.PHONY: help run run-auto research implementation summary agents test

help:
	@echo "make run            - interactive full cycle"
	@echo "make run-auto       - automatic full cycle"
	@echo "make research       - research only"
	@echo "make implementation - implementation only"
	@echo "make summary        - latest log summary"
	@echo "make agents         - list agents"
	@echo "make test           - run tests"

run:
	python start.py --mode interactive

run-auto:
	python start.py --mode auto

research:
	python start.py --phase research

implementation:
	python start.py --phase implementation

summary:
	python monitor_logs.py summary

agents:
	python manage_agents.py list

test:
	pytest -q
