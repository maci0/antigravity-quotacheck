"""Tests for quotacheck."""

import io
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from rich.console import Console

import quotacheck


# --- fixtures ---

SAMPLE_MODELS = {
    "gemini-3-pro-high": {
        "displayName": "Gemini 3 Pro (High)",
        "model": "gemini-3-pro-high",
        "modelProvider": "MODEL_PROVIDER_GOOGLE",
        "quotaInfo": {"remainingFraction": 0.85, "resetTime": "2026-02-25T07:00:00Z"},
        "recommended": True,
    },
    "claude-sonnet-4-6": {
        "displayName": "Claude Sonnet 4.6 (Thinking)",
        "model": "claude-sonnet-4-6",
        "modelProvider": "MODEL_PROVIDER_ANTHROPIC",
        "quotaInfo": {"remainingFraction": 0.15, "resetTime": "2026-02-25T03:00:00Z"},
        "recommended": True,
    },
    "gpt-oss-120b-medium": {
        "displayName": "GPT-OSS 120B (Medium)",
        "model": "gpt-oss-120b-medium",
        "modelProvider": "MODEL_PROVIDER_OPENAI",
        "quotaInfo": {"remainingFraction": 0.0, "resetTime": "2026-02-25T01:00:00Z"},
        "recommended": True,
    },
    "tab_flash_lite": {
        "displayName": "Tab Flash Lite",
        "model": "tab_flash_lite",
        "modelProvider": "MODEL_PROVIDER_GOOGLE",
        "quotaInfo": {"remainingFraction": 1.0},
    },
    "chat_20706": {
        "displayName": "Chat Internal",
        "model": "chat_20706",
        "modelProvider": "",
        "quotaInfo": {"remainingFraction": 1.0},
    },
    "unknown-model": {
        "displayName": "Some Unknown Model",
        "model": "unknown-model",
        "modelProvider": "MODEL_PROVIDER_UNKNOWN",
        "quotaInfo": {"remainingFraction": 0.5},
    },
}

SAMPLE_ACCOUNTS_DATA = {
    "version": 4,
    "accounts": [
        {
            "email": "test@example.com",
            "refreshToken": "1//fake-refresh-token",
            "enabled": True,
        },
        {
            "email": "other@example.com",
            "refreshToken": "1//other-token",
            "enabled": True,
        },
    ],
    "activeIndex": 0,
}


def _render_to_text(renderable) -> str:
    """Render a Rich object to plain text."""
    c = Console(file=io.StringIO(), width=120, force_terminal=True)
    c.print(renderable)
    return c.file.getvalue()


# --- format_reset_time ---


class TestFormatResetTime:
    def test_none(self):
        assert quotacheck.format_reset_time(None) == "-"

    def test_empty_string(self):
        assert quotacheck.format_reset_time("") == "-"

    def test_past_time(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert quotacheck.format_reset_time(past) == "now"

    def test_future_hours_and_minutes(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)).isoformat()
        result = quotacheck.format_reset_time(future)
        assert result.startswith("3h ")
        assert "m" in result

    def test_future_minutes_only(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
        result = quotacheck.format_reset_time(future)
        assert "h" not in result
        assert result.endswith("m")

    def test_z_suffix(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        result = quotacheck.format_reset_time(future)
        assert "h" in result

    def test_invalid_string(self):
        assert quotacheck.format_reset_time("not-a-date") == "?"


# --- quota_color ---


class TestQuotaColor:
    def test_high(self):
        assert quotacheck.quota_color(1.0) == "green"
        assert quotacheck.quota_color(0.5) == "green"

    def test_medium(self):
        assert quotacheck.quota_color(0.49) == "yellow"
        assert quotacheck.quota_color(0.2) == "yellow"

    def test_low(self):
        assert quotacheck.quota_color(0.19) == "red"
        assert quotacheck.quota_color(0.0) == "red"


# --- build_bar ---


class TestBuildBar:
    def test_full(self):
        bar = quotacheck.build_bar(1.0)
        text = bar.plain
        assert "100%" in text
        assert "░" not in text

    def test_empty(self):
        bar = quotacheck.build_bar(0.0)
        text = bar.plain
        assert "0%" in text
        assert "█" not in text

    def test_half(self):
        bar = quotacheck.build_bar(0.5, width=10)
        text = bar.plain
        assert "50%" in text
        assert "█" in text
        assert "░" in text

    def test_clamp_above_one(self):
        bar = quotacheck.build_bar(1.5)
        text = bar.plain
        assert "100%" in text

    def test_clamp_below_zero(self):
        bar = quotacheck.build_bar(-0.5)
        text = bar.plain
        assert "0%" in text


# --- model_family ---


class TestModelFamily:
    def test_gemini_by_provider(self):
        assert quotacheck.model_family("x", {"displayName": "X", "modelProvider": "MODEL_PROVIDER_GOOGLE"}) == "Gemini"

    def test_gemini_by_key(self):
        assert quotacheck.model_family("gemini-3-pro", {"displayName": "G"}) == "Gemini"

    def test_claude_by_provider(self):
        assert quotacheck.model_family("x", {"displayName": "X", "modelProvider": "MODEL_PROVIDER_ANTHROPIC"}) == "Claude"

    def test_claude_by_key(self):
        assert quotacheck.model_family("claude-sonnet", {"displayName": "C"}) == "Claude"

    def test_gpt_by_provider(self):
        assert quotacheck.model_family("x", {"displayName": "X", "modelProvider": "MODEL_PROVIDER_OPENAI"}) == "GPT"

    def test_gpt_by_key(self):
        assert quotacheck.model_family("gpt-oss-120b", {"displayName": "G"}) == "GPT"

    def test_other(self):
        assert quotacheck.model_family("mystery", {"displayName": "M", "modelProvider": "UNKNOWN"}) == "Other"

    def test_hidden_tab_prefix(self):
        assert quotacheck.model_family("tab_flash", {"displayName": "T"}) is None

    def test_hidden_chat_prefix(self):
        assert quotacheck.model_family("chat_123", {"displayName": "C"}) is None

    def test_no_display_name(self):
        assert quotacheck.model_family("gemini-x", {}) is None
        assert quotacheck.model_family("gemini-x", {"displayName": None}) is None


# --- _model_name_text ---


class TestModelNameText:
    def test_recommended(self):
        text = quotacheck._model_name_text("Model X", None, True)
        plain = text.plain
        assert plain.startswith("* ")
        assert "Model X" in plain

    def test_not_recommended(self):
        text = quotacheck._model_name_text("Model X", None, False)
        plain = text.plain
        assert plain.startswith("  ")

    def test_with_tag(self):
        text = quotacheck._model_name_text("Model X", "New", False)
        assert "(New)" in text.plain

    def test_markup_safe(self):
        """Ensure brackets in names don't break rendering."""
        text = quotacheck._model_name_text("Model [v2]", "[beta]", True)
        assert "[v2]" in text.plain
        assert "([beta])" in text.plain


# --- build_dashboard ---


class TestBuildDashboard:
    def test_groups_and_filters(self):
        panel = quotacheck.build_dashboard("test@example.com", SAMPLE_MODELS)
        text = _render_to_text(panel)
        # Visible models appear
        assert "Gemini 3 Pro (High)" in text
        assert "Claude Sonnet 4.6" in text
        assert "GPT-OSS 120B" in text
        assert "Some Unknown Model" in text
        # Hidden models do not appear
        assert "Tab Flash Lite" not in text
        assert "Chat Internal" not in text

    def test_group_order(self):
        """Gemini before Claude before GPT before Other."""
        panel = quotacheck.build_dashboard("x@y.com", SAMPLE_MODELS)
        text = _render_to_text(panel)
        gemini_pos = text.index("Gemini 3 Pro")
        claude_pos = text.index("Claude Sonnet")
        gpt_pos = text.index("GPT-OSS")
        other_pos = text.index("Some Unknown")
        assert gemini_pos < claude_pos < gpt_pos < other_pos

    def test_empty_models(self):
        panel = quotacheck.build_dashboard("x@y.com", {})
        text = _render_to_text(panel)
        # Should render without crashing, contains the email
        assert "x@y.com" in text

    def test_no_quota_info(self):
        models = {
            "test-model": {
                "displayName": "Test",
                "modelProvider": "MODEL_PROVIDER_GOOGLE",
            }
        }
        text = _render_to_text(quotacheck.build_dashboard("x@y.com", models))
        assert "Test" in text
        assert "0%" in text

    def test_quota_values_displayed(self):
        models = {
            "m1": {
                "displayName": "Half Model",
                "modelProvider": "MODEL_PROVIDER_GOOGLE",
                "quotaInfo": {"remainingFraction": 0.5},
            }
        }
        text = _render_to_text(quotacheck.build_dashboard("x@y.com", models))
        assert "50%" in text


# --- _search_paths ---


class TestSearchPaths:
    def test_default_no_xdg(self):
        with patch.dict(os.environ, {"HOME": os.environ.get("HOME", "/tmp")}, clear=True):
            paths = quotacheck._search_paths()
            assert len(paths) == 2
            assert ".config" in str(paths[0])
            assert ".local/share" in str(paths[1])

    def test_with_custom_xdg(self, tmp_path):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path / "custom")}):
            paths = quotacheck._search_paths()
            assert len(paths) == 3
            assert "custom" in str(paths[1])

    def test_xdg_same_as_default(self):
        default = str(Path.home() / ".local" / "share")
        with patch.dict(os.environ, {"XDG_DATA_HOME": default}):
            paths = quotacheck._search_paths()
            # Should NOT duplicate the default path
            assert len(paths) == 2

    def test_xdg_empty_string(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": ""}):
            paths = quotacheck._search_paths()
            # Empty string should be treated as unset
            assert len(paths) == 2


# --- find_accounts_file ---


class TestFindAccountsFile:
    def test_returns_first_existing(self, tmp_path):
        f = tmp_path / "accounts.json"
        f.write_text("{}")
        with patch.object(quotacheck, "_search_paths", return_value=[f]):
            assert quotacheck.find_accounts_file() == f

    def test_returns_none_when_missing(self, tmp_path):
        missing = tmp_path / "nope.json"
        with patch.object(quotacheck, "_search_paths", return_value=[missing]):
            assert quotacheck.find_accounts_file() is None

    def test_skips_missing_returns_second(self, tmp_path):
        missing = tmp_path / "nope.json"
        existing = tmp_path / "yes.json"
        existing.write_text("{}")
        with patch.object(quotacheck, "_search_paths", return_value=[missing, existing]):
            assert quotacheck.find_accounts_file() == existing


# --- load_accounts ---


class TestLoadAccounts:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "accounts.json"
        f.write_text(json.dumps(SAMPLE_ACCOUNTS_DATA))
        accounts = quotacheck.load_accounts(f)
        assert len(accounts) == 2
        assert accounts[0]["email"] == "test@example.com"

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "accounts.json"
        f.write_text("not json")
        with pytest.raises(SystemExit):
            quotacheck.load_accounts(f)

    def test_missing_accounts_key(self, tmp_path):
        f = tmp_path / "accounts.json"
        f.write_text("{}")
        assert quotacheck.load_accounts(f) == []


# --- token caching ---


class TestTokenCache:
    def setup_method(self):
        quotacheck.invalidate_token_cache()

    def test_invalidate(self):
        quotacheck._cached_token = "old"
        quotacheck._cached_token_expiry = time.monotonic() + 9999
        quotacheck.invalidate_token_cache()
        assert quotacheck._cached_token is None
        assert quotacheck._cached_token_expiry == 0

    @patch.object(quotacheck.session, "post")
    def test_caches_token(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok123", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        t1 = quotacheck.get_access_token("refresh")
        t2 = quotacheck.get_access_token("refresh")
        assert t1 == t2 == "tok123"
        # Should only have called the API once
        assert mock_post.call_count == 1

    @patch.object(quotacheck.session, "post")
    def test_refreshes_expired_token(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "new_tok", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        # Set an already-expired cache
        quotacheck._cached_token = "old_tok"
        quotacheck._cached_token_expiry = time.monotonic() - 1

        token = quotacheck.get_access_token("refresh")
        assert token == "new_tok"
        assert mock_post.call_count == 1


# --- fetch_models ---


class TestFetchModels:
    def setup_method(self):
        quotacheck.invalidate_token_cache()

    @patch.object(quotacheck.session, "post")
    def test_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": SAMPLE_MODELS}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = quotacheck.fetch_models("tok")
        assert "models" in result

    @patch.object(quotacheck.session, "post")
    def test_401_invalidates_cache(self, mock_post):
        quotacheck._cached_token = "stale"
        quotacheck._cached_token_expiry = time.monotonic() + 9999

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = Exception("401")
        mock_post.return_value = mock_resp

        with pytest.raises(Exception, match="401"):
            quotacheck.fetch_models("stale")
        assert quotacheck._cached_token is None
