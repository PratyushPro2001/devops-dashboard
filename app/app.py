"""Market Pulse: live stock prices for ten companies, refreshed every 3 seconds.

Built as a DevOps practice target. It has a real /health check and a "chaos lab"
that breaks the app on purpose in the ways production systems usually break.

Endpoints
    /              dashboard (prices, health, system stats, chaos lab)
    /health        "ok" (200) or "unhealthy: <reasons>" (503)
    /api/stocks    prices as JSON (this is the endpoint chaos faults attack)
    /api/system    health reasons, process CPU/memory, request stats, active faults
    /api/chaos     GET fault list | POST {"fault": "...", "enabled": true}
    /metrics       Prometheus text format
"""

import gc
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, deque

import psutil
from flask import Flask, Response, g, jsonify, render_template, request

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("market")


def env_float(name, default):
    return float(os.getenv(name, default))


UPDATE_INTERVAL = env_float("UPDATE_INTERVAL", 3)   # seconds between price refreshes
STALE_AFTER = env_float("STALE_AFTER", 10)          # data older than this => unhealthy
MEM_LIMIT_MB = env_float("MEM_LIMIT_MB", 300)       # memory above this => unhealthy
SLOW_SECONDS = env_float("SLOW_SECONDS", 4)         # delay added by the "slow" fault
CRASH_DELAY = env_float("CRASH_DELAY", 1.5)
HISTORY_LEN = 40

# Starting prices are illustrative placeholders, not real quotes.
COMPANIES = [
    {"symbol": "META",   "name": "Meta",              "public": True,  "seed": 700.0},
    {"symbol": "TSLA",   "name": "Tesla",             "public": True,  "seed": 430.0},
    {"symbol": "SPACEX", "name": "SpaceX",            "public": False, "seed": None},
    {"symbol": "AAPL",   "name": "Apple",             "public": True,  "seed": 230.0},
    {"symbol": "MSFT",   "name": "Microsoft",         "public": True,  "seed": 480.0},
    {"symbol": "AMD",    "name": "AMD",               "public": True,  "seed": 160.0},
    {"symbol": "NVDA",   "name": "Nvidia",            "public": True,  "seed": 180.0},
    {"symbol": "GOOGL",  "name": "Google (Alphabet)", "public": True,  "seed": 250.0},
    {"symbol": "NFLX",   "name": "Netflix",           "public": True,  "seed": 1100.0},
    {"symbol": "AMZN",   "name": "Amazon",            "public": True,  "seed": 220.0},
]


# --------------------------------------------------------------------------
# Data providers: where the prices come from
# --------------------------------------------------------------------------
class SimulatedProvider:
    """Random-walk prices. No network, no key: good for practice."""

    name = "simulated"

    def __init__(self):
        self.rng = random.Random()
        self.prices = {c["symbol"]: c["seed"] for c in COMPANIES if c["public"]}
        self.prev_close = dict(self.prices)

    def fetch(self):
        out = {}
        for sym, price in self.prices.items():
            price *= math.exp(self.rng.gauss(0, 0.0012))
            self.prices[sym] = price
            out[sym] = {"price": round(price, 2), "prev_close": self.prev_close[sym]}
        return out


class FinnhubProvider:
    """Real quotes from finnhub.io (free API key needed).

    The free tier allows ~60 calls/minute and we need 9 calls per refresh, so
    results are cached for `min_interval` seconds. Untested against the live API.
    """

    name = "finnhub"
    URL = "https://finnhub.io/api/v1/quote?symbol={sym}&token={key}"

    def __init__(self, api_key, http_get=None, min_interval=20):
        self.key = api_key
        self.http_get = http_get or self._http_get
        self.min_interval = min_interval
        self._cache = None
        self._cache_t = 0.0

    @staticmethod
    def _http_get(url):
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def fetch(self):
        now = time.time()
        if self._cache is not None and now - self._cache_t < self.min_interval:
            return self._cache
        out = {}
        for c in COMPANIES:
            if not c["public"]:
                continue
            data = self.http_get(self.URL.format(sym=c["symbol"], key=urllib.parse.quote(self.key)))
            out[c["symbol"]] = {"price": data.get("c"), "prev_close": data.get("pc")}
        self._cache, self._cache_t = out, now
        return out


# --------------------------------------------------------------------------
# Chaos lab: faults you can switch on to practise debugging
# --------------------------------------------------------------------------
FAULTS = {
    "upstream_down": {
        "title": "Dependency down (market-data provider)",
        "kind": "toggle",
        "what": "The upstream data source stops answering, like a third-party API outage.",
        "symptoms": [
            "Prices freeze and the 'last update' counter keeps growing",
            "After 10 s /health turns 503 (stale data)",
            "Logs fill with 'market data refresh failed'",
        ],
        "debug": [
            "curl -i localhost:5000/health",
            "docker compose logs -f app   (or read the terminal running the app)",
            "Check: is it us or the dependency? Look at the last_error in /api/system",
        ],
        "lesson": "Most outages are a dependency, not your code. Good apps cache, retry with backoff, and alert on data age.",
    },
    "slow": {
        "title": "Slow responses (latency)",
        "kind": "toggle",
        "what": "Every /api/stocks call takes 4 s. The UI gives up after 3 s.",
        "symptoms": [
            "The dashboard shows 'request timed out'",
            "Average latency in the System panel jumps to ~4000 ms",
            "/health still says OK, because the process is alive",
        ],
        "debug": [
            'curl -s -o /dev/null -w "%{time_total}s\\n" localhost:5000/api/stocks',
            "Compare with: curl -s -o /dev/null -w '%{time_total}s\\n' localhost:5000/health",
        ],
        "lesson": "'Healthy' and 'fast enough for users' are different things. Monitor latency, not only up/down.",
    },
    "errors": {
        "title": "Intermittent 500 errors",
        "kind": "toggle",
        "what": "Half of the /api/stocks requests fail with HTTP 500.",
        "symptoms": [
            "The table flickers between data and an error banner",
            "5xx count rises in the System panel",
            "ERROR lines appear in the logs",
        ],
        "debug": [
            'for i in $(seq 20); do curl -s -o /dev/null -w "%{http_code}\\n" localhost:5000/api/stocks; done | sort | uniq -c',
            "docker compose logs app | grep ERROR",
        ],
        "lesson": "Intermittent failures are the hardest: one curl may look fine. Measure the error RATE over many requests.",
    },
    "health_fail": {
        "title": "Health check fails (app is actually fine)",
        "kind": "toggle",
        "what": "/health returns 503 even though prices keep flowing.",
        "symptoms": [
            "The health pill turns red but the table keeps updating",
            "docker ps shows the container as (unhealthy)",
        ],
        "debug": [
            "curl -i localhost:5000/health",
            "docker ps            # STATUS column",
            "docker inspect --format '{{json .State.Health}}' <container>",
        ],
        "lesson": "Plain Docker only LABELS a container unhealthy. Kubernetes liveness probes would restart it, load balancers would stop sending traffic. A wrong health check can cause an outage by itself.",
    },
    "memory_leak": {
        "title": "Memory leak",
        "kind": "toggle",
        "what": "The app allocates ~10 MB every 0.5 s and never frees it (stops at 600 MB).",
        "symptoms": [
            "Memory in the System panel climbs steadily",
            "Past 300 MB /health turns 503",
            "In a real container with a memory limit this ends in an OOM kill (exit code 137)",
        ],
        "debug": [
            "docker stats",
            "ps -o pid,rss,cmd -C gunicorn      # RSS in KB, run it twice and compare",
            "curl -s localhost:5000/metrics | grep memory",
        ],
        "lesson": "A leak looks fine for hours, then kills the app. Graph memory over time and alert on the trend.",
    },
    "cpu_spike": {
        "title": "CPU spike (runaway loop)",
        "kind": "toggle",
        "what": "A thread burns CPU for up to 60 s. Because of Python's GIL the whole app slows down.",
        "symptoms": [
            "CPU % in the System panel jumps to ~100",
            "Refreshes get late and latency rises",
        ],
        "debug": [
            "top   (press P to sort by CPU) or htop",
            "docker stats",
            "py-spy dump --pid <pid>    # shows which Python function is spinning",
        ],
        "lesson": "Find WHICH process is hot first (top), then WHERE in the code (profiler).",
    },
    "crash": {
        "title": "Process crash",
        "kind": "oneshot",
        "what": "Kills the server process. Under Docker it kills the whole container (like an OOM kill, exit code 137).",
        "symptoms": [
            "The page stops updating and the connection badge turns red",
            "With 'restart: unless-stopped' Docker brings it back after a few seconds",
        ],
        "debug": [
            "docker ps -a              # exited? how long ago?",
            "docker inspect --format '{{.State.ExitCode}} restarts={{.RestartCount}}' <container>",
            "docker compose logs --tail 50 app",
            "echo $?                   # exit code if you ran it in a terminal",
        ],
        "lesson": "Exit code 137 = killed (often out of memory), 1 = app error. Restart policies buy you time; they do not fix the cause.",
    },
}
TOGGLES = [k for k, v in FAULTS.items() if v["kind"] == "toggle"]

LEAK_CHUNK_MB = 10
LEAK_INTERVAL = 0.5
LEAK_CAP_MB = 600
CPU_SPIKE_MAX_SECONDS = 60


class Chaos:
    def __init__(self, enabled=True, crash_delay=CRASH_DELAY):
        self.enabled = enabled
        self.crash_delay = crash_delay
        self._lock = threading.Lock()
        self._on = {name: False for name in TOGGLES}
        self._leak = []
        self._threads = {}

    def is_on(self, name):
        return self._on.get(name, False)

    def active(self):
        return dict(self._on)

    def set(self, name, on):
        with self._lock:
            if self._on[name] == on:
                return
            self._on[name] = on
        LOG.warning("CHAOS fault %s %s", name, "ENABLED" if on else "disabled")
        if name == "memory_leak":
            if on:
                self._start("memory_leak", self._leak_loop)
            else:
                self._stop("memory_leak")
                self._leak.clear()
                gc.collect()
        elif name == "cpu_spike":
            if on:
                self._start("cpu_spike", self._cpu_loop)
            else:
                self._stop("cpu_spike")

    def reset(self):
        for name in TOGGLES:
            self.set(name, False)

    def crash(self, under_gunicorn=False):
        LOG.critical("CHAOS crash: process will be killed in %.1fs", self.crash_delay)

        def die():
            if under_gunicorn:  # kill the gunicorn master too, so the container exits like an OOM kill
                os.kill(os.getppid(), signal.SIGKILL)
            os._exit(1)

        threading.Timer(self.crash_delay, die).start()

    # -- background workers ---------------------------------------------
    def _start(self, name, target):
        t = threading.Thread(target=target, name=f"chaos-{name}", daemon=True)
        self._threads[name] = t
        t.start()

    def _stop(self, name):
        t = self._threads.pop(name, None)
        if t:
            t.join(timeout=2)

    def _leak_loop(self):
        while self.is_on("memory_leak"):
            if len(self._leak) * LEAK_CHUNK_MB < LEAK_CAP_MB:
                self._leak.append(b"\xff" * (LEAK_CHUNK_MB * 1024 * 1024))  # b"\xff" * n touches every page
            time.sleep(LEAK_INTERVAL)

    def _cpu_loop(self):
        end = time.time() + CPU_SPIKE_MAX_SECONDS
        while self.is_on("cpu_spike") and time.time() < end:
            sum(i * i for i in range(50000))
        if self.is_on("cpu_spike"):
            with self._lock:
                self._on["cpu_spike"] = False
            LOG.warning("CHAOS cpu_spike auto-stopped after %ss", CPU_SPIKE_MAX_SECONDS)

    def leaked_mb(self):
        return len(self._leak) * LEAK_CHUNK_MB


# --------------------------------------------------------------------------
# Service: keeps the latest prices and history
# --------------------------------------------------------------------------
class StockService:
    def __init__(self, provider, chaos):
        self.provider = provider
        self.chaos = chaos
        self._lock = threading.Lock()
        self.quotes = {}
        self.history = {c["symbol"]: deque(maxlen=HISTORY_LEN) for c in COMPANIES}
        self.last_ok = 0.0
        self.failures_total = 0
        self.consecutive_failures = 0
        self.last_error = None

    def prime(self, steps):
        """Pre-fill the sparkline history (simulated provider only)."""
        for _ in range(steps):
            for sym, q in self.provider.fetch().items():
                self.history[sym].append(q["price"])

    def refresh(self):
        try:
            if self.chaos.is_on("upstream_down"):
                raise ConnectionError("upstream market-data provider unreachable (injected fault)")
            raw = self.provider.fetch()
            for sym, q in raw.items():
                price = q.get("price")
                if price is None or not math.isfinite(price) or price <= 0:
                    raise ValueError(f"invalid price for {sym}: {price!r}")
        except Exception as exc:
            with self._lock:
                self.failures_total += 1
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                n = self.consecutive_failures
            LOG.error("market data refresh failed (%d in a row): %s", n, self.last_error)
            return False
        with self._lock:
            for sym, q in raw.items():
                self.quotes[sym] = q
                self.history[sym].append(q["price"])
            self.last_ok = time.time()
            if self.consecutive_failures:
                LOG.info("market data recovered after %d failed refreshes", self.consecutive_failures)
            self.consecutive_failures = 0
        return True

    def age(self):
        return (time.time() - self.last_ok) if self.last_ok else None

    def snapshot(self):
        with self._lock:
            rows = []
            for c in COMPANIES:
                sym = c["symbol"]
                row = {"symbol": sym, "name": c["name"], "public": c["public"]}
                q = self.quotes.get(sym)
                if c["public"] and q:
                    price, prev = q["price"], q["prev_close"]
                    row.update(
                        price=price,
                        change=round(price - prev, 2),
                        change_pct=round((price - prev) / prev * 100, 2) if prev else 0.0,
                        history=list(self.history[sym]),
                    )
                rows.append(row)
            return {
                "quotes": rows,
                "source": self.provider.name,
                "updated_at": self.last_ok,
                "age": self.age(),
                "interval": UPDATE_INTERVAL,
            }

    def status(self):
        with self._lock:
            return {
                "age": self.age(),
                "failures_total": self.failures_total,
                "last_error": self.last_error if self.consecutive_failures else None,
            }


class ProcessMonitor:
    def __init__(self):
        self.proc = psutil.Process()
        self.proc.cpu_percent(None)  # first call always returns 0
        self.cpu = 0.0

    def sample(self):
        self.cpu = self.proc.cpu_percent(None)

    def rss_mb(self):
        return self.proc.memory_info().rss / 1e6


class RequestStats:
    """Counts user-facing HTTP traffic (not the dashboard's own control calls)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.by_status = Counter()
        self.recent = deque()  # (timestamp, seconds, status) for the last 60 s

    def record(self, status, seconds):
        now = time.time()
        with self._lock:
            self.total += 1
            self.by_status[f"{status // 100}xx"] += 1
            self.recent.append((now, seconds, status))
            self._trim(now)

    def _trim(self, now):
        while self.recent and self.recent[0][0] < now - 60:
            self.recent.popleft()

    def snapshot(self):
        with self._lock:
            self._trim(time.time())
            durs = [d for _, d, _ in self.recent]
            return {
                "total": self.total,
                "last_60s": len(self.recent),
                "errors_60s": sum(1 for _, _, s in self.recent if s >= 500),
                "avg_ms": round(sum(durs) / len(durs) * 1000, 1) if durs else 0.0,
                "max_ms": round(max(durs) * 1000, 1) if durs else 0.0,
                "by_status": dict(self.by_status),
            }


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------
QUIET_PATHS = {"/health", "/api/system", "/api/chaos"}       # logged at DEBUG when they succeed
UNCOUNTED_PATHS = {"/api/system", "/api/chaos"}              # dashboard control plane


def build_provider(source):
    if source == "finnhub":
        key = os.getenv("FINNHUB_API_KEY", "")
        if not key:
            raise SystemExit("FATAL: DATA_SOURCE=finnhub but FINNHUB_API_KEY is not set")
        return FinnhubProvider(key)
    if source == "simulated":
        return SimulatedProvider()
    raise SystemExit(f"FATAL: unknown DATA_SOURCE {source!r} (use 'simulated' or 'finnhub')")


def create_app(start_sampler=True, source=None, stale_after=None, slow_seconds=None, crash_delay=None):
    source = source or os.getenv("DATA_SOURCE", "simulated")
    stale_after = STALE_AFTER if stale_after is None else stale_after
    slow_seconds = SLOW_SECONDS if slow_seconds is None else slow_seconds
    crash_delay = CRASH_DELAY if crash_delay is None else crash_delay
    chaos_enabled = os.getenv("CHAOS_ENABLED", "1") == "1"

    app = Flask(__name__)
    chaos = Chaos(enabled=chaos_enabled, crash_delay=crash_delay)
    service = StockService(build_provider(source), chaos)
    monitor = ProcessMonitor()
    stats = RequestStats()
    app.config.update(chaos=chaos, service=service, stats=stats, monitor=monitor)

    if source == "simulated":
        service.prime(30)
    service.refresh()
    monitor.sample()

    if start_sampler:
        def loop():
            while True:
                try:
                    service.refresh()
                    monitor.sample()
                except Exception:
                    LOG.exception("sampler loop error")
                time.sleep(UPDATE_INTERVAL)

        threading.Thread(target=loop, name="sampler", daemon=True).start()

    LOG.info("starting Market Pulse source=%s interval=%ss chaos=%s", source, UPDATE_INTERVAL,
             "on" if chaos_enabled else "off")

    def health_report():
        reasons = []
        if chaos.is_on("health_fail"):
            reasons.append("health check forced to fail (injected fault)")
        age = service.age()
        if age is None:
            reasons.append("no market data received yet")
        elif age > stale_after:
            reasons.append(f"market data is stale: {age:.0f}s old (limit {stale_after:.0f}s)")
        rss = monitor.rss_mb()
        if rss > MEM_LIMIT_MB:
            reasons.append(f"memory {rss:.0f} MB above limit {MEM_LIMIT_MB:.0f} MB")
        return reasons

    @app.before_request
    def _start_timer():
        g.t0 = time.perf_counter()

    @app.after_request
    def _after(resp):
        dur = time.perf_counter() - g.get("t0", time.perf_counter())
        path = request.path
        if path not in UNCOUNTED_PATHS and not path.startswith("/static"):
            stats.record(resp.status_code, dur)
        if resp.status_code >= 500:
            level = logging.ERROR
        elif path in QUIET_PATHS:
            level = logging.DEBUG
        else:
            level = logging.INFO
        LOG.log(level, "%s %s -> %d (%.0f ms)", request.method, path, resp.status_code, dur * 1000)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/")
    def index():
        return render_template("index.html", interval=UPDATE_INTERVAL)

    @app.route("/health")
    def health():
        reasons = health_report()
        if reasons:
            return Response("unhealthy: " + "; ".join(reasons), status=503, mimetype="text/plain")
        return Response("ok", mimetype="text/plain")

    @app.route("/api/stocks")
    def api_stocks():
        if chaos.is_on("slow"):
            time.sleep(slow_seconds)
        if chaos.is_on("errors") and random.random() < 0.5:
            return jsonify(error="internal server error (injected fault)"), 500
        return jsonify(service.snapshot())

    @app.route("/api/system")
    def api_system():
        reasons = health_report()
        return jsonify(
            health={"ok": not reasons, "reasons": reasons},
            process={"cpu": monitor.cpu, "rss_mb": round(monitor.rss_mb(), 1), "mem_limit_mb": MEM_LIMIT_MB},
            requests=stats.snapshot(),
            data={**service.status(), "stale_after": stale_after, "source": service.provider.name},
            chaos={"enabled": chaos.enabled, "active": chaos.active()},
        )

    # -- chaos lab --------------------------------------------------------
    def chaos_guard():
        if not chaos.enabled:
            return jsonify(error="chaos lab disabled (CHAOS_ENABLED=0)"), 404
        return None

    @app.route("/api/chaos", methods=["GET"])
    def chaos_list():
        if (blocked := chaos_guard()):
            return blocked
        faults = [{"id": k, **v} for k, v in FAULTS.items()]
        return jsonify(faults=faults, active=chaos.active())

    @app.route("/api/chaos", methods=["POST"])
    def chaos_set():
        if (blocked := chaos_guard()):
            return blocked
        body = request.get_json(silent=True) or {}
        name = body.get("fault")
        if name == "crash":
            chaos.crash(under_gunicorn=request.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"))
            return jsonify(status="crash scheduled", in_seconds=chaos.crash_delay)
        if name not in TOGGLES:
            return jsonify(error=f"unknown fault {name!r}", valid=TOGGLES + ["crash"]), 400
        chaos.set(name, bool(body.get("enabled")))
        return jsonify(active=chaos.active())

    @app.route("/api/chaos/reset", methods=["POST"])
    def chaos_reset():
        if (blocked := chaos_guard()):
            return blocked
        chaos.reset()
        return jsonify(active=chaos.active())

    @app.route("/metrics")
    def prometheus():
        st = service.status()
        rq = stats.snapshot()
        reasons = health_report()
        lines = []

        def metric(name, help_text, value, kind="gauge"):
            lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", f"{name} {value}"])

        metric("market_health_status", "1 if /health is ok, else 0", 0 if reasons else 1)
        metric("market_data_age_seconds", "Seconds since the last successful price refresh", round(st["age"] or -1, 2))
        metric("market_provider_failures_total", "Failed price refreshes", st["failures_total"], "counter")
        metric("market_process_memory_megabytes", "Resident memory of the app", round(monitor.rss_mb(), 1))
        metric("market_process_cpu_percent", "CPU use of the app", monitor.cpu)
        metric("market_http_requests_total", "HTTP requests served", rq["total"], "counter")
        metric("market_http_5xx_total", "HTTP 5xx responses", rq["by_status"].get("5xx", 0), "counter")
        return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
