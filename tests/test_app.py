import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import app as market  # noqa: E402


@pytest.fixture()
def client():
    flask_app = market.create_app(start_sampler=False, stale_after=0.3, slow_seconds=0.2, crash_delay=0.05)
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    c.app = flask_app
    return c


# ---- basics -------------------------------------------------------------
def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.get_data(as_text=True) == "ok"


def test_dashboard_renders(client):
    assert b"Market Pulse" in client.get("/").data


def test_stocks_returns_ten_companies(client):
    quotes = client.get("/api/stocks").get_json()["quotes"]
    assert len(quotes) == 10
    names = {q["name"] for q in quotes}
    assert {"Meta", "Tesla", "SpaceX", "Apple", "Microsoft", "AMD", "Nvidia", "Netflix"} <= names


def test_spacex_is_private_and_has_no_price(client):
    spacex = next(q for q in client.get("/api/stocks").get_json()["quotes"] if q["symbol"] == "SPACEX")
    assert spacex["public"] is False and "price" not in spacex


def test_prices_move_on_refresh(client):
    service = client.app.config["service"]
    before = {q["symbol"]: q["price"] for q in service.snapshot()["quotes"] if "price" in q}
    service.refresh()
    after = {q["symbol"]: q["price"] for q in service.snapshot()["quotes"] if "price" in q}
    assert before != after
    assert all(p > 0 for p in after.values())


# ---- chaos faults -------------------------------------------------------
def set_fault(client, name, on=True):
    return client.post("/api/chaos", json={"fault": name, "enabled": on})


def test_unknown_fault_rejected(client):
    assert client.post("/api/chaos", json={"fault": "nope", "enabled": True}).status_code == 400


def test_health_fail_fault_and_reset(client):
    set_fault(client, "health_fail")
    res = client.get("/health")
    assert res.status_code == 503 and "injected" in res.get_data(as_text=True)
    client.post("/api/chaos/reset")
    assert client.get("/health").status_code == 200


def test_upstream_down_makes_data_stale_and_unhealthy(client):
    set_fault(client, "upstream_down")
    service = client.app.config["service"]
    assert service.refresh() is False
    assert service.status()["failures_total"] == 1
    time.sleep(0.4)  # stale_after is 0.3 s in this fixture
    res = client.get("/health")
    assert res.status_code == 503 and "stale" in res.get_data(as_text=True)
    set_fault(client, "upstream_down", on=False)
    assert service.refresh() is True
    assert client.get("/health").status_code == 200


def test_errors_fault_returns_500s(client, monkeypatch):
    set_fault(client, "errors")
    monkeypatch.setattr(market.random, "random", lambda: 0.1)  # always below the 0.5 threshold
    assert client.get("/api/stocks").status_code == 500
    monkeypatch.setattr(market.random, "random", lambda: 0.9)
    assert client.get("/api/stocks").status_code == 200


def test_slow_fault_adds_latency_but_health_stays_ok(client):
    set_fault(client, "slow")
    t0 = time.time()
    client.get("/api/stocks")
    assert time.time() - t0 >= 0.2
    assert client.get("/health").status_code == 200


def test_memory_leak_allocates_then_frees(client):
    chaos = client.app.config["chaos"]
    set_fault(client, "memory_leak")
    time.sleep(1.2)
    assert chaos.leaked_mb() >= LEAK_MIN
    set_fault(client, "memory_leak", on=False)
    assert chaos.leaked_mb() == 0


LEAK_MIN = market.LEAK_CHUNK_MB


def test_cpu_spike_starts_and_stops(client):
    chaos = client.app.config["chaos"]
    set_fault(client, "cpu_spike")
    assert chaos.is_on("cpu_spike")
    set_fault(client, "cpu_spike", on=False)
    assert not chaos.is_on("cpu_spike")


def test_crash_schedules_exit(client, monkeypatch):
    exited = []
    monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))
    res = client.post("/api/chaos", json={"fault": "crash"})
    assert res.status_code == 200
    time.sleep(0.3)
    assert exited == [1]


def test_chaos_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "0")
    c = market.create_app(start_sampler=False).test_client()
    assert c.get("/api/chaos").status_code == 404
    assert c.post("/api/chaos", json={"fault": "slow", "enabled": True}).status_code == 404


# ---- config / providers ---------------------------------------------------
def test_finnhub_without_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        market.create_app(start_sampler=False, source="finnhub")
    assert "FINNHUB_API_KEY" in str(exc.value)


def test_unknown_source_refuses_to_start():
    with pytest.raises(SystemExit):
        market.create_app(start_sampler=False, source="nope")


def test_finnhub_provider_parses_and_caches():
    calls = []

    def fake_get(url):
        calls.append(url)
        return {"c": 101.5, "pc": 100.0}

    p = market.FinnhubProvider("k", http_get=fake_get, min_interval=60)
    out = p.fetch()
    assert out["AAPL"] == {"price": 101.5, "prev_close": 100.0}
    assert "SPACEX" not in out
    n = len(calls)
    p.fetch()
    assert len(calls) == n  # second call served from cache


def test_invalid_price_is_rejected():
    class Bad:
        name = "bad"

        def fetch(self):
            return {"AAPL": {"price": float("nan"), "prev_close": 1.0}}

    svc = market.StockService(Bad(), market.Chaos())
    assert svc.refresh() is False
    assert "invalid price" in svc.status()["last_error"]


def test_prometheus_metrics(client):
    text = client.get("/metrics").get_data(as_text=True)
    assert "market_health_status 1" in text
    assert "market_data_age_seconds" in text
