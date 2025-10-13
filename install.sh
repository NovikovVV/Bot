#!/bin/bash
set -e
echo "Creating venv..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "Done. To run: source venv/bin/activate && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
