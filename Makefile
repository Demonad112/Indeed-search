PY := .venv/bin/python

.PHONY: help setup daily discover status test clean replay

help:
	@echo "make setup    — create the venv and install dependencies"
	@echo "make daily    — discover, then show what is waiting in the queue"
	@echo "make discover — Phase 1 only"
	@echo "make status   — what is currently in the database"
	@echo "make test     — run the test suite"
	@echo "make replay   — re-ingest the committed 2026-08-14 Indeed harvest"
	@echo "make clean    — drop the database (harvest files are kept)"

setup:
	uv venv
	uv pip install httpx feedparser PyYAML python-dotenv pytest
	@test -f .env || (cp .env.example .env && echo "created .env — set JOBPIPE_CONTACT_EMAIL")

# Phases 2 and 3 join this line as they are built:
#   $(PY) run.py score && $(PY) run.py draft
daily: discover status
	@echo
	@echo "Phases 2-5 are not built yet, so nothing is scored, drafted or queued."
	@echo "Postings above are sitting at status='new'."

discover:
	$(PY) run.py discover

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
