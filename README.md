# Market Pulse

A Python (Flask) web app that shows live stock prices for ten companies, refreshed every 3 seconds, with a real `/health` check and a **chaos lab** that breaks the app on purpose so you can practise finding and fixing the failures DevOps engineers meet most often.

Prices are **simulated** by default (random walk, clearly labelled in the UI). SpaceX is private and has no public price, so it appears as a "private company" row; Amazon is the tenth company.

```
Browser ──► Nginx :80 ──► Gunicorn/Flask :5000 ──► price provider (simulated | Finnhub)
                                  │
GitHub push ─► Actions: test ─► build image ─► push to GHCR ─► SSH deploy
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `/` | Dashboard: prices, health pill, system panel, chaos lab |
| `/health` | `ok` (200) or `unhealthy: <reasons>` (503). Unhealthy when data is older than 10 s, memory is above 300 MB, or a health fault is injected |
| `/api/stocks` | Prices as JSON (the endpoint most faults attack) |
| `/api/system` | Health reasons, CPU, memory, request stats, active faults |
| `/api/chaos` | `GET` list faults, `POST {"fault": "slow", "enabled": true}` |
| `/metrics` | Prometheus format: health, data age, memory, CPU, requests, 5xx |

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
python app/app.py                 # http://localhost:5000
```

With Docker (app + Nginx):

```bash
docker compose up --build         # http://localhost
```

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 5000 | Port for `python app/app.py` |
| `DATA_SOURCE` | simulated | `simulated` or `finnhub` (real quotes, needs `FINNHUB_API_KEY`) |
| `UPDATE_INTERVAL` | 3 | Seconds between refreshes |
| `STALE_AFTER` | 10 | Data older than this makes `/health` fail |
| `MEM_LIMIT_MB` | 300 | Memory above this makes `/health` fail |
| `CHAOS_ENABLED` | 1 | Set `0` to disable all fault injection (do this on any public server) |
| `LOG_LEVEL` | INFO | `DEBUG` also logs health/system polls |

## The chaos lab: practice routine

For each fault: **1)** switch it on in the dashboard, **2)** describe what you see, **3)** find the cause from a terminal using only logs and commands, **4)** say how you'd prevent or alert on it. The "How to debug" panel under each card has the commands; try first without opening it.

| Fault | What it simulates | Where you look first |
|---|---|---|
| Dependency down | Third-party API outage; data goes stale | logs, `curl -i /health`, `last_error` |
| Slow responses | High latency, client timeouts | `curl -w "%{time_total}"`; note `/health` stays OK |
| Intermittent 500s | Flaky bug under load | count status codes over 20 requests; `grep ERROR` in logs |
| Health check fails | Wrong/over-strict probe | `curl -i /health`; `docker ps` shows `(unhealthy)` |
| Memory leak | Slow death by OOM | `docker stats`, `ps -o rss`, `/metrics` |
| CPU spike | Runaway loop | `top`, `docker stats`, `py-spy` |
| Process crash | Container exits (code 137) | `docker ps -a`, `docker inspect`, `docker logs` |

### Startup failures (no button: you create them yourself)

These are the other half of everyday DevOps work. Break it, read the error, fix it.

1. **Port already in use.** Start the app in two terminals. Find the owner with `ss -tulpn | grep 5000`, then run the second one with `PORT=5001`.
2. **Missing secret.** `DATA_SOURCE=finnhub python app/app.py` refuses to start with a clear message. In Docker, read it with `docker compose logs app`.
3. **Wrong config value.** `DATA_SOURCE=nope python app/app.py`.
4. **Bad Gunicorn target.** Change `app:app` in the Dockerfile `CMD` to `main:app`, rebuild, and read the "Worker failed to boot" error.
5. **Wrong image tag on deploy.** Set `IMAGE=ghcr.io/you/missing:tag` and run `docker compose up -d` to see a pull failure.
6. **Nginx cannot reach the app.** Stop the app container only (`docker compose stop app`) and request `http://localhost/`: you get a 502 from Nginx. Check `docker compose logs nginx`.

## Things worth knowing for interviews

- **Liveness vs readiness:** `/health` here mixes both. Plain Docker only *labels* a container unhealthy; Kubernetes liveness probes *restart* it and readiness probes *remove it from traffic*.
- **Exit code 137** means the process was killed (usually out of memory); **1** is an application error.
- **Gunicorn runs 1 worker + threads** because prices and history live in process memory. More workers would each hold different data. A shared store (Redis) is what lets you scale out.
- **Logs are structured by level:** faults log WARNING/CRITICAL, failures ERROR, recovery INFO, so `grep` gets you the timeline.
- Never leave the chaos lab enabled on a server others can reach; anyone could break the app.

## Deploy

Same pipeline as before (`.github/workflows/ci.yml`): tests, build and push the image to GHCR, copy the compose file, `docker compose up -d`, then `curl /health`. Required repository secrets: `SERVER_HOST`, `SERVER_USER`, `SERVER_SSH_KEY`, `GHCR_PULL_USER`, `GHCR_PULL_TOKEN`. `scripts/healthcheck.sh` is a cron-able monitor that logs a line whenever `/health` fails.

## Ideas to extend

- Add real prices with `DATA_SOURCE=finnhub` (free key from finnhub.io) and watch rate limits and secrets handling become real problems.
- Scrape `/metrics` with Prometheus, graph in Grafana, and alert on `market_health_status == 0`.
- Put the app on a free-tier VM, then try the same faults there with `journalctl` / `docker logs`.
- Run it on Kubernetes (kind or minikube) with liveness and readiness probes pointing at `/health`, and watch pods restart during the memory leak.
