"""Tests for kiro_crew.dashboard.handlers.usage."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.dashboard.handlers.usage import (
    _cached_parse_sessions,
    _parse_sessions,
    _parse_token_history,
    _resolve_model,
    _wrapper_chain,
    api_kiro_usage,
    get_usage_cache,
    persist_token_record,
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
    read_effective_model,
    read_turn_model,
)

# ── _parse_sessions ─────────────────────────────────────────────────────


def _write_session(path, lines, mtime=None):
    """Write a JSONL session file and optionally set mtime."""
    path.write_text("\n".join(json.dumps(item) for item in lines) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))


class TestParseSessions:
    @pytest.mark.parametrize("directory_exists", [False, True])
    def test_empty_session_stats(self, tmp_path, directory_exists):
        sessions_dir = tmp_path / "cli"
        if directory_exists:
            sessions_dir.mkdir()
        with patch.object(usage_mod, "_SESSIONS_DIR", sessions_dir):
            result = _parse_sessions()
        assert result == {
            "total_sessions": 0,
            "total_messages": 0,
            "total_tool_calls": 0,
            "all_time_sessions": 0,
            "daily_history": [],
            "today": {"sessions": 0, "messages": 0, "tool_calls": 0},
            "this_week": {"sessions": 0, "messages": 0, "tool_calls": 0},
            "this_month": {"sessions": 0, "messages": 0, "tool_calls": 0},
            "avg_msgs_per_session": 0,
            "avg_tools_per_session": 0,
            # An empty history is COMPLETE data, not incomplete: nothing was
            # dropped, so the did-not-load total is a present zero rather than
            # an absent key. Omitting it would make "complete" and
            # "unknown" indistinguishable on the wire -- the adapter's
            # ``s.refused_transcripts ?? 0`` would synthesise the promise of
            # completeness the payload never made.
            "refused_transcripts": 0,
        }
        assert sessions_dir.exists() == directory_exists

    def test_iterdir_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch("pathlib.Path.iterdir", side_effect=OSError("boom")),
        ):
            result = _parse_sessions()
            assert "error" in result
            # The OSError carries a filesystem path; it stays server-side. The
            # returned `error` is a generic message with a machine-readable code.
            assert "boom" not in result["error"]
            assert result["error"] == "cannot read sessions directory"
            assert result["code"] == "sessions_dir_unreadable"

    def test_iterdir_oserror_keeps_the_whole_statistics_shape(self, tmp_path):
        """An unreadable directory reports the reason WITHOUT changing the shape.

        Consumers read the period keys unconditionally --
        ``website/src/providers/adapters/acp.ts`` goes straight to
        ``s.today.sessions`` on the 200 -- so an error-ONLY object is not a
        degraded answer, it is a differently-shaped one, and it raises a
        ``TypeError`` in the client instead of showing the message this branch
        exists to produce. The zeros are a shape, not a measurement, which is why
        ``error`` has to travel WITH them.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        with patch.object(usage_mod, "_SESSIONS_DIR", empty):
            baseline = _parse_sessions()
        # Guard the guard: a baseline that lost its period keys would make the
        # comparison below pass while proving nothing.
        assert {"today", "this_week", "this_month", "daily_history"} <= set(baseline)

        d = tmp_path / "cli"
        d.mkdir()
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch("pathlib.Path.iterdir", side_effect=OSError("boom")),
        ):
            result = _parse_sessions()

        assert result["error"] == "cannot read sessions directory"
        assert result["code"] == "sessions_dir_unreadable"
        assert "boom" not in result["error"]
        # Exactly the successful key set plus the two error keys, so a statistic
        # added later cannot go missing from this branch without failing here.
        assert set(result) - {"error", "code"} == set(baseline)
        assert {k: v for k, v in result.items() if k in baseline} == baseline

    def test_skips_non_jsonl(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        (d / "readme.txt").write_text("hi")
        with patch.object(usage_mod, "_SESSIONS_DIR", d):
            r = _parse_sessions()
            assert r["total_sessions"] == 0
            assert r["all_time_sessions"] == 0

    def test_skips_invalid_path(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        _write_session(d / "s1.jsonl", [{"kind": "Prompt"}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=None),
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 0

    def test_refused_transcripts_are_reported_not_swallowed(self, tmp_path, caplog):
        """A refused transcript is skipped, and the skip must be visible.
        Before this, a home whose every transcript the path validator refused
        rendered as a legitimate "zero sessions" with nothing to say why.

        Asserted on BOTH the payload field and the log: the count is now carried
        in ``refused_transcripts`` so the usage page can render a warning instead
        of a confident zero; UsageTab renders that warning.
        """
        d = tmp_path / "cli"
        d.mkdir()
        _write_session(d / "s1.jsonl", [{"kind": "Prompt"}])
        _write_session(d / "s2.jsonl", [{"kind": "Prompt"}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=None),
            caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.usage"),
        ):
            r = _parse_sessions()
        assert r["total_sessions"] == 0
        # The silent-zero fix: the count reaches the client, not just the log.
        assert r["refused_transcripts"] == 2
        # One aggregated record, not one per file: a UNC home refuses every
        # transcript, and per-file logging would emit thousands.
        refusals = [rec for rec in caplog.records if "could not be loaded" in rec.getMessage()]
        assert len(refusals) == 1
        assert "2" in refusals[0].getMessage()

    def test_no_refusal_log_when_every_transcript_validates(self, tmp_path, caplog):
        """The counterpart: the healthy path stays quiet and reports zero refusals."""
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
            caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.usage"),
        ):
            r = _parse_sessions()
        assert r["total_sessions"] == 1
        assert r["refused_transcripts"] == 0
        assert not [rec for rec in caplog.records if "could not be loaded" in rec.getMessage()]

    def test_stat_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        orig_stat = Path.stat

        def stat_side_effect(self_, *a, **kw):
            if self_.name == f.name:
                raise OSError("stat fail")
            return orig_stat(self_, *a, **kw)

        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
            patch.object(Path, "stat", stat_side_effect),
        ):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 0
            # First Principles: a stat failure is a did-not-load branch, so
            # it feeds the incomplete-data count -- otherwise the transcript
            # vanishes with no trace and the warning stays silent.
            assert r["refused_transcripts"] == 1

    def test_all_three_did_not_load_branches_feed_the_count(self, tmp_path, caplog):
        """First Principles: the warning's ABSENCE promises complete data,
        so every branch that drops a transcript must feed refused_transcripts --
        not just the validator refusal. Three transcripts, one lost to each of
        the three branches (validator refusal, stat failure, read failure);
        the count must be 3 and total_sessions 0, so a page hit by only the two
        non-validator branches still shows the warning rather than a silent
        under-count.
        """
        d = tmp_path / "cli"
        d.mkdir()
        f_refuse = d / "refuse.jsonl"
        f_stat = d / "stat.jsonl"
        f_read = d / "read.jsonl"
        _write_session(f_refuse, [{"kind": "Prompt"}])
        _write_session(f_stat, [{"kind": "Prompt"}])
        _write_session(f_read, [{"kind": "Prompt"}])

        def validate(p):
            return None if p.endswith("refuse.jsonl") else p

        orig_stat = Path.stat

        def stat_side_effect(self_, *a, **kw):
            if self_.name == f_stat.name:
                raise OSError("stat fail")
            return orig_stat(self_, *a, **kw)

        orig_open = Path.open

        def open_side_effect(self_, *a, **kw):
            if self_.name == f_read.name:
                raise OSError("read fail")
            return orig_open(self_, *a, **kw)

        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", side_effect=validate),
            patch.object(Path, "stat", stat_side_effect),
            patch.object(Path, "open", open_side_effect),
            caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.usage"),
        ):
            r = _parse_sessions()
        assert r["total_sessions"] == 0
        assert r["refused_transcripts"] == 3
        loaded_msgs = [rec for rec in caplog.records if "could not be loaded" in rec.getMessage()]
        assert len(loaded_msgs) == 1
        assert "3" in loaded_msgs[0].getMessage()

    def test_old_session_is_not_counted_as_did_not_load(self, tmp_path):
        """The cutoff branch is a deliberate 30-day window filter, NOT a load
        failure: an old transcript loaded fine, so it must NOT inflate the
        incomplete-data count (it stays in all_time_sessions)."""
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "old.jsonl"
        old_mtime = time.time() - (60 * 86400)
        _write_session(f, [{"kind": "Prompt"}], mtime=old_mtime)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
        assert r["all_time_sessions"] == 1
        assert r["refused_transcripts"] == 0

    def test_old_session_counted_alltime_only(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "old.jsonl"
        old_mtime = time.time() - (60 * 86400)  # 60 days ago
        _write_session(f, [{"kind": "Prompt"}], mtime=old_mtime)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 1
            assert r["total_sessions"] == 0

    def test_counts_messages_and_tools(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt"},
            {"kind": "AssistantMessage"},
            {"kind": "ToolResults"},
            {"kind": "ToolResults"},
            {"kind": "Other"},
        ]
        _write_session(f, lines)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            assert r["total_messages"] == 2
            assert r["total_tool_calls"] == 2
            assert r["all_time_sessions"] == 1
            assert len(r["daily_history"]) == 1
            assert r["avg_msgs_per_session"] == 2.0
            assert r["avg_tools_per_session"] == 2.0

    def test_json_decode_error_skipped(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        f.write_text('{"kind":"Prompt"}\nNOT_JSON\n{"kind":"ToolResults"}\n')
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_non_dict_json_skipped(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        f.write_text('"just a string"\n42\nnull\n[1,2]\n{"kind":"Prompt"}\n')
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_messages"] == 1

    def test_file_read_oserror(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        orig_open = Path.open

        def open_raises(self_, *a, **kw):
            if self_.suffix == ".jsonl":
                raise OSError("read fail")
            return orig_open(self_, *a, **kw)

        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
            patch.object(Path, "open", open_raises),
        ):
            r = _parse_sessions()
            assert r["all_time_sessions"] == 1
            assert r["total_sessions"] == 0  # not counted when file read fails
            assert r["total_messages"] == 0

    def test_period_summaries(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        now = time.time()
        f = d / "today.jsonl"
        _write_session(f, [{"kind": "Prompt"}, {"kind": "ToolResults"}], mtime=now)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["today"]["sessions"] == 1
            assert r["today"]["messages"] == 1
            assert r["today"]["tool_calls"] == 1
            assert r["this_week"]["sessions"] >= 1
            assert r["this_month"]["sessions"] >= 1

    def test_timestamp_derives_day(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "2026-04-20T10:00:00"},
            {"kind": "ToolResults"},
        ]
        # mtime is today, but timestamp says April 20
        _write_session(f, lines)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            assert r["daily_history"][0]["date"] == "2026-04-20"
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_malformed_timestamp_fallback_to_mtime(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "not-a-date"},
            {"kind": "ToolResults"},
        ]
        _write_session(f, lines)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            # Messages still counted despite bad timestamp
            assert r["total_messages"] == 1
            assert r["total_tool_calls"] == 1

    def test_z_suffix_timestamp(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s1.jsonl"
        lines = [
            {"kind": "Prompt", "timestamp": "2026-04-20T10:00:00Z"},
            {"kind": "AssistantMessage"},
        ]
        _write_session(f, lines)
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
        ):
            r = _parse_sessions()
            assert r["total_sessions"] == 1
            # Z suffix parsed and converted to local TZ via .astimezone()
            expected = (
                datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d")
            )
            assert r["daily_history"][0]["date"] == expected
            assert r["total_messages"] == 2


# ── get_usage_cache ──────────────────────────────────────────────────────


class TestGetUsageCache:
    def test_returns_cache(self):
        mock_cache = {"credits_used": 42}
        with patch.dict(
            "sys.modules",
            {"kiro_crew.dashboard.handlers.sessions": MagicMock(_usage_cache=mock_cache)},
        ):
            assert get_usage_cache() == {"credits_used": 42}

    def test_empty_cache(self):
        with patch.dict(
            "sys.modules", {"kiro_crew.dashboard.handlers.sessions": MagicMock(_usage_cache={})}
        ):
            assert get_usage_cache() == {}

    def test_import_error(self):
        with patch.dict("sys.modules", {"kiro_crew.dashboard.handlers.sessions": None}):
            assert get_usage_cache() == {}


# ── api_kiro_usage ───────────────────────────────────────────────────────

# The route waits ``_USAGE_SESSION_WAIT_SECONDS`` for the session scan, then answers
# billing plus ``refreshing: True`` while the scan continues. How long a real scan
# takes belongs to the host (an executor thread and a stat of a just-written file on a
# loaded Windows runner exceed the budget), so a test about the SETTLED payload waits
# on the scan task the route left running and asks again rather than racing the budget.
# The backstop only turns a scan that never finishes into a named failure.
_SCAN_BACKSTOP_SECONDS = 30.0


async def _get_settled_usage(client: TestClient) -> tuple[int, dict]:
    """GET /api/usage/kiro and return the answer the finished session scan gives."""
    response = await client.get("/api/usage/kiro")
    data = await response.json()
    if data.get("refreshing"):
        task = usage_mod._USAGE_SESSION_REFRESH_TASK
        assert task is not None, f"refreshing answer with no refresh task: {data}"
        done, _ = await asyncio.wait({task}, timeout=_SCAN_BACKSTOP_SECONDS)
        assert done, f"session scan unfinished after the {_SCAN_BACKSTOP_SECONDS}s backstop"
        response = await client.get("/api/usage/kiro")
        data = await response.json()
        assert data["refreshing"] is False, data
    return response.status, data


class TestApiKiroUsage:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        usage_mod._CACHE = {}
        usage_mod._CACHE_TS = 0.0
        usage_mod._SESSIONS_CACHE = None
        usage_mod._SESSIONS_CACHE_TS = 0.0
        usage_mod._USAGE_SESSION_REFRESH_TASK = None
        yield
        usage_mod._CACHE = {}
        usage_mod._CACHE_TS = 0.0
        usage_mod._SESSIONS_CACHE = None
        usage_mod._SESSIONS_CACHE_TS = 0.0
        usage_mod._USAGE_SESSION_REFRESH_TASK = None

    @pytest.mark.asyncio
    async def test_returns_cached(self):
        usage_mod._CACHE = {"cached": True}
        usage_mod._CACHE_TS = time.time()
        app = web.Application()
        app.router.add_get("/api/usage/kiro", api_kiro_usage)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/usage/kiro")
            assert resp.status == 200
            data = await resp.json()
            assert data == {"cached": True}

    @pytest.mark.asyncio
    async def test_fresh_fetch(self, tmp_path):
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
            patch.object(
                usage_mod,
                "get_usage_cache",
                return_value={
                    "credits_used": 10,
                    "credits_plan": 100,
                    "cost_usd": 0,
                    "resets": "May 1",
                    "plan": "Pro",
                    "overage_rate": 0.01,
                },
            ),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                status, data = await _get_settled_usage(client)
                assert status == 200
                assert "sessions" in data
                assert "billing" in data
                assert data["billing"]["credits_used"] == 10
                assert data["sessions"]["total_sessions"] == 1
                assert data["refreshing"] is False

    @pytest.mark.asyncio
    async def test_slow_session_scan_returns_billing_then_refreshes(self):
        release = usage_mod.asyncio.Event()
        finished = usage_mod._empty_session_summary()
        finished["total_sessions"] = 2

        async def _slow_parse():
            await release.wait()
            return finished

        billing = {"credits_used": 25, "credits_plan": 100, "plan": "Pro"}
        with (
            patch.object(usage_mod, "_cached_parse_sessions", _slow_parse),
            patch.object(usage_mod, "_USAGE_SESSION_WAIT_SECONDS", 0.001),
            patch.object(usage_mod, "get_usage_cache", return_value=billing),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                first = await client.get("/api/usage/kiro")
                assert first.status == 200
                partial = await first.json()
                assert partial["refreshing"] is True
                assert partial["billing"]["plan"] == "Pro"
                assert partial["billing"]["credits_used"] == 25
                assert partial["sessions"]["today"] == {
                    "sessions": 0,
                    "messages": 0,
                    "tool_calls": 0,
                }
                # The placeholder must not hide the finished scan for two minutes.
                assert usage_mod._CACHE == {}

                release.set()
                task = usage_mod._USAGE_SESSION_REFRESH_TASK
                assert task is not None
                await task

                second = await client.get("/api/usage/kiro")
                complete = await second.json()
                assert complete["refreshing"] is False
                assert complete["sessions"]["total_sessions"] == 2
                assert usage_mod._CACHE == complete

    @pytest.mark.asyncio
    async def test_missing_directory_preserves_billing_and_refreshes(self, tmp_path):
        sessions_dir = tmp_path / "cli"
        session_file = sessions_dir / "first.jsonl"
        billing = {"credits_used": 10, "credits_plan": 100, "plan": "Pro"}
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", sessions_dir),
            patch.object(usage_mod, "get_usage_cache", return_value=billing),
            patch.object(usage_mod, "validate_file_path", return_value=str(session_file)),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                status, data = await _get_settled_usage(client)
                assert status == 200
                assert "error" not in data
                assert data["sessions"]["total_sessions"] == 0
                for period in ("today", "this_week", "this_month"):
                    assert data["sessions"][period] == {
                        "sessions": 0,
                        "messages": 0,
                        "tool_calls": 0,
                    }
                assert data["billing"]["credits_used"] == 10
                assert data["billing"]["plan"] == "Pro"
                assert not sessions_dir.exists()

                sessions_dir.mkdir()
                _write_session(session_file, [{"kind": "Prompt"}])
                usage_mod._CACHE_TS = time.time() - usage_mod._CACHE_TTL - 1
                usage_mod._SESSIONS_CACHE_TS = time.time() - usage_mod._CACHE_TTL - 1
                status, updated = await _get_settled_usage(client)
                assert status == 200
                assert updated["sessions"]["total_sessions"] == 1
                assert updated["sessions"]["total_messages"] == 1
                assert updated["billing"] == data["billing"]

    @pytest.mark.asyncio
    async def test_error_not_cached(self, tmp_path):
        invalid_directory = tmp_path / "cli"
        invalid_directory.write_text("not a directory", encoding="utf-8")
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", invalid_directory),
            patch.object(usage_mod, "get_usage_cache", return_value={}),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                _status, data = await _get_settled_usage(client)
                assert "error" in data
                # Cache should NOT be set
                assert usage_mod._CACHE == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_directory_still_answers_the_full_shape(self, tmp_path):
        """The reason rides WITH the statistics, and billing is unaffected.

        A file where the transcript directory should be makes ``iterdir()`` raise
        a real ``NotADirectoryError`` -- no mock -- which is the branch a
        roaming-profile or permission-denied home takes. The route answers 200
        because billing is a separate half of the payload, so the sessions half
        has to stay readable by a client that goes straight to ``today``.
        """
        invalid_directory = tmp_path / "cli"
        invalid_directory.write_text("not a directory", encoding="utf-8")
        billing = {"credits_used": 10, "credits_plan": 100, "plan": "Pro"}
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", invalid_directory),
            patch.object(usage_mod, "get_usage_cache", return_value=billing),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                status, data = await _get_settled_usage(client)
                assert status == 200

        assert data["error"] == "cannot read sessions directory"
        assert data["sessions"]["code"] == "sessions_dir_unreadable"
        assert data["sessions"]["total_sessions"] == 0
        for period in ("today", "this_week", "this_month"):
            assert data["sessions"][period] == {
                "sessions": 0,
                "messages": 0,
                "tool_calls": 0,
            }
        assert data["sessions"]["daily_history"] == []
        assert data["billing"]["plan"] == "Pro"
        assert usage_mod._CACHE == {}

    @pytest.mark.asyncio
    async def test_unavailable_sentinel_yields_empty_billing(self, tmp_path):
        # The {"available": False} sentinel is truthy but carries no plan — billing
        # must stay {} rather than a dict of all-None fields.
        d = tmp_path / "cli"
        d.mkdir()
        f = d / "s.jsonl"
        _write_session(f, [{"kind": "Prompt"}])
        with (
            patch.object(usage_mod, "_SESSIONS_DIR", d),
            patch.object(usage_mod, "validate_file_path", return_value=str(f)),
            patch.object(usage_mod, "get_usage_cache", return_value={"available": False}),
        ):
            app = web.Application()
            app.router.add_get("/api/usage/kiro", api_kiro_usage)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/usage/kiro")
                data = await resp.json()
                assert data["billing"] == {}


# ── _parse_token_history ─────────────────────────────────────────────────


def _reset_token_cache():
    """Tests share a process-global cache; clear it before each test that
    swaps out the shard directory."""
    usage_mod._TOKEN_CACHE = {}
    usage_mod._TOKEN_CACHE_KEY = None
    usage_mod._TOKEN_CACHE_TS = 0.0


def _patch_shard_layout(monkeypatch, tmp_path):
    """Point the module at an isolated shard directory so each test runs
    against an empty slate.
    """
    shard_dir = tmp_path / "tokens"
    shard_dir.mkdir()
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    _reset_token_cache()
    return shard_dir


def _unwritable_shard_dir(monkeypatch, tmp_path):
    """Point the module at a shard directory that can never be created.

    ``tmp_path / "blocker"`` is a regular file, so ``mkdir(parents=True)`` on
    anything beneath it fails (NotADirectoryError on POSIX, FileExistsError /
    PermissionError on Windows) -- the same failure shape a read-only data home
    produces, without borrowing a path such as ``/proc`` on the operator's host.
    The write attempt therefore stays inside the test's own sandbox.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    shard_dir = blocker / "usage" / "tokens"
    assert shard_dir.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
    _reset_token_cache()
    return shard_dir


def _write_shard(shard_dir: Path, day: str, records):
    (shard_dir / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


class TestParseTokenHistory:
    def test_no_file(self, tmp_path, monkeypatch):
        _patch_shard_layout(monkeypatch, tmp_path)
        # Empty shard dir → empty result.
        assert _parse_token_history() == {}

    def test_skips_old_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        record = {
            "_type": "tokens",
            "ts": "2020-01-01T00:00:00+00:00",
            "input": 100,
            "output": 50,
        }
        _write_shard(shard_dir, "2020-01-01", [record])
        # Old shard is outside the 30-day window → directory listing skips
        # it, so the parse returns the empty-history result.
        assert _parse_token_history() == {}

    def test_aggregates_tokens(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        ts1 = (now - timedelta(minutes=30)).isoformat()
        ts2 = (now - timedelta(minutes=15)).isoformat()
        records = [
            {
                "_type": "tokens",
                "ts": ts1,
                "input": 100,
                "output": 50,
                "cache_create": 10,
                "cache_read": 5,
                "cost": 0.01,
            },
            {
                "_type": "tokens",
                "ts": ts2,
                "input": 200,
                "output": 100,
                "cache_create": 20,
                "cache_read": 10,
                "cost": 0.02,
            },
        ]
        _write_shard(shard_dir, now.strftime("%Y-%m-%d"), records)
        result = _parse_token_history()
        assert result["total_input"] == 300
        assert result["total_output"] == 150
        assert result["cache_creation"] == 30
        assert result["cache_read"] == 15
        assert result["total"] == 495
        assert result["cost_usd"] == 0.03
        assert len(result["daily_history"]) == 1
        assert result["daily_history"][0]["input"] == 300

    def test_multi_day_aggregation(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0)
        yesterday = now - timedelta(days=1)
        _write_shard(
            shard_dir,
            yesterday.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": yesterday.isoformat(), "input": 50, "output": 25}],
        )
        _write_shard(
            shard_dir,
            now.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": now.isoformat(), "input": 100, "output": 75}],
        )
        result = _parse_token_history()
        assert result["total_input"] == 150
        assert result["total_output"] == 100
        assert len(result["daily_history"]) == 2

    def test_ignores_non_token_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        now = datetime.now().astimezone()
        recent_ts = (now - timedelta(hours=1)).isoformat()
        records = [
            {"_type": "metadata", "created_at": recent_ts},
            {"role": "user", "content": "hello"},
            {"_type": "tokens", "ts": recent_ts, "input": 42, "output": 10},
        ]
        _write_shard(shard_dir, now.strftime("%Y-%m-%d"), records)
        result = _parse_token_history()
        assert result["total_input"] == 42
        assert result["total_output"] == 10

    def test_skips_files_with_invalid_names(self, tmp_path, monkeypatch):
        """A stray file with a non-date stem (e.g. README, .DS_Store) in
        the shard dir must not crash the parser."""
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        (shard_dir / "README.txt").write_text("not a shard")
        (shard_dir / "garbage.jsonl").write_text("{}\n")
        # Real shard alongside the noise.
        now = datetime.now().astimezone()
        _write_shard(
            shard_dir,
            now.strftime("%Y-%m-%d"),
            [{"_type": "tokens", "ts": now.isoformat(), "input": 9, "output": 1}],
        )
        result = _parse_token_history()
        assert result["total_input"] == 9


# ── _persist_token_record ────────────────────────────────────────────────


class TestPersistTokenRecord:
    def test_writes_token_record(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)

        slot_key = "test-slot"
        model = "claude-sonnet-4"

        event = MagicMock()
        event.input_tokens = 500
        event.output_tokens = 200
        event.cache_creation_tokens = 50
        event.cache_read_tokens = 25
        event.cost_usd = 0.05
        event.num_turns = 3
        event.duration_ms = 1500

        persist_token_record(slot_key, model, event)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        shard_path = shard_dir / f"{today}.jsonl"
        assert shard_path.exists()
        lines = shard_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["_type"] == "tokens"
        assert record["slot"] == "test-slot"
        assert record["model"] == "claude-sonnet-4"
        assert record["input"] == 500
        assert record["output"] == 200
        assert record["cache_create"] == 50
        assert record["cache_read"] == 25
        assert record["cost"] == 0.05
        assert record["turns"] == 3
        assert record["duration_ms"] == 1500
        assert "ts" in record

    def test_no_crash_on_error(self, tmp_path, monkeypatch):
        slot_key = "test"
        model = "test-model"
        event = MagicMock()
        event.input_tokens = 100
        event.output_tokens = 50

        # Point the shard dir at an uncreatable location; persist must swallow.
        # A regular FILE where the parent directory should be makes every
        # ``mkdir(parents=True)`` fail on every platform, and keeps the failing
        # write under ``tmp_path`` rather than probing a path on the real host.
        shard_dir = _unwritable_shard_dir(monkeypatch, tmp_path)
        # Should not raise
        persist_token_record(slot_key, model, event)
        assert not shard_dir.exists()
        assert (tmp_path / "blocker").is_file(), "the blocker file was replaced"

    def test_appends_multiple_records(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)

        slot_key = "s1"
        model = "opus"

        event1 = MagicMock()
        event1.input_tokens = 100
        event1.output_tokens = 50
        event1.cache_creation_tokens = 0
        event1.cache_read_tokens = 0
        event1.cost_usd = 0.01
        event1.num_turns = 1
        event1.duration_ms = 500

        event2 = MagicMock()
        event2.input_tokens = 200
        event2.output_tokens = 80
        event2.cache_creation_tokens = 10
        event2.cache_read_tokens = 5
        event2.cost_usd = 0.02
        event2.num_turns = 1
        event2.duration_ms = 800

        persist_token_record(slot_key, model, event1)
        persist_token_record(slot_key, model, event2)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        shard_path = shard_dir / f"{today}.jsonl"
        lines = shard_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_writes_provider_field(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 10
        event.output_tokens = 5
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.0
        event.num_turns = 0
        event.duration_ms = 0

        persist_token_record("slot", "opus", event, provider="claude_code")

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["provider"] == "claude_code"
        assert record["model"] == "opus"

    def test_provider_defaults_to_empty(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 10
        event.output_tokens = 5
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.0
        event.num_turns = 0
        event.duration_ms = 0

        persist_token_record("slot", "opus", event)

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["provider"] == ""

    def test_parse_token_history_aggregates_provider(self, tmp_path, monkeypatch):
        _patch_shard_layout(monkeypatch, tmp_path)
        for ev in (
            ("opencode", "claude-sonnet-4", 100, 50),
            ("opencode", "claude-haiku-3", 30, 10),
            ("claude_code", "opus", 200, 80),
        ):
            event = MagicMock()
            event.input_tokens = ev[2]
            event.output_tokens = ev[3]
            event.cache_creation_tokens = 0
            event.cache_read_tokens = 0
            event.cost_usd = 0.01
            event.num_turns = 1
            event.duration_ms = 100
            persist_token_record("slot", ev[1], event, provider=ev[0])

        # Cache may have been populated by the empty-shard parse triggered
        # before the writes; reset so the next read picks them up.
        _reset_token_cache()
        history = usage_mod._parse_token_history()

        assert sorted(history["providers"]) == ["claude_code", "opencode"]
        # Non-claude_code provider models keep their raw namespace verbatim.
        assert "claude-sonnet-4" in history["models"]
        # claude_code models are canonicalized so pre/post-migration records
        # aggregate into one bucket: bare 'opus' -> canonical 'opus-4.8-1m'.
        assert "opus-4.8-1m" in history["models"]
        assert "opus" not in history["models"]
        # The day entry should carry both providers + models sub-maps.
        day = history["daily_history"][0]
        assert "providers" in day
        assert "models" in day
        assert "opencode" in day["providers"]
        assert day["providers"]["opencode"]["input"] == 130

        # provider_models maps each provider to ONLY the models that have
        # actually appeared paired with it, so the frontend can cascade
        # the model dropdown safely.
        pm = history["provider_models"]
        assert sorted(pm["opencode"]) == ["claude-haiku-3", "claude-sonnet-4"]
        assert pm["claude_code"] == ["opus-4.8-1m"]  # canonicalized for claude_code
        # opus must NOT appear under opencode — that would be the bug.
        assert "opus" not in pm["opencode"]

        # Per-day cross-tab carries the true intersection bucket, so the
        # chart can render correct numbers when both filters are active.
        day_pm = day["provider_models"]
        assert day_pm["opencode"]["claude-sonnet-4"]["input"] == 100
        assert day_pm["opencode"]["claude-haiku-3"]["input"] == 30
        assert day_pm["claude_code"]["opus-4.8-1m"]["input"] == 200
        # Invalid pair (opencode + opus) is absent from the cross-tab.
        assert "opus" not in day_pm["opencode"]


# ── persist_token_record_async / _cached_parse_sessions (event-loop hygiene) ──


def _reset_sessions_cache():
    usage_mod._SESSIONS_CACHE = None
    usage_mod._SESSIONS_CACHE_TS = 0.0
    usage_mod._USAGE_SESSION_REFRESH_TASK = None


class TestPersistTokenRecordAsync:
    @pytest.mark.asyncio
    async def test_async_writes_same_record_as_sync(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        event = MagicMock()
        event.input_tokens = 7
        event.output_tokens = 3
        event.cache_creation_tokens = 0
        event.cache_read_tokens = 0
        event.cost_usd = 0.01
        event.num_turns = 1
        event.duration_ms = 42

        await persist_token_record_async("slot-a", "opus", event, provider="claude_code")

        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["slot"] == "slot-a"
        assert record["provider"] == "claude_code"
        assert record["input"] == 7

    @pytest.mark.asyncio
    async def test_async_no_crash_on_error(self, tmp_path, monkeypatch):
        event = MagicMock()
        event.input_tokens = 1
        event.output_tokens = 1
        shard_dir = _unwritable_shard_dir(monkeypatch, tmp_path)
        await persist_token_record_async("s", "m", event)  # must not raise
        assert not shard_dir.exists()
        assert (tmp_path / "blocker").is_file(), "the blocker file was replaced"


class TestCachedParseSessions:
    @pytest.mark.asyncio
    async def test_returns_complete_zero_shape_without_dir(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", tmp_path / "nope")
        result = await _cached_parse_sessions()
        assert result["total_sessions"] == 0
        assert result["today"] == {"sessions": 0, "messages": 0, "tool_calls": 0}
        assert result["daily_history"] == []

    @pytest.mark.asyncio
    async def test_offloads_and_caches(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)

        calls = {"n": 0}

        def _fake_parse():
            calls["n"] += 1
            return {"total_sessions": 1, "daily_history": []}

        monkeypatch.setattr(usage_mod, "_parse_sessions", _fake_parse)
        first = await _cached_parse_sessions()
        second = await _cached_parse_sessions()
        assert first == {"total_sessions": 1, "daily_history": []}
        assert second == first
        # Second call served from the TTL cache — parse ran once.
        assert calls["n"] == 1
        _reset_sessions_cache()

    @pytest.mark.asyncio
    async def test_completed_refresh_task_is_not_reused_after_cache_expiry(self, monkeypatch):
        _reset_sessions_cache()
        calls = {"n": 0}

        async def _fake_cached_parse():
            calls["n"] += 1
            return {"total_sessions": calls["n"], "daily_history": []}

        monkeypatch.setattr(usage_mod, "_cached_parse_sessions", _fake_cached_parse)
        first = usage_mod.asyncio.create_task(_fake_cached_parse())
        assert await first == {"total_sessions": 1, "daily_history": []}
        usage_mod._USAGE_SESSION_REFRESH_TASK = first
        usage_mod._SESSIONS_CACHE = {"total_sessions": 1, "daily_history": []}
        usage_mod._SESSIONS_CACHE_TS = time.time() - usage_mod._CACHE_TTL - 1

        result, refreshing = await usage_mod._usage_sessions_snapshot()

        assert result == {"total_sessions": 2, "daily_history": []}
        assert refreshing is False
        assert calls["n"] == 2
        assert usage_mod._USAGE_SESSION_REFRESH_TASK is None
        _reset_sessions_cache()

    @pytest.mark.asyncio
    async def test_error_result_not_cached(self, tmp_path, monkeypatch):
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr(usage_mod, "_parse_sessions", lambda: {"error": "boom"})
        result = await _cached_parse_sessions()
        assert result == {"error": "boom"}
        # An error result must not poison the cache (stays unpopulated).
        assert usage_mod._SESSIONS_CACHE is None
        _reset_sessions_cache()

    @pytest.mark.asyncio
    async def test_empty_result_is_cached(self, tmp_path, monkeypatch):
        """A valid-but-empty {} parse must be cached and served from the fast
        path (sentinel check), not re-parsed on every call."""
        _reset_sessions_cache()
        sessions_dir = tmp_path / "cli"
        sessions_dir.mkdir()
        monkeypatch.setattr(usage_mod, "_SESSIONS_DIR", sessions_dir)

        calls = {"n": 0}

        def _fake_parse():
            calls["n"] += 1
            return {}  # valid, but empty (dir present, no session files)

        monkeypatch.setattr(usage_mod, "_parse_sessions", _fake_parse)
        assert await _cached_parse_sessions() == {}
        assert await _cached_parse_sessions() == {}
        # Empty dict is cached (sentinel is None, so {} is a hit) — parse once.
        assert calls["n"] == 1
        _reset_sessions_cache()


class TestBuildTokenRecordCredits:
    """_build_token_record persists per-turn credits (kiro) and defaults to 0."""

    def test_record_includes_credits(self):
        from types import SimpleNamespace

        event = SimpleNamespace(
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            credits=1.19,
            num_turns=0,
            duration_ms=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        rec = usage_mod._build_token_record(
            "chat-1", "claude-opus-4-8", event, "acp", datetime.now(timezone.utc)
        )
        assert rec["_type"] == "tokens"
        assert rec["credits"] == pytest.approx(1.19)
        assert rec["slot"] == "chat-1"
        assert rec["provider"] == "acp"

    def test_record_defaults_credits_to_zero_when_absent(self):
        from types import SimpleNamespace

        event = SimpleNamespace(input_tokens=10, output_tokens=5)
        rec = usage_mod._build_token_record(
            "s", "m", event, "claude_code", datetime.now(timezone.utc)
        )
        assert rec["credits"] == 0.0

    def test_record_coerces_non_numeric_credits_to_zero(self):
        from types import SimpleNamespace

        event = SimpleNamespace(input_tokens=0, output_tokens=0, credits="not-a-number")
        rec = usage_mod._build_token_record("s", "m", event, "acp", datetime.now(timezone.utc))
        assert rec["credits"] == 0.0


# ── read_context_tokens / context-occupancy row fields ──────────────────────


class TestReadContextTokens:
    """read_context_tokens() reads provider occupancy accessors defensively."""

    def test_returns_real_values_from_provider(self):
        from types import SimpleNamespace

        # A fake provider exposing both public accessors (as AcpProvider /
        # AcpSessionProvider do) returns their exact values.
        provider = SimpleNamespace(
            context_used_tokens=lambda: 12345,
            context_window_tokens=lambda: 1_000_000,
        )
        assert read_context_tokens(provider) == (12345, 1_000_000)

    def test_returns_zero_when_accessors_absent(self):
        # A plain object with neither accessor — e.g. a non-ACP provider or a
        # bare test double — records (0, 0) rather than raising.
        assert read_context_tokens(object()) == (0, 0)

    def test_returns_zero_when_accessor_raises(self):
        class _Boom:
            def context_used_tokens(self) -> int:
                raise RuntimeError("boom")

            def context_window_tokens(self) -> int:
                return 1_000_000

        assert read_context_tokens(_Boom()) == (0, 0)

    def test_returns_zero_when_only_one_accessor_present(self):
        from types import SimpleNamespace

        # Both accessors are required; a partial provider still yields (0, 0).
        partial = SimpleNamespace(context_used_tokens=lambda: 500)
        assert read_context_tokens(partial) == (0, 0)


class TestBuildTokenRecordContextFields:
    """_build_token_record emits the additive surface/agent/context_* fields."""

    @staticmethod
    def _event():
        from types import SimpleNamespace

        return SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            credits=0.0,
            num_turns=1,
            duration_ms=0,
        )

    def test_emits_new_keys(self):
        rec = usage_mod._build_token_record(
            "chat-1",
            "claude-opus-4-8",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            surface="dashboard",
            agent="kirocrew",
            context_used=44_000,
            context_window=1_000_000,
        )
        assert rec["surface"] == "dashboard"
        assert rec["agent"] == "kirocrew"
        assert rec["context_used"] == 44_000
        assert rec["context_window"] == 1_000_000

    def test_coerces_non_numeric_context_to_zero(self):
        # Defensive int coercion keeps the record json.dumps-safe.
        rec = usage_mod._build_token_record(
            "s",
            "m",
            self._event(),
            "acp",
            datetime.now(timezone.utc),
            context_used="not-a-number",
            context_window=None,
        )
        assert rec["context_used"] == 0
        assert rec["context_window"] == 0
        json.dumps(rec)  # must not raise

    def test_stop_reason_recorded_from_event(self):
        """The row carries the turn's terminal stop reason so watchdog outcomes
        (tool_stall / stale_recover) can be joined against the free-form
        ``agent`` field retroactively — per-agent stall analysis happens HERE,
        not on OTel attrs (cardinality rule)."""
        from types import SimpleNamespace

        ev = SimpleNamespace(usage=None, stop_reason="error: tool stall")
        rec = usage_mod._build_token_record("chat-1", "m", ev, "acp", datetime.now(timezone.utc))
        assert rec["stop_reason"] == "error: tool stall"
        json.dumps(rec)  # must not raise

    def test_stop_reason_defaults_empty_and_tolerates_non_string(self):
        # A bare TurnUsage-shaped event (provider_last_turn_usage) has no
        # stop_reason; a non-string on a test double must not break json.dumps.
        rec = usage_mod._build_token_record(
            "chat-1", "m", self._event(), "acp", datetime.now(timezone.utc)
        )
        assert rec["stop_reason"] == ""

        from types import SimpleNamespace

        weird = SimpleNamespace(usage=None, stop_reason=1234)
        rec = usage_mod._build_token_record("chat-1", "m", weird, "acp", datetime.now(timezone.utc))
        assert rec["stop_reason"] == ""
        json.dumps(rec)

    def test_backcompat_defaults_when_no_kwargs(self):
        # Called positionally with no new kwargs (mirrors every legacy caller):
        # the original keys are unchanged and the new keys default to ""/0, so
        # old readers and old shards stay valid.
        rec = usage_mod._build_token_record(
            "chat-1", "opus", self._event(), "acp", datetime.now(timezone.utc)
        )
        for key in (
            "_type",
            "ts",
            "slot",
            "provider",
            "model",
            "input",
            "output",
            "cache_create",
            "cache_read",
            "cost",
            "credits",
            "turns",
            "duration_ms",
        ):
            assert key in rec
        assert rec["input"] == 10
        assert rec["provider"] == "acp"
        # New fields present with defaults.
        assert rec["surface"] == ""
        assert rec["agent"] == ""
        assert rec["context_used"] == 0
        assert rec["context_window"] == 0

    def test_persist_writes_new_fields_to_shard(self, tmp_path, monkeypatch):
        # End-to-end: the keyword-only params flow through persist_token_record
        # into the written JSONL row.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "opus",
            self._event(),
            provider="acp",
            surface="dashboard",
            agent="kirocrew",
            context_used=44_000,
            context_window=1_000_000,
        )
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        record = json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())
        assert record["surface"] == "dashboard"
        assert record["agent"] == "kirocrew"
        assert record["context_used"] == 44_000
        assert record["context_window"] == 1_000_000


class TestBuildTokenRecordCarriesNoInjectionBreakdown:
    """The row stopped carrying the per-turn injection breakdown and its phase."""

    @staticmethod
    def _event():
        return SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            credits=0.0,
            num_turns=1,
            duration_ms=0,
        )

    def test_the_row_no_longer_carries_the_injection_breakdown(self):
        """Both fields are gone, and the crew log is where the fact lives now.

        They were written every turn for exactly one reader, ``context_trace``, which
        is now served from the ``usage`` projection -- so the row was paying to store
        a breakdown nothing read. ``context/composed`` in the crew log carries the
        same blocks and the same phase, recorded by the same composer.
        """
        rec = usage_mod._build_token_record(
            "chat-1", "claude-opus-4-8", self._event(), "acp", datetime.now(timezone.utc)
        )
        assert "ctx_blocks" not in rec
        assert "phase" not in rec
        json.dumps(rec)

    def test_the_two_parameters_are_gone_from_the_builder(self):
        """A caller still passing them fails loudly rather than writing a dead field."""
        with pytest.raises(TypeError):
            usage_mod._build_token_record(
                "chat-1",
                "opus",
                self._event(),
                "acp",
                datetime.now(timezone.utc),
                ctx_blocks={"memory": 1200},
            )
        with pytest.raises(TypeError):
            usage_mod._build_token_record(
                "chat-1",
                "opus",
                self._event(),
                "acp",
                datetime.now(timezone.utc),
                phase="session_start",
            )


class _Inner:
    def __init__(self, model):
        self._model = model


class TestReadEffectiveModel:
    """read_effective_model: raw resolved model id, attribution only."""

    def test_reads_via_public_client_chain(self):
        src = type("P", (), {"client": _Inner("global.anthropic.claude-opus-4-8[1m]")})()
        assert read_effective_model(src) == "global.anthropic.claude-opus-4-8[1m]"

    def test_reads_via_private_client_chain(self):
        src = type("P", (), {"_client": _Inner("claude-opus-4.8")})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_reads_via_handle_chain(self):
        src = type("P", (), {"_handle": _Inner("claude-haiku-4.5")})()
        assert read_effective_model(src) == "claude-haiku-4.5"

    def test_reads_direct_model_attr(self):
        assert read_effective_model(_Inner("claude-sonnet-4.5")) == "claude-sonnet-4.5"

    def test_skips_auto_sentinel(self):
        # "auto" means "backend chooses" — not a model, so not attribution data.
        assert read_effective_model(_Inner("auto")) == ""

    def test_returns_empty_when_absent(self):
        assert read_effective_model(object()) == ""

    def test_returns_empty_when_accessor_raises(self):
        class Boom:
            @property
            def _client(self):
                raise RuntimeError("boom")

        assert read_effective_model(Boom()) == ""

    def test_prefers_first_populated_chain(self):
        src = type("P", (), {"client": _Inner(""), "_client": _Inner("claude-opus-4.8")})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_resolved_id_wins_over_model(self):
        # Mirrors AcpClient's own precedence: `_resolved_model_id or _model`.
        inner = _Inner("claude-opus-4.8")
        inner._resolved_model_id = "global.anthropic.claude-opus-4-8[1m]"
        src = type("P", (), {"_client": inner})()
        assert read_effective_model(src) == "global.anthropic.claude-opus-4-8[1m]"

    def test_default_model_turn_uses_resolved_id(self):
        # The common case: the caller asked for the default, so `_model` is left
        # at the "auto" sentinel while the backend's resolved id is the real one.
        inner = _Inner("auto")
        inner._resolved_model_id = "claude-opus-4.8"
        src = type("P", (), {"_handle": inner})()
        assert read_effective_model(src) == "claude-opus-4.8"

    def test_falls_back_to_model_when_resolved_id_blank(self):
        inner = _Inner("claude-haiku-4.5")
        inner._resolved_model_id = ""
        assert read_effective_model(inner) == "claude-haiku-4.5"

    def test_walks_nested_handle_two_levels_down(self):
        # The default Kiro turn shape: providers/acp_runtime_start.py assigns an
        # AcpSessionProvider to _client, which holds the handle on _handle, so
        # the resolved id sits at provider.client._handle.
        handle = _Inner("auto")
        handle._resolved_model_id = "claude-opus-4.8"
        mid = type("SessionProvider", (), {"_handle": handle})()
        outer = type("P", (), {"client": mid, "_client": mid})()
        assert read_effective_model(outer) == "claude-opus-4.8"

    def test_resolved_id_deep_beats_model_shallow(self):
        # A resolved id anywhere outranks a plain _model anywhere: _model may
        # still hold a pre-resolution request.
        handle = _Inner("")
        handle._resolved_model_id = "global.anthropic.claude-opus-4-8[1m]"
        outer = type("P", (), {"_model": "claude-opus-4.8", "_handle": handle})()
        assert read_effective_model(outer) == "global.anthropic.claude-opus-4-8[1m]"

    def test_self_referential_chain_terminates(self):
        node = type("Loop", (), {})()
        node._client = node
        node._model = "claude-opus-4.8"
        assert read_effective_model(node) == "claude-opus-4.8"


class TestReadTurnModel:
    """read_turn_model: display attribution — concrete id, `auto`, or blank."""

    def test_concrete_id_outranks_the_sentinel(self):
        # A resolved id anywhere in the chain wins even while an outer wrapper
        # still reports the Auto request, so a pinned turn never reads "auto".
        handle = type("Handle", (), {"_resolved_model_id": "claude-opus-4.8"})()
        provider = type("P", (), {"_model": "auto", "_handle": handle})()
        assert read_turn_model(provider) == "claude-opus-4.8"

    def test_auto_request_with_no_resolved_id_reports_auto(self):
        # The case read_effective_model collapses to "": the user chose Auto and
        # the backend disclosed no id. Reporting the choice is not guessing.
        assert read_turn_model(_Inner("auto")) == "auto"
        assert read_effective_model(_Inner("auto")) == ""

    def test_auto_sentinel_is_matched_case_and_space_insensitively(self):
        assert read_turn_model(_Inner("  AUTO ")) == "auto"

    def test_auto_found_deeper_in_the_chain(self):
        inner = type("Handle", (), {"_model": "auto"})()
        assert read_turn_model(type("P", (), {"_handle": inner})()) == "auto"

    def test_no_model_information_stays_blank(self):
        # Distinct from the Auto case: nothing is known, so nothing is claimed.
        assert read_turn_model(object()) == ""

    def test_never_raises_on_a_hostile_source(self):
        class Boom:
            @property
            def _model(self):
                raise RuntimeError("no")

        assert read_turn_model(Boom()) == ""


class TestReadEffectiveAgent:
    """The resolved agent, not the slot alias."""

    def test_reads_resolved_agent_off_client(self):
        inner = type("C", (), {"_agent": "kirocrew"})()
        assert read_effective_agent(inner) == "kirocrew"

    def test_walks_nested_handle(self):
        handle = type("H", (), {"_agent": "kirocrew-lite"})()
        mid = type("SessionProvider", (), {"_handle": handle})()
        outer = type("P", (), {"client": mid})()
        assert read_effective_agent(outer) == "kirocrew-lite"

    def test_blank_when_absent(self):
        assert read_effective_agent(object()) == ""

    def test_never_raises_on_exploding_attribute(self):
        class Boom:
            @property
            def _agent(self):
                raise RuntimeError("nope")

        assert read_effective_agent(Boom()) == ""

    def test_reads_agent_off_the_runtime(self):
        # The session-provider shape: the agent is held only by the spawned CLI
        # runtime (AcpRuntime._agent), reached via provider -> _handle -> _runtime.
        runtime = type("Runtime", (), {"_agent": "kirocrew"})()
        handle = type("Handle", (), {"_runtime": runtime})()
        provider = type("SessionProvider", (), {"_handle": handle})()
        assert read_effective_agent(provider) == "kirocrew"

    def test_session_model_outranks_runtime_model(self):
        # _runtime is walked last so its process-level --model argument cannot
        # outrank the session handle's own model.
        runtime = type("Runtime", (), {"_model": "claude-haiku-4.5"})()
        handle = type("Handle", (), {"_model": "claude-opus-4.8", "_runtime": runtime})()
        provider = type("SessionProvider", (), {"_handle": handle})()
        assert read_effective_model(provider) == "claude-opus-4.8"


class TestModelSourceFallback:
    """model_source fills `model` only when the caller resolved none."""

    @staticmethod
    def _event():
        # Not a real TurnUsage, so _build_token_record reads credits off the
        # event itself — enough to exercise the model fallback.
        return SimpleNamespace(credits=1.0)

    def _row(self, shard_dir):
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        return json.loads((shard_dir / f"{today}.jsonl").read_text(encoding="utf-8").strip())

    def test_fallback_fills_empty_model(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "",
            self._event(),
            provider="acp",
            surface="webhook",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-opus-4.8"

    def test_explicit_model_wins(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "claude-haiku-4.5",
            self._event(),
            provider="acp",
            surface="cron",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-haiku-4.5"

    def test_no_source_leaves_model_empty(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record("slot", "", self._event(), provider="acp", surface="webhook")
        assert self._row(shard_dir)["model"] == ""

    def test_unusable_source_leaves_model_empty(self, tmp_path, monkeypatch):
        # A test double with no model attr must not break the write.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot", "", self._event(), provider="acp", surface="webhook", model_source=object()
        )
        assert self._row(shard_dir)["model"] == ""

    def test_auto_sentinel_is_treated_as_unresolved(self, tmp_path, monkeypatch):
        # agent.model defaults to "auto" and the task runner forwards it verbatim.
        # "auto" is not a model, so the provider's resolved id must win.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "slot",
            "auto",
            self._event(),
            provider="acp",
            surface="task_runner",
            model_source=_Inner("claude-opus-4.8"),
        )
        assert self._row(shard_dir)["model"] == "claude-opus-4.8"

    def test_auto_without_resolvable_source_records_auto(self, tmp_path, monkeypatch):
        # `auto` records the explicit backend-selection mode even when the
        # backend does not disclose the concrete model for this completed turn.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record("slot", "auto", self._event(), provider="acp", surface="task_runner")
        assert self._row(shard_dir)["model"] == "auto"

    def test_auto_is_case_and_space_insensitive(self, tmp_path, monkeypatch):
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record("slot", "  AUTO ", self._event(), provider="acp", surface="cron")
        assert self._row(shard_dir)["model"] == "auto"

    def test_auto_source_fills_empty_model(self, tmp_path, monkeypatch):
        # Dashboard slots use an empty override for Auto while the live client
        # retains the request sentinel.
        shard_dir = _patch_shard_layout(monkeypatch, tmp_path)
        persist_token_record(
            "dashboard:auto",
            "",
            self._event(),
            provider="acp",
            surface="dashboard",
            model_source=_Inner("  AUTO "),
        )
        assert self._row(shard_dir)["model"] == "auto"


# ── model attribution through wrapper layers ────────────────────────────
#
# ``persist_token_record`` fills a blank caller-side model from ``model_source``
# by walking the provider/client/handle nesting with ``_wrapper_chain``. The
# model sits at the bottom of the ``_handle`` chain under wrapper layers whose
# sibling holders hold nothing, so the walk is depth-first with a visited set and
# only a runaway guard; a blank model on a billed row would otherwise land the
# turn's credits in the ``unknown`` bucket. A billed row that still has no model
# is logged at the write site.


class _Node:
    """A bare wrapper. Attributes are exactly what the constructor sets."""

    def __init__(self, name: str, **holders: object) -> None:
        self._name = name
        for holder, inner in holders.items():
            setattr(self, holder, inner)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{self._name}>"


def _documented_shape(extra_layers: int, *, model: str = "auto") -> tuple[object, object]:
    """The nesting ``_wrapper_chain``'s docstring documents, plus wrappers.

    ``AcpProvider`` -> ``client``/``_client`` -> ``AcpSessionProvider`` ->
    ``_handle`` -> ``AcpSessionHandle`` (holds ``_model``) -> ``_runtime``, then
    ``extra_layers`` wrappers stacked on top, each carrying the same four holders
    the walk follows, three of them siblings that hold nothing. Returns
    ``(outermost, handle)``.
    """
    runtime = _Node("runtime")
    handle = _Node("handle", _runtime=runtime)
    handle._model = model
    session_provider = _Node("session_provider", _handle=handle, _runtime=runtime)
    node: object = _Node("provider", client=session_provider, _client=session_provider)
    for i in range(extra_layers):
        node = _Node(
            f"wrapper{i}",
            client=_Node(f"a{i}"),
            _client=_Node(f"b{i}"),
            _handle=node,
            _runtime=_Node(f"c{i}"),
        )
    return node, handle


class TestWalkReachesTheModelRegardlessOfDepth:
    """The wrapper-depth failure, pinned against the real walk."""

    @pytest.mark.parametrize("extra_layers", [0, 1, 2, 3, 4])
    def test_documented_shape_resolves_auto_through_every_layer_count(self, extra_layers):
        source, handle = _documented_shape(extra_layers)
        assert any(
            node is handle for node in _wrapper_chain(source)
        ), f"the handle must be in the chain at {extra_layers} extra layer(s)"
        assert _resolve_model("", source) == "auto"

    @pytest.mark.parametrize("extra_layers", [3, 4, 12])
    def test_concrete_model_survives_deep_wrapping(self, extra_layers):
        source, _handle = _documented_shape(extra_layers, model="claude-opus-5")
        assert read_effective_model(source) == "claude-opus-5"
        assert read_turn_model(source) == "claude-opus-5"
        assert _resolve_model("", source) == "claude-opus-5"

    def test_resolved_id_deep_in_the_chain_still_beats_a_shallow_model(self):
        # Two-pass precedence is unchanged by the traversal order: a resolved id
        # ANYWHERE outranks a plain _model anywhere.
        source, handle = _documented_shape(4, model="auto")
        handle._resolved_model_id = "global.anthropic.claude-opus-5[1m]"
        outer = _Node("outer", _handle=source)
        outer._model = "claude-opus-5"
        assert read_effective_model(outer) == "global.anthropic.claude-opus-5[1m]"

    def test_agent_is_read_off_the_runtime_below_deep_wrapping(self):
        source, handle = _documented_shape(5)
        handle._runtime._agent = "kirocrew-lite"
        assert read_effective_agent(source) == "kirocrew-lite"

    def test_session_model_still_outranks_the_runtime_model_when_wrapped(self):
        # _runtime is walked after the whole _handle subtree, so the process-level
        # --model argument never outranks the session handle's own model, no
        # matter how many wrappers sit above both.
        source, handle = _documented_shape(4, model="claude-opus-5")
        handle._runtime._model = "claude-haiku-4.5"
        assert read_effective_model(source) == "claude-opus-5"

    def test_runtime_reached_from_a_shallow_wrapper_does_not_preempt_the_handle(self):
        # A wrapper holding both a runtime and a deeper session provider must
        # follow _handle to the bottom before it reads the runtime.
        runtime = _Node("runtime")
        runtime._model = "process-level-arg"
        handle = _Node("handle", _runtime=runtime)
        handle._model = "claude-opus-5"
        session_provider = _Node("session_provider", _handle=handle)
        wrapper = _Node("wrapper", _client=session_provider, _runtime=runtime)
        assert read_effective_model(wrapper) == "claude-opus-5"


def _default_model_session(extra_layers: int, *, resolved: str = "auto") -> object:
    """The chain a kiro session on the default model really produces.

    Built on the real classes, with no constructor run: the start path skips
    ``set_model`` for the default model, so the handle's ``_model`` stays
    ``""``, and ``session/new`` stores the backend's ``currentModelId`` as
    ``_resolved_model_id``. ``extra_layers`` wrappers sit on top, as in
    :func:`_documented_shape`.
    """
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    runtime = object.__new__(AcpRuntime)
    handle = object.__new__(AcpSessionHandle)
    handle._runtime = runtime
    handle._model = ""
    handle._resolved_model_id = resolved
    session_provider = object.__new__(AcpSessionProvider)
    session_provider._handle = handle
    session_provider._runtime = runtime
    provider = object.__new__(AcpProvider)
    provider._client = session_provider
    node: object = provider
    for i in range(extra_layers):
        node = _Node(
            f"wrapper{i}",
            client=_Node(f"a{i}"),
            _client=_Node(f"b{i}"),
            _handle=node,
            _runtime=_Node(f"c{i}"),
        )
    return node


class TestBackendReportedAutoIsAttributed:
    """A default-model session's rows and footer read ``auto``, not blank."""

    @pytest.mark.parametrize("extra_layers", [0, 1, 4])
    def test_backend_reported_auto_resolves_auto(self, extra_layers):
        source = _default_model_session(extra_layers)
        assert read_effective_model(source) == "", "auto is never a concrete id"
        assert _resolve_model("", source) == "auto"

    @pytest.mark.parametrize("resolved", ["AUTO", " auto "])
    def test_backend_reported_auto_is_case_and_space_insensitive(self, resolved):
        assert _resolve_model("", _default_model_session(1, resolved=resolved)) == "auto"

    def test_footer_reads_auto_for_the_default_model_session(self):
        assert read_turn_model(_default_model_session(0)) == "auto"

    @pytest.mark.parametrize("extra_layers", [0, 4])
    def test_blank_request_and_blank_resolved_id_stay_blank(self, extra_layers):
        source = _default_model_session(extra_layers, resolved="")
        assert read_turn_model(source) == ""
        assert _resolve_model("", source) == ""

    def test_concrete_resolved_id_still_wins_over_auto(self):
        source = _default_model_session(2, resolved="claude-opus-5")
        outer = _Node("outer", _handle=source)
        outer._resolved_model_id = "auto"
        assert read_turn_model(outer) == "claude-opus-5"
        assert _resolve_model("", outer) == "claude-opus-5"


class TestWalkTerminates:
    """Cycle safety and the runaway guard."""

    def test_self_referential_wrapper_terminates(self):
        node = _Node("loop")
        node._client = node
        node.client = node
        node._model = "claude-opus-5"
        assert [n for n in _wrapper_chain(node)] == [node]
        assert read_effective_model(node) == "claude-opus-5"

    def test_mutual_cycle_terminates_with_each_node_once(self):
        a = _Node("a")
        b = _Node("b", _handle=a)
        a._client = b
        a._runtime = b
        chain = _wrapper_chain(a)
        assert len(chain) == 2
        assert chain[0] is a and chain[1] is b

    def test_shared_runtime_is_visited_once(self):
        # Both the handle and the session provider hold the SAME runtime
        # (session_handle_class.py / session_provider.py); it must not be double-counted.
        source, handle = _documented_shape(2)
        chain = _wrapper_chain(source)
        assert sum(1 for n in chain if n is handle._runtime) == 1
        assert len(chain) == len({id(n) for n in chain})

    def test_attribute_synthesizing_source_hits_the_guard_and_reports_blank(self):
        # A MagicMock answers every getattr with a fresh child, so without the
        # guard the walk would never end. It stops, and claims no model.
        source = MagicMock()
        chain = _wrapper_chain(source)
        assert len(chain) == usage_mod._WRAPPER_CHAIN_MAX_NODES
        assert read_effective_model(source) == ""
        assert read_turn_model(source) == ""

    def test_guard_is_far_above_the_documented_shape(self):
        # The guard is a runaway stop, not a depth limit: the documented shape
        # with four extra layers uses a fraction of it.
        source, _handle = _documented_shape(4)
        assert len(_wrapper_chain(source)) < usage_mod._WRAPPER_CHAIN_MAX_NODES // 2


class TestBlankModelBilledRowIsLoud:
    """A non-zero charge with no model cannot be persisted silently."""

    @staticmethod
    def _shard_dir(monkeypatch, tmp_path):
        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        return shard_dir

    @staticmethod
    def _rows(shard_dir):
        return [
            json.loads(line)
            for shard in sorted(shard_dir.glob("*.jsonl"))
            for line in shard.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _attribution_warnings(caplog):
        return [
            r
            for r in caplog.records
            if r.name == usage_mod.__name__
            and r.levelno == logging.WARNING
            and "model attribution failed" in r.getMessage()
        ]

    def test_billed_row_with_blank_model_is_persisted_and_logged(
        self, tmp_path, monkeypatch, caplog
    ):
        shard_dir = self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "chat-synthetic",
                "",
                SimpleNamespace(credits=12.0),
                provider="acp",
                surface="dashboard",
                model_source=None,
            )
        rows = self._rows(shard_dir)
        assert len(rows) == 1 and rows[0]["credits"] == 12.0, rows
        assert rows[0]["model"] == "", "the charge is real; the row must still be written"
        warnings = self._attribution_warnings(caplog)
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        message = warnings[0].getMessage()
        assert "slot=chat-synthetic" in message
        assert "surface=dashboard" in message
        assert "provider=acp" in message
        assert "credits=12.0" in message

    def test_cost_only_billing_is_also_loud(self, tmp_path, monkeypatch, caplog):
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "slot", "", SimpleNamespace(cost_usd=0.03), provider="claude_code", surface="cron"
            )
        assert len(self._attribution_warnings(caplog)) == 1

    @pytest.mark.parametrize(
        "usage",
        [
            SimpleNamespace(input_tokens=1200),
            SimpleNamespace(output_tokens=40),
            SimpleNamespace(cache_creation_tokens=900),
            SimpleNamespace(cache_read_tokens=3000),
        ],
        ids=["input", "output", "cache_create", "cache_read"],
    )
    def test_token_only_billing_is_also_loud(self, tmp_path, monkeypatch, caplog, usage):
        # The claude seam bills in tokens with cost still 0 on most turns and
        # credits 0 off the kiro path: every dimension usage_has_billing
        # accepts must count here too, or exactly those rows stay silent.
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record("slot", "", usage, provider="claude_code", surface="dashboard")
        warnings = self._attribution_warnings(caplog)
        assert len(warnings) == 1
        assert "tokens=" in warnings[0].getMessage()

    def test_record_predicate_agrees_with_usage_has_billing_on_every_dimension(self):
        # `_record_is_billed` is a copy of `usage_has_billing` spelled in record
        # field names. This pins the copy to the original across EVERY TurnUsage
        # field: a dimension the original bills in must bill here too, and one
        # it ignores (num_turns, duration_ms) must stay free here.
        from dataclasses import fields

        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.llm_helpers import usage_has_billing

        now = datetime.now().astimezone()
        for field in fields(TurnUsage):
            usage = TurnUsage(**{field.name: 1})
            record = usage_mod._build_token_record(
                "slot", "", SimpleNamespace(usage=usage), "acp", now
            )
            assert usage_mod._record_is_billed(record) == usage_has_billing(usage), field.name

    def test_attributed_row_is_quiet(self, tmp_path, monkeypatch, caplog):
        self._shard_dir(monkeypatch, tmp_path)
        source, _handle = _documented_shape(4, model="claude-opus-5")
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record(
                "slot", "", SimpleNamespace(credits=12.0), provider="acp", model_source=source
            )
        assert self._attribution_warnings(caplog) == []

    @pytest.mark.parametrize(
        "credits",
        [
            # A timeout that billed nothing is not an attribution failure.
            pytest.param(0.0, id="test_free_row_with_blank_model_is_quiet"),
            # A NaN credit is a corrupt measurement (the sanitizer's warning
            # names it), not a charge to attribute.
            pytest.param(math.nan, id="test_non_finite_charge_is_not_treated_as_billing"),
        ],
    )
    def test_unbilled_row_with_blank_model_is_quiet(self, tmp_path, monkeypatch, caplog, credits):
        self._shard_dir(monkeypatch, tmp_path)
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            persist_token_record("slot", "", SimpleNamespace(credits=credits), provider="acp")
        assert self._attribution_warnings(caplog) == []

    def test_write_site_covers_every_persist_path(self, tmp_path, monkeypatch, caplog):
        # The check lives on the shared write, so the sync and async variants
        # cannot drift apart.
        self._shard_dir(monkeypatch, tmp_path)
        record = usage_mod._build_token_record(
            "slot", "", SimpleNamespace(credits=3.0), "acp", datetime.now().astimezone()
        )
        with caplog.at_level(logging.WARNING, logger=usage_mod.__name__):
            usage_mod._write_token_record(record, datetime.now().astimezone())
        assert len(self._attribution_warnings(caplog)) == 1

    def test_deep_wrapping_end_to_end_names_the_model(self, tmp_path, monkeypatch):
        # Model "" from the caller, four extra wrapper layers on model_source,
        # a real charge: the row names the model.
        shard_dir = self._shard_dir(monkeypatch, tmp_path)
        source, _handle = _documented_shape(4, model="gpt-5.6-luna")
        persist_token_record(
            "chat-1", "", SimpleNamespace(credits=7.5), provider="acp", model_source=source
        )
        rows = self._rows(shard_dir)
        assert rows[0]["model"] == "gpt-5.6-luna"
        assert rows[0]["credits"] == 7.5


class TestBillingStatsHolderWalkReachesTheRunner:
    """The sibling walk in ``llm_helpers`` follows the same discipline."""

    @staticmethod
    def _deep_provider(extra_layers: int) -> tuple[object, object]:
        # The documented billing shape: acp provider -> _client -> session
        # provider -> _handle -> runner (holds last_prompt_stats), plus wrappers
        # each carrying every holder the walk follows, siblings holding nothing.
        runner = _Node("runner")
        runner.last_prompt_stats = SimpleNamespace(credits=4.0)
        session_provider = _Node("session_provider", _handle=runner)
        node: object = _Node("provider", _client=session_provider)
        for i in range(extra_layers):
            node = _Node(
                f"wrapper{i}",
                _client=node,
                _handle=_Node(f"h{i}"),
                _sess=_Node(f"s{i}"),
                provider=_Node(f"p{i}"),
            )
        return node, runner

    @pytest.mark.parametrize("extra_layers", [0, 2, 4, 12])
    def test_runner_is_reached_through_every_layer_count(self, extra_layers):
        from kiro_crew.llm_helpers import _billing_stat_holders, _billing_stats

        source, runner = self._deep_provider(extra_layers)
        assert any(node is runner for node in _billing_stat_holders(source))
        assert _billing_stats(source) is runner.last_prompt_stats

    def test_nearest_wrapper_is_still_first(self):
        from kiro_crew.llm_helpers import _billing_stat_holders

        source, _runner = self._deep_provider(3)
        assert _billing_stat_holders(source)[0] is source

    def test_cycle_terminates_with_each_node_once(self):
        from kiro_crew.llm_helpers import _billing_stat_holders

        a = _Node("a")
        b = _Node("b", provider=a)
        a._sess = b
        a._client = b
        chain = _billing_stat_holders(a)
        assert len(chain) == 2 and chain[0] is a and chain[1] is b

    def test_attribute_synthesizing_source_hits_the_guard(self):
        from kiro_crew import llm_helpers

        chain = llm_helpers._billing_stat_holders(MagicMock())
        assert len(chain) == llm_helpers._WRAPPER_WALK_MAX_NODES
