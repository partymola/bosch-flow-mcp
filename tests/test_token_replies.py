"""What a token reply or a credential file can hold that a parser or a save chokes on."""

import io
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import bosch_flow_mcp.auth as auth_module
from bosch_flow_mcp import api
from bosch_flow_mcp.config import CLIENT_ID
from bosch_flow_mcp.tools.sync_tools import run_sync

# Unclosed, so it raises at parse and nothing ever holds a deep value.
DEEP_BODY = b"[" * 200_000


def _answer(body):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


def _now():
    return datetime.now(timezone.utc).timestamp()


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "bosch_tokens.json"
    path.write_text(
        json.dumps(
            {
                "access_token": "tok-old",
                "refresh_token": "ref-old",
                "expiry": 0,
                "client_id": CLIENT_ID,
            }
        )
    )
    monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
    return path


GOOD_ANSWER = json.dumps(
    {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 7200}
).encode()


def test_the_deep_body_really_raises_recursion_error():
    """If a parser accepts this depth, the tests below stop proving anything."""
    with pytest.raises(RecursionError):
        json.loads(DEEP_BODY)


class TestABodyNestedTooDeeply:
    def test_a_refresh_answered_with_one_is_an_unreadable_response(self, token_file):
        with (
            patch("urllib.request.urlopen", lambda *a, **k: _answer(DEEP_BODY)),
            pytest.raises(auth_module.RefreshNetworkError, match="unreadable response"),
        ):
            auth_module.refresh_token()
        assert json.loads(token_file.read_text())["refresh_token"] == "ref-old"

    def test_a_token_file_holding_one_is_a_refusal(self, token_file):
        token_file.write_bytes(DEEP_BODY)
        with pytest.raises(auth_module.TokenRefused, match="unreadable"):
            auth_module.refresh_token()

    def test_a_file_that_becomes_one_during_a_refresh_is_saved_over(self, token_file):
        """Otherwise the refresh token just issued is lost."""

        def urlopen(request, timeout=None):
            token_file.write_bytes(DEEP_BODY)
            return _answer(GOOD_ANSWER)

        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert json.loads(token_file.read_text())["refresh_token"] == "ref-new"

    def test_a_refusal_while_the_file_holds_one_is_not_a_refusal(self, token_file):
        def urlopen(request, timeout=None):
            token_file.write_bytes(DEEP_BODY)
            raise urllib.error.HTTPError("https://example.invalid", 400, "no", {}, io.BytesIO(b""))

        with (
            patch("urllib.request.urlopen", urlopen),
            pytest.raises(auth_module.RefreshNetworkError, match="could not be read"),
        ):
            auth_module.refresh_token()

    def test_a_credential_file_holding_one_still_routes(self, token_file, tmp_path, monkeypatch):
        config = tmp_path / "bosch_config.json"
        config.write_bytes(DEEP_BODY)
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config)
        token_file.write_bytes(DEEP_BODY)
        assert auth_module.current_client_id() == CLIENT_ID
        assert auth_module.token_is_euda() is False

    def test_a_data_request_answered_with_one_does_not_stop_a_sync(self, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "refresh_token", lambda: "tok")
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        with patch("urllib.request.urlopen", lambda *a, **k: _answer(DEEP_BODY)):
            results = run_sync(["bikes", "service"])
        assert results["bikes"]["status"] == "error"
        assert "RecursionError" in results["bikes"]["message"]
        assert results["service"]["status"] == "unavailable"

    async def test_a_live_read_answered_with_one_becomes_a_tool_result(self, monkeypatch):
        from bosch_flow_mcp.tools.activity_tools import bosch_get_activities

        monkeypatch.setattr(api, "refresh_token", lambda: "tok")
        with patch("urllib.request.urlopen", lambda *a, **k: _answer(DEEP_BODY)):
            result = json.loads(await bosch_get_activities())
        assert result == {"error": "Unexpected error (RecursionError)."}


class TestOnlyWhatWasCheckedIsSaved:
    """A field copied from a reply unchecked can be anything JSON can hold,
    and nothing reads the token type."""

    FIELDS = {"access_token", "refresh_token", "expiry", "client_id"}

    def test_a_refresh_saves_only_the_fields_it_checked(self, token_file):
        body = json.dumps(
            {"access_token": "tok-new", "refresh_token": "ref-new", "token_type": "REPLY-TEXT"}
        ).encode()
        with patch("urllib.request.urlopen", lambda *a, **k: _answer(body)):
            auth_module.refresh_token()
        assert set(json.loads(token_file.read_text())) == self.FIELDS

    def test_a_sign_in_saves_only_the_fields_it_checked_and_prints_none(self, token_file, capsys):
        body = json.dumps(
            {"access_token": "tok-new", "refresh_token": "ref-new", "token_type": "REPLY-TEXT"}
        ).encode()
        with patch("urllib.request.urlopen", lambda *a, **k: _answer(body)):
            auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")
        assert set(json.loads(token_file.read_text())) == self.FIELDS
        captured = capsys.readouterr()
        assert "Auth complete" in captured.out
        for reply_value in ("REPLY-TEXT", "tok-new", "ref-new"):
            assert reply_value not in captured.out + captured.err


class TestResiduesThatStayOpen:
    def test_a_sign_in_saved_with_an_expiry_of_0_during_a_refresh_is_saved_over(self, token_file):
        """It is not a later grant, so the refresh wins and the sign-in is lost."""

        def urlopen(request, timeout=None):
            token_file.write_text(
                json.dumps({"access_token": "tok-auth", "refresh_token": "ref-auth", "expiry": 0})
            )
            return _answer(GOOD_ANSWER)

        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert json.loads(token_file.read_text())["refresh_token"] == "ref-new"

    def test_an_access_only_sign_in_inside_the_buffer_reports_success_then_is_refused(
        self, token_file, capsys
    ):
        body = json.dumps({"access_token": "tok-new", "expires_in": 100}).encode()
        with patch("urllib.request.urlopen", lambda *a, **k: _answer(body)):
            auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")
        assert "Auth complete" in capsys.readouterr().out
        assert json.loads(token_file.read_text())["expiry"] > _now()
        with pytest.raises(auth_module.TokenRefused, match="no refresh token"):
            auth_module.refresh_token()
