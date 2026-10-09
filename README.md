# Employee Attendance & Analytics API

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set MONGO_URI and MONGO_DB; real environment variables win
python sample_seed.py   # optional nine-document sample
uvicorn app.main:app --port 8000
```

MongoDB 7. Indexes are created when the process starts. `GET /health` returns 200 only after MongoDB answers a ping, otherwise 503.

The contract in `openapi.yaml` is implemented in `app/main.py`, including punch rules R1–R5, the four analytics aggregations, and `GET /admin/explain/{endpoint}`.
