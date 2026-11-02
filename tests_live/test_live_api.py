"""Real OpenAI and Tavily calls through `station api`: response shapes, usage, pricing, the ledger."""

import json
import subprocess

import pytest

from conftest import need


def test_openai_embeddings_are_metered_from_usage(station, ledger):
    body = {"model": "text-embedding-3-small", "input": ["a walk by the sea", "watching seagulls"], "dimensions": 512}
    r = station("api", "openai", "POST", "/v1/embeddings", "--json", json.dumps(body), "--what", "live test")
    data = json.loads(r.stdout)
    assert [d["index"] for d in data["data"]] == [0, 1]
    assert all(len(d["embedding"]) == 512 for d in data["data"])
    assert data["usage"]["prompt_tokens"] > 0
    (row,) = ledger()
    assert row["service"] == "openai" and row["kind"] == "/v1/embeddings"
    assert row["model"].startswith("text-embedding-3-small")
    assert row["list_usd"] == pytest.approx(data["usage"]["prompt_tokens"] * 0.02 / 1e6)
    assert row["credits"] == pytest.approx(row["list_usd"]) and row["source"] == "api"


def test_openai_transcription_is_metered(station, ledger, tmp_path):
    need("ffmpeg")
    audio = tmp_path / "speech.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "flite=text='The quick brown fox jumps over the lazy dog.'",
                    "-ac", "1", "-b:a", "32k", str(audio)], check=True)
    r = station("api", "openai", "POST", "/v1/audio/transcriptions", "-F", "model=gpt-4o-mini-transcribe",
                "-F", f"file=@{audio}", "--what", "live test")
    data = json.loads(r.stdout)
    assert "fox" in data["text"].lower()
    (row,) = ledger()
    assert row["model"].startswith("gpt-4o-mini-transcribe")
    print("transcription usage:", json.dumps(data.get("usage")), "list_usd:", row["list_usd"])
    assert row["list_usd"] is not None and 0 < row["list_usd"] < 0.01, "usage was not priced"


def test_tavily_search_and_extract_report_usage(runtime, ledger):
    from station import config, web
    from station.store import Store

    cfg = config.load(runtime)
    led = Store(runtime / "state").ledger
    found = web.search(cfg, led, "Python sqlite3 documentation", max_results=2)
    assert "http" in found
    page = web.extract(cfg, led, ["https://example.com/"])
    assert "documentation examples" in page  # Tavily returns the body text, without the <title>
    rows = ledger()
    assert [r["kind"] for r in rows] == ["/search", "/extract"]
    print("tavily units:", [r["units"] for r in rows])
    assert all(r["units"].get("tavily_credits") is not None for r in rows), "Tavily sent no usage.credits"
    search, extract = rows
    assert search["units"]["tavily_credits"] == 1 and search["list_usd"] == pytest.approx(0.008)
    # Extract bills 1 credit per 5 successful urls; one url reports 0, recorded as a priced 0.
    assert extract["units"]["tavily_credits"] == 0 and extract["list_usd"] == 0


def test_memory_end_to_end_with_real_embeddings(station, runtime, ledger):
    diary = runtime / "home" / "diary"
    diary.mkdir(parents=True)
    (diary / "2026-10-30.md").write_text(
        "# 2026-10-30\n\n## 10:00\n\nWalked along the beach and watched the seagulls.\n\n"
        "## 20:00\n\nFixed a cron expression for the weekly schedule.\n\n"
        "## 22:00\n\n今天读了《三体》，想到黑暗森林。\n")
    out = station("memory", "sync").stdout
    assert "embedded 3 chunks, 0 waiting" in out
    hits = json.loads(station("memory", "search", "a walk by the ocean", "--json").stdout)
    assert hits["note"] is None
    assert hits["results"][0]["title"] == "diary 2026-10-30 10:00" and "meaning" in hits["results"][0]["how"]
    hits = json.loads(station("memory", "search", "科幻小说", "--json").stdout)  # no shared word
    assert hits["results"][0]["title"] == "diary 2026-10-30 22:00"
    assert all(r["service"] == "openai" and r["list_usd"] for r in ledger())
