PYTHON ?= .venv/bin/python
SRC_PATH ?= src

.PHONY: dashboard clean-job

dashboard:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$(SRC_PATH) $(PYTHON) -m style_kb.dashboard

clean-job:
ifndef JOB_ID
	$(error JOB_ID is required, usage: make clean-job JOB_ID=1_zCcipRRik)
endif
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$(SRC_PATH) $(PYTHON) -m style_kb.maintenance.clean_job "$(JOB_ID)"
