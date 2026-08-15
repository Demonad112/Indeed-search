PY := .venv/bin/python

.PHONY: help setup daily discover status test clean replay

help:
	@echo "make setup     — create the venv and install dependencies"
	@echo "make daily     — discover, score, then show what is waiting"
	@echo "make discover  — Phase 1 only"
	@echo "make score     — Phase 2 only"
	@echo "make calibrate — check scoring against the three reference postings"
	@echo "make status    — what is currently in the database"
	@echo "make test     — run the test suite"
	@echo "make replay   — re-ingest the committed 2026-08-14 Indeed harvest"
	@echo "make clean    — drop the database (harvest files are kept)"

setup:
	uv venv
	uv pip install httpx feedparser PyYAML python-dotenv anthropic pytest
	@test -f .env || (cp .env.example .env && echo "created .env — set JOBPIPE_CONTACT_EMAIL and ANTHROPIC_API_KEY")

# Phase 3 joins this line as it is built:  && $(PY) run.py draft
daily: discover score status
	@echo
	@echo "Phases 3-5 are not built yet, so nothing is drafted or queued."
	@echo "Scored postings above the threshold are waiting for draft.py."

discover:
	$(PY) run.py discover

score:
	$(PY) run.py score

calibrate:
	$(PY) run.py score --calibrate

status:
	@$(PY) run.py status

test:
	$(PY) -m pytest

replay:
	cp data/harvests/2026-08-14/*.json data/inbox/indeed/
	$(PY) run.py discover

clean:
	rm -f data/jobpipe.db data/jobpipe.db-wal data/jobpipe.db-shm
	@echo "database dropped; data/harvests/ untouched"
