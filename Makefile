PY := .venv/bin/python

.PHONY: help setup daily discover score draft dashboard calibrate status test clean replay

help:
	@echo "make setup     — create the venv and install dependencies"
	@echo "make daily     — discover, score, then show what is waiting"
	@echo "make discover  — Phase 1 only"
	@echo "make score     — Phase 2 only"
	@echo "make draft     — Phase 3 only"
	@echo "make dashboard — Phase 4: the private review dashboard"
	@echo "make calibrate — check scoring against the three reference postings"
	@echo "make status    — what is currently in the database"
	@echo "make test     — run the test suite"
	@echo "make replay   — re-ingest the committed 2026-08-14 Indeed harvest"
	@echo "make clean    — drop the database (harvest files are kept)"

setup:
	uv venv
	uv pip install httpx feedparser PyYAML python-dotenv anthropic fastapi 'uvicorn[standard]' pytest
	@test -f .env || (cp .env.example .env && echo "created .env — set JOBPIPE_CONTACT_EMAIL, ANTHROPIC_API_KEY, JOBPIPE_PASSPHRASE")

daily: discover score draft status
	@echo
	@echo "Review them:   make dashboard"

discover:
	$(PY) run.py discover

score:
	$(PY) run.py score

draft:
	$(PY) run.py draft

dashboard:
	$(PY) run.py serve

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
