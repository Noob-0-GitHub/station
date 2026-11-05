"""Credits: pricing, the ledger, periods (local midnight, Monday), the panel from 90%, the limit at 200%,
metered API calls. Offline: HTTP is faked at urlopen."""

import datetime as dt
import io
import json
import threading
import time
import urllib.error

import pytest

from station import api, clock, config
from station import credits as credits_mod
from station.cli import main
from station.config import DEFAULTS
from station.ledger import Ledger
from station.store import Store

from test_loop import make, use

SOL = {"input": 2.0, "output": 10.0, "cache_read": 0.10, "long_context_tokens": 272000,
       "long_input_x": 2.0, "long_output_x": 1.5}
OPUS = {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.4}


# ---- pricing and periods ------------------------------------------------------

def test_call_cost():
    assert credits_mod.call_cost(SOL, 100_000, 0, 0, 100_000, 100_000) == pytest.approx(1.21)
    # Past 272k input the whole request is 2x on input and cache, 1.5x on output.
    assert credits_mod.call_cost(SOL, 200_000, 0, 0, 100_000, 10_000) == pytest.approx(
        (2 * (200_000 * 2.0 + 100_000 * 0.10) + 1.5 * 10_000 * 10.0) / 1e6)
    assert credits_mod.call_cost(OPUS, 10, 100, 1000, 0, 5) == pytest.approx((40 + 500 + 8000 + 100) / 1e6)
    assert credits_mod.call_cost(None, 1, 0, 0, 0, 1) is None


def test_periods_reset_at_local_midnight_and_monday():
    wed = clock.wall(dt.date(2026, 11, 4), 15, 30)  # a Wednesday
    assert clock.at(credits_mod.period_start("daily", wed, {})).strftime("%a %H:%M") == "Wed 00:00"
    assert clock.at(credits_mod.period_start("weekly", wed, {})).strftime("%Y-%m-%d %a %H:%M") == "2026-11-02 Mon 00:00"
    assert clock.at(credits_mod.next_reset("weekly", wed, {})).strftime("%Y-%m-%d %a %H:%M") == "2026-11-09 Mon 00:00"
    assert clock.at(credits_mod.next_reset("daily", wed, {})).strftime("%Y-%m-%d %H:%M") == "2026-11-05 00:00"


def test_allowances_are_independent_config_values():
    assert credits_mod.allowances({"allowances": {"daily": 7, "weekly": 29}}) == {"daily": 7.0, "weekly": 29.0}


@pytest.fixture
def daily(monkeypatch):
    """Stub budget room for loop limit/reset scenarios, not production configuration."""
    def set_budget(value):
        monkeypatch.setattr(credits_mod, "allowances", lambda cfg: {"daily": value, "weekly": value * 3.5})
    return set_budget


def test_status_splits_today_and_this_week(tmp_path, daily):
    daily(20.0)
    ledger = Ledger(tmp_path / "l.db")
    now = clock.wall(dt.date(2026, 11, 4), 12, 0)  # Wednesday
    ledger.add("model", ts=now - 3600, credits=2.0)  # today
    ledger.add("tavily", ts=now - 86400, credits=3.0)  # Tuesday
    ledger.add("model", ts=now - 4 * 86400, credits=50.0)  # last week
    st = credits_mod.status(ledger, {}, now)
    assert st["daily"]["used"] == 2.0 and st["weekly"]["used"] == 5.0
    assert st["daily"]["ratio"] == pytest.approx(0.1)
    assert credits_mod.pct(0.004) == "<1%" and credits_mod.pct(0.9) == "90%" and credits_mod.pct(0) == "0%"


def test_openai_usage_is_priced_from_the_response():
    prices = DEFAULTS["prices"]
    # A transcription billed by tokens: audio input and text output.
    usage = {"type": "tokens", "input_tokens": 1000, "input_token_details": {"text_tokens": 100, "audio_tokens": 900},
             "output_tokens": 200, "total_tokens": 1200}
    assert api.openai_cost(prices, "gpt-4o-mini-transcribe", usage) == pytest.approx(
        (100 * 1.25 + 900 * 1.25 + 200 * 5.0) / 1e6)
    assert api.openai_cost(prices, "whisper-1", {"type": "duration", "seconds": 90}) == pytest.approx(0.009)
    assert api.openai_cost(prices, "text-embedding-3-small",
                           {"prompt_tokens": 1_000_000, "total_tokens": 1_000_000}) == pytest.approx(0.02)
    # A dated snapshot uses its base name; a model without a matching price is unpriced.
    assert api.openai_cost(prices, "gpt-4o-transcribe-2025-03-20", usage) is not None
    assert api.openai_cost(prices, "gpt-4o-transcribe-2025-03-20", {"type": "duration", "seconds": 60}) is None
    assert api.openai_cost(prices, "some-new-model", usage) is None


# ---- the ledger -----------------------------------------------------------------

def test_ledger_survives_concurrent_writers(tmp_path):
    path = tmp_path / "l.db"
    Ledger(path)

    def write():
        ledger = Ledger(path)
        for _ in range(50):
            ledger.add("model", credits=0.01)

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Ledger(path).totals()["rows"] == 200
    assert Ledger(path).total(0) == pytest.approx(2.0)


def test_old_usage_files_are_imported_once(tmp_path):
    import gzip

    state = tmp_path / "state"
    (state / "usage").mkdir(parents=True)
    with gzip.open(state / "usage" / "2026-10-30.jsonl.gz", "wt") as f:
        f.write(json.dumps({"ts": 1.0, "model": "m", "tokens": 10, "cost": 0.5}) + "\n")
    (state / "usage.jsonl").write_text(json.dumps({"ts": 2.0, "service": "tavily", "cost": 0.008, "credits": 0.008,
                                                   "units": 1, "unit": "tavily credit"}) + "\n")
    store = Store(state)
    rows = store.ledger.rows()
    assert [(r["service"], r["credits"]) for r in rows] == [("model", 0.5), ("tavily", 0.008)]
    assert rows[1]["units"] == {"units": 1, "unit": "tavily credit"}
    assert not (state / "usage.jsonl").exists() and not (state / "usage").exists()
    assert sorted(p.name for p in (state / "usage-legacy").iterdir()) == ["2026-10-30.jsonl.gz", "usage.jsonl"]
    Store(state)
    assert store.ledger.totals()["rows"] == 2  # not imported twice


# ---- the loop: panel, notice, limit ------------------------------------------------

def life_with(tmp_path, script):
    life = make(tmp_path, script)
    life.llm.input_tokens = 100_000  # 100k fresh input at $2/M = $0.20 per call (+ $0.00005 output)
    return life


def boards(life):
    return [json.dumps(r["messages"][-1], ensure_ascii=False) for r in life.llm.requests]


def test_panel_is_hidden_until_90_percent_then_shows_percentages(tmp_path, daily):
    daily(1.0)  # each call costs ~0.20, so after the 5th call the day is at 100%
    life = life_with(tmp_path, [[use("register", "r", name="Lin")]] +
                     [[use("bash", f"b{i}", command="true")] for i in range(5)])
    life.run(max_calls=6)
    seen = boards(life)
    assert not any("## credits" in b for b in seen[:5])
    panel = seen[5].split("## credits · built-in")[1].split("\\n\\n")[0]
    assert "daily: 100% used" in panel and "weekly: 29% used" in panel and "$" not in panel
    assert not [n for n in life.notices.list() if (n.get("key") or "").startswith("credits")]  # none before 200%


def test_limit_at_200_percent_leaves_a_notice_and_pauses_until_reset(tmp_path, daily):
    daily(0.1)  # the first call (0.20) is already at 200%
    life = life_with(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b1", command="true")],
                                      [use("bash", "b2", command="true")]])
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        daily(1000)  # the day ends: the next status sees room again

    life._pause = pause
    life.run(max_calls=3)
    assert len(pauses) == 1 and 0 < pauses[0] <= 86400 + 1
    found = [n for n in life.notices.list() if n.get("key") == "credits:daily"]
    assert len(found) == 1 and found[0]["priority"] == "high" and "made no model calls" in found[0]["text"]
    assert found[0]["expires"] > credits_mod.next_reset("daily", time.time(), life.cfg["credits"])  # still there after the reset wake
    sent = json.dumps(life.llm.requests[1]["messages"][-1], ensure_ascii=False)
    assert "reached 200%" in sent and "made no model calls until it reset" in sent


def test_spending_elsewhere_counts_toward_the_limit(tmp_path, daily):
    daily(0.1)
    life = life_with(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.llm.input_tokens = 10
    Store(tmp_path / "state").ledger.add("openai", credits=5.0, list_usd=5.0)
    pauses = []
    life._pause = lambda seconds: (pauses.append(seconds), daily(1e6))
    life.run(max_calls=2)
    assert pauses


def test_model_rows_record_list_paid_and_credits(tmp_path):
    life = life_with(tmp_path, [[use("register", "r", name="Lin")]])
    life.cfg["credits"]["factor"] = {"codex": 0.5}
    life.run(max_calls=1)
    row = Store(tmp_path / "state").ledger.rows()[-1]
    assert row["service"] == "model" and row["provider"] == "codex" and row["source"] == "loop"
    assert row["list_usd"] == pytest.approx(0.20005) and row["paid_usd"] == 0.0
    assert row["credits"] == pytest.approx(0.100025) and row["units"]["input_tokens"] == 100_000


# ---- metered API calls ---------------------------------------------------------------

class FakeResponse(io.BytesIO):
    status = 200
    headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """Replaces urlopen: records requests, answers from a list (an exception is raised)."""

    def __init__(self, monkeypatch, *answers):
        self.answers, self.requests = list(answers), []
        monkeypatch.setattr(api.urllib.request, "urlopen", self)

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode())


@pytest.fixture
def keys(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    d.mkdir()
    for name, line in (("openai.env", "OPENAI_API_KEY=sk-test\n"), ("tavily.env", "TAVILY_API_KEY=tvly-test\n")):
        (d / name).write_text(line)
        (d / name).chmod(0o600)
    for v in ("OPENAI_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "station.yaml").write_text(f"secrets: {{dir: {d}}}\ncredits: {{allowances: {{daily: 5, weekly: 17.5}}}}\n")
    return d


def test_tavily_through_the_web_tool_is_metered_from_usage(tmp_path, keys, monkeypatch):
    http = FakeHTTP(monkeypatch,
                    {"results": [{"title": "t", "url": "https://a", "content": "c"}], "usage": {"credits": 1}},
                    {"results": [{"url": "https://a/1", "raw_content": "x"}], "failed_results": [], "usage": {"credits": 2}})
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("web", "w1", action="search", query="q")],
                           [use("web", "w2", action="extract", urls=["https://a"])], [use("bash", "b", command="true")]])
    life.cfg["secrets"]["dir"] = str(keys)
    life.run(max_calls=4)
    sent = [json.loads(r.data) for r in http.requests]
    assert all(b["include_usage"] is True for b in sent)
    assert http.requests[0].get_header("Authorization") == "Bearer tvly-test"
    rows = Store(tmp_path / "state").ledger.rows(service="tavily")
    assert [r["units"]["tavily_credits"] for r in rows] == [1, 2]
    assert [r["list_usd"] for r in rows] == pytest.approx([0.008, 0.016])
    assert rows[0]["what"] == "web search: q"


def test_station_api_meters_openai_from_the_response(tmp_path, keys, monkeypatch, capsys):
    http = FakeHTTP(monkeypatch, {"object": "list", "model": "text-embedding-3-small",
                                  "data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 500, "total_tokens": 500}})
    code = main(["--root", str(tmp_path), "api", "openai", "POST", "/v1/embeddings",
                 "--json", '{"model": "text-embedding-3-small", "input": "hello"}', "--what", "memory test"])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["data"][0]["embedding"] == [0.1]
    assert "0.000010 credits" in err
    assert http.requests[0].full_url == "https://api.openai.com/v1/embeddings"
    assert http.requests[0].get_header("Authorization") == "Bearer sk-test"
    row = Store(tmp_path / "state").ledger.rows(service="openai")[0]
    assert row["model"] == "text-embedding-3-small" and row["what"] == "memory test" and row["source"] == "api"
    assert row["list_usd"] == pytest.approx(500 * 0.02 / 1e6) and row["paid_usd"] == pytest.approx(row["list_usd"])


def test_station_api_multipart_upload(tmp_path, keys, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3fake")
    http = FakeHTTP(monkeypatch, {"text": "hi", "usage": {"type": "tokens", "input_tokens": 100, "output_tokens": 10,
                                                          "input_token_details": {"audio_tokens": 100}}})
    assert main(["--root", str(tmp_path), "api", "openai", "POST", "/v1/audio/transcriptions",
                 "-F", "model=gpt-4o-mini-transcribe", "-F", f"file=@{audio}"]) == 0
    body = http.requests[0].data
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in body and b"ID3fake" in body
    row = Store(tmp_path / "state").ledger.rows(service="openai")[0]
    assert row["model"] == "gpt-4o-mini-transcribe"
    assert row["list_usd"] == pytest.approx((100 * 1.25 + 10 * 5.0) / 1e6)


def test_image_generation_usage_is_priced_by_token_kind():
    prices = config.DEFAULTS["prices"]
    # Shape of a real /v1/images/generations response for gpt-image-2.5-flare (ledger, 2026-11-04).
    usage = {"input_tokens": 194, "input_tokens_details": {"image_tokens": 0, "text_tokens": 194},
             "output_tokens": 343, "output_tokens_details": {"image_tokens": 343, "text_tokens": 0}, "total_tokens": 537}
    assert api.openai_cost(prices, "gpt-image-2.5-flare", usage) == pytest.approx((194 * 5 + 343 * 30) / 1e6)
    assert api.openai_cost(prices, "gpt-image-2.5-flare-2026-09-08", usage) == pytest.approx((194 * 5 + 343 * 30) / 1e6)
    edit = {"input_tokens": 1100, "input_tokens_details": {"image_tokens": 1000, "text_tokens": 100, "cached_tokens": 300,
                                                            "cached_tokens_details": {"image_tokens": 200}},
            "output_tokens": 500}
    expected = ((100 - 100) * 5 + 100 * 1.25 + 800 * 8 + 200 * 2 + 500 * 30) / 1e6
    assert api.openai_cost(prices, "gpt-image-2.5-flare", edit) == pytest.approx(expected)
    # A model without an image_input rate is not priced as if image tokens were text.
    assert api.openai_cost(prices, "gpt-6-luna", edit) is None


def test_unpriced_calls_are_recorded_and_shown(tmp_path, keys, monkeypatch, capsys):
    FakeHTTP(monkeypatch, {"id": "x", "model": "some-new-model"})
    assert main(["--root", str(tmp_path), "api", "openai", "POST", "/v1/things", "--json", "{}"]) == 0
    assert "unpriced" in capsys.readouterr().err
    main(["--root", str(tmp_path), "credits"])
    assert "1 unpriced" in capsys.readouterr().out


def test_station_api_refuses_at_the_limit_and_bad_input(tmp_path, keys, monkeypatch, capsys):
    FakeHTTP(monkeypatch)  # any request would fail the test with IndexError
    Store(tmp_path / "state").ledger.add("model", credits=10.0)  # 200% of 5
    args = ["--root", str(tmp_path), "api", "openai", "POST", "/v1/embeddings", "--json", "{}"]
    assert main(args) == 4 and "credit limit reached" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "api", "nope", "POST", "/v1/x"]) == 2
    assert main(["--root", str(tmp_path), "api", "openai", "POST", "../etc"]) == 2
    assert main(["--root", str(tmp_path), "api", "openai", "POST", "/v1/x", "--json", '{"stream": true}']) == 2


def test_failed_calls_are_not_charged(tmp_path, keys, monkeypatch, capsys):
    FakeHTTP(monkeypatch, urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": {"message": "no"}}')))
    assert main(["--root", str(tmp_path), "api", "openai", "POST", "/v1/embeddings", "--json", "{}"]) == 1
    assert '"no"' in capsys.readouterr().err
    assert Store(tmp_path / "state").ledger.totals()["rows"] == 0


def test_manual_record_and_credits_commands(tmp_path, capsys, daily):
    daily(4.0)
    (tmp_path / "station.yaml").write_text("credits: {factor: {replicate: 2}}\n")
    root = ["--root", str(tmp_path)]
    assert main(root + ["api", "replicate", "--record", "0.5", "--what", "an image"]) == 0
    assert "1.0000 credits" in capsys.readouterr().out
    row = Store(tmp_path / "state").ledger.rows()[-1]
    assert row["source"] == "manual" and row["list_usd"] == 0.5 and row["what"] == "an image"
    assert main(root + ["api", "Model", "--record", "1"]) == 2
    assert main(root + ["api", "x", "--record", "-1"]) == 2
    assert main(root + ["api", "openai", "POST", "/v1/x", "--record", "1"]) == 2  # a record makes no call
    assert main(root + ["api", "openai"]) == 2  # neither a call nor a record
    capsys.readouterr()
    main(root + ["credits", "--log", "5"])
    out = capsys.readouterr().out
    assert "daily: 25% used" in out and "weekly: 7% used" in out and "replicate:" in out and "an image" in out
    main(root + ["credits", "--json"])
    assert json.loads(capsys.readouterr().out)["weekly"]["by_service"]["replicate"]["credits"] == 1.0
