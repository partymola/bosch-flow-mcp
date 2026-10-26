"""Tests for auth token management."""

import io
import json
import logging
import socket
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest

import bosch_flow_mcp.auth as auth_module
from bosch_flow_mcp.config import CLIENT_ID


def _write_tokens(path, **tokens):
    path.write_text(json.dumps(tokens))


def test_refresh_token_missing_file(tmp_path, monkeypatch):
    """A missing token file is a refusal, not a network condition.

    Both classes subclass RuntimeError, so asserting the base class alone
    cannot tell them apart - and they lead to opposite advice: re-authorise,
    or wait for the network.
    """
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tmp_path / "no_tokens.json")
    with pytest.raises(auth_module.TokenRefused, match="not configured"):
        auth_module.refresh_token()


def test_refresh_token_uses_the_stored_token_if_valid(tmp_path, monkeypatch):
    """refresh_token returns the stored token without a request if not expired."""
    tokens_path = tmp_path / "tokens.json"
    _write_tokens(
        tokens_path,
        access_token="stored_token_abc",
        refresh_token="refresh_xyz",
        expiry=datetime.now(timezone.utc).timestamp() + 3600,
    )
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tokens_path)
    token = auth_module.refresh_token()
    assert token == "stored_token_abc"


def test_refresh_token_refreshes_when_expired(tmp_path, monkeypatch):
    """refresh_token calls token URL when access token is expired."""
    past_expiry = datetime.now(timezone.utc).timestamp() - 10

    fake_response_data = json.dumps(
        {
            "access_token": "new_token_def",
            "refresh_token": "new_refresh",
            "expires_in": 7200,
        }
    ).encode()

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = fake_response_data

    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "access_token": "old_token",
                "refresh_token": "valid_refresh",
                "expiry": past_expiry,
            }
        )
    )
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tokens_path)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        token = auth_module.refresh_token()

    assert token == "new_token_def"
    assert json.loads(tokens_path.read_text())["access_token"] == "new_token_def"


def test_refresh_token_no_refresh_token_raises(tmp_path, monkeypatch):
    """An expired token with no refresh token to renew it is a refusal."""
    tokens_path = tmp_path / "tokens.json"
    _write_tokens(
        tokens_path,
        access_token="old_token",
        refresh_token="",
        expiry=datetime.now(timezone.utc).timestamp() - 10,
    )
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tokens_path)

    with pytest.raises(auth_module.TokenRefused, match="no refresh token"):
        auth_module.refresh_token()


def test_client_id_is_one_bike_app():
    """Primary client ID is the mobile app public client."""
    assert CLIENT_ID == "one-bike-app"


def test_generate_pkce():
    """PKCE verifier and challenge should be non-empty base64url strings."""
    verifier, challenge = auth_module._generate_pkce()
    assert len(verifier) >= 40
    assert len(challenge) >= 40
    assert "=" not in verifier
    assert "=" not in challenge


# --- Routing helpers: current_client_id / token_is_euda ---


def test_current_client_id_reads_stored_id(token_as):
    """current_client_id reflects the client_id written in the token file."""
    token_as("euda-abc123")
    assert auth_module.current_client_id() == "euda-abc123"


def test_current_client_id_fallback_when_no_client_id(tmp_path, monkeypatch):
    """A legacy token lacking client_id falls back to the default client."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "x", "expiry": 9999999999}))
    monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
    assert auth_module.current_client_id() == CLIENT_ID


def test_current_client_id_missing_file_fallback():
    """With no token file (isolated by default), falls back without raising."""
    assert auth_module.current_client_id() == CLIENT_ID


def test_current_client_id_reflects_file_change(token_as):
    """current_client_id reads the file fresh, so an out-of-band re-auth is seen."""
    token_as("one-bike-app")
    assert auth_module.current_client_id() == "one-bike-app"
    token_as("euda-newclient")  # rewrites the same token file
    assert auth_module.current_client_id() == "euda-newclient"


@pytest.mark.parametrize(
    "client_id,expected",
    [
        ("euda-00000000-0000-0000-0000-000000000001", True),
        ("euda-", True),
        ("one-bike-app", False),
    ],
)
def test_token_is_euda(token_as, client_id, expected):
    token_as(client_id)
    assert auth_module.token_is_euda() is expected


def test_token_is_euda_no_token_is_false():
    """No token file -> default one-bike-app -> not euda (and no crash on None)."""
    assert auth_module.token_is_euda() is False


class TestTheTokenFileIsTheOnlyCopy:
    """`bosch-flow-mcp auth` runs in another process while the server runs.

    Each test writes real files at a temp path, because the property is about
    what is on disk, not about anything the module holds. The refresh token is
    treated as rotating, so a refreshed one left unsaved is a grant lost.
    """

    _REFRESHED = {"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 7200}

    @pytest.fixture(autouse=True)
    def _token_file(self, tmp_path, monkeypatch):
        self.path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", self.path)

    def _write(self, access="tok-old", refresh="ref-old", expiry=0.0, client_id=CLIENT_ID):
        self.path.write_text(
            json.dumps(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expiry": expiry,
                    "client_id": client_id,
                }
            )
        )

    def _stored(self):
        return json.loads(self.path.read_text())

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _answer(payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def _urlopen_writing(self, then, raw=None, **tokens):
        """An urlopen that rewrites the token file mid-request, then answers."""

        def urlopen(request, timeout=None):
            if raw is not None:
                self.path.write_text(raw)
            else:
                self._write(**tokens)
            if isinstance(then, Exception):
                raise then
            return self._answer(then)

        return urlopen

    @staticmethod
    def _refusal(code):
        import urllib.error

        return urllib.error.HTTPError("https://example.invalid", code, "no", {}, io.BytesIO(b""))

    def test_a_token_written_by_auth_is_used_without_a_restart(self):
        """Held in memory, the old token stays in use after `auth` until a
        restart, while routing already reads the new client from the file."""
        import os

        self._write(access="tok-aaaa", expiry=self._now() + 3600)
        assert auth_module.refresh_token() == "tok-aaaa"
        before = self.path.stat()
        self.path.write_text(self.path.read_text().replace("tok-aaaa", "tok-bbbb"))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert auth_module.refresh_token() == "tok-bbbb"

    def test_a_refresh_does_not_overwrite_a_grant_saved_while_it_ran(self):
        """Saving over it loses the user's new grant.

        The competing grant expires before the refreshed one would, as one
        issued moments earlier does, so a comparison against the refreshed
        expiry instead of the starting one fails here.
        """
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._REFRESHED, access="tok-auth", refresh="ref-auth", expiry=self._now() + 3600
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-auth"
        assert (self._stored()["access_token"], self._stored()["refresh_token"]) == (
            "tok-auth",
            "ref-auth",
        )

    @pytest.mark.parametrize("copy_expiry", [-100, 50], ids=["older", "later-but-expired"])
    def test_a_refresh_saves_over_a_copy_it_cannot_use(self, copy_expiry):
        """An older copy, or one whose token has expired, holds a refresh token
        rotation has already retired; the one just issued is the live one."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._REFRESHED, access="tok-copy", refresh="ref-copy", expiry=copy_expiry
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_refresh_does_not_overwrite_a_file_caught_mid_write(self):
        """Unreadable after the refresh most likely means another writer is
        part-way through, so the token just obtained is returned unsaved."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(self._REFRESHED, raw="{")
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert self.path.read_text() == "{"

    @pytest.mark.parametrize("code", sorted(auth_module._REFUSAL_CODES))
    def test_a_refusal_after_the_file_was_replaced_uses_the_new_file(self, code):
        """The refusal judged a refresh token `auth` had already replaced.
        Reporting it as a refusal tells the user to re-authorise over a working
        grant."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._refusal(code), access="tok-auth", refresh="ref-auth", expiry=self._now() + 3600
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-auth"

    @pytest.mark.parametrize(
        "copy_expiry", [-100, 50, "usable"], ids=["older", "later-but-expired", "usable"]
    )
    def test_a_refusal_with_the_same_refresh_token_in_the_file_is_a_refusal(self, copy_expiry):
        """The file changed in other fields, but still holds the refresh token
        that was refused, so the refusal is a verdict on it, even where its
        access token is still valid."""
        if copy_expiry == "usable":
            copy_expiry = self._now() + 3600
        self._write(expiry=0)
        urlopen = self._urlopen_writing(self._refusal(400), access="tok-copy", expiry=copy_expiry)
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth_module.TokenRefused):
                auth_module.refresh_token()

    @pytest.mark.parametrize("copy_expiry", [-100, 50], ids=["older", "later-but-expired"])
    def test_a_refusal_after_the_file_changed_to_an_unusable_grant_is_not_a_refusal(
        self, copy_expiry
    ):
        """The refusal judged the refresh token sent, not the one now in the
        file, so the next call refreshes with that one rather than this call
        telling the user to re-authorise."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._refusal(400), access="tok-copy", refresh="ref-copy", expiry=copy_expiry
        )
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()

    @pytest.mark.parametrize("client_id", [CLIENT_ID, "euda-00000000-0000-0000-0000-000000000009"])
    def test_a_sign_in_to_either_client_saved_mid_request_wins(self, client_id):
        """A re-auth can switch client, and that is the case routing follows:
        saving the old sign-in back over it sends one client's token to the
        other client's host."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._REFRESHED,
            access="tok-auth",
            refresh="ref-auth",
            expiry=self._now() + 3600,
            client_id=client_id,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-auth"
        assert self._stored()["client_id"] == client_id

    def test_a_refresh_sends_and_keeps_the_client_the_token_was_issued_to(self):
        """Routing reads the client back out of the file, so a refresh that
        sent or saved another one would route to the wrong host."""
        euda = "euda-00000000-0000-0000-0000-000000000009"
        self._write(expiry=0, client_id=euda)
        sent = []

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["client_id"][0])
            return self._answer(self._REFRESHED)

        with patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
        assert sent == [euda]
        assert self._stored()["client_id"] == euda

    @pytest.mark.parametrize("truncate", [False, True], ids=["file-intact", "file-emptied"])
    def test_a_held_grant_that_expires_is_refreshed_with_its_own_refresh_token(self, truncate):
        """The file's refresh token is the one the held grant replaced, and may
        already be spent; sending it is refused, and reads as the file
        unchanged, so the user is told to re-authorise over a live grant."""
        self._write(expiry=0)
        sent = []
        answers = [
            dict(self._REFRESHED, expires_in=0),
            dict(self._REFRESHED, access_token="tok-long", refresh_token="ref-long"),
        ]

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["refresh_token"][0])
            return self._answer(answers.pop(0))

        # Two failures, so the retry before the second refresh fails too and
        # the file still holds ref-old when it runs.
        with self._fail_saves(2, truncate=truncate), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-long"
        assert sent == ["ref-old", "ref-new"]

    def test_a_later_grant_that_wins_also_ends_a_held_one(self):
        """Left held, a later unreadable moment saves the held grant, whose
        refresh token this refresh has just spent, over the other writer's."""
        self._write(expiry=0)
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            if len(posts) == 1:
                return self._answer(dict(self._REFRESHED, expires_in=0))
            self._write(access="tok-auth", refresh="ref-auth", expiry=self._now() + 3600)
            return self._answer(self._REFRESHED)

        # Two failures: the first refresh's save, and the retry before the
        # second, so the grant is still held when the second refresh runs.
        with self._fail_saves(2), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-auth"
        assert len(posts) == 2
        assert auth_module._unsaved is None

    def test_a_re_read_file_with_a_blank_access_token_is_saved_over(self):
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._REFRESHED, access="", refresh="ref-blank", expiry=self._now() + 3600
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_non_numeric_expiry_counts_as_expired(self):
        self._write(expiry="soon")
        with patch("urllib.request.urlopen", lambda *a, **k: self._answer(self._REFRESHED)):
            assert auth_module.refresh_token() == "tok-new"
        assert isinstance(self._stored()["expiry"], float)

    def test_a_refresh_saves_when_the_clock_steps_back_during_it(self):
        """The file is untouched and its token still looks valid by the stepped
        clock; only the later-expiry test tells it from a grant someone saved,
        and yielding to it leaves the file on a replaced refresh token."""
        start = self._now()
        clock = {"now": start}

        class _SteppingDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock["now"], tz)

        self._write(expiry=start + 200)

        def urlopen(request, timeout=None):
            clock["now"] = start - 1000
            return self._answer(self._REFRESHED)

        with (
            patch.object(auth_module, "datetime", _SteppingDatetime),
            patch("urllib.request.urlopen", urlopen),
        ):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-new"

    def _fail_saves(self, times, truncate=False):
        """A `_save_json` that raises OSError for its first `times` calls."""
        real = auth_module._save_json
        calls = {"n": 0}

        def save(path, data):
            calls["n"] += 1
            if calls["n"] <= times:
                if truncate:
                    self.path.write_text("")
                raise OSError(28, "No space left on device")
            real(path, data)

        return patch.object(auth_module, "_save_json", save)

    @pytest.mark.parametrize("truncate", [False, True], ids=["file-intact", "file-emptied"])
    def test_a_grant_whose_save_failed_is_kept_and_saved_later(self, truncate):
        """The refresh token in the file may already be spent, so the refreshed
        grant is the only live copy until a save succeeds."""
        self._write(expiry=0)
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            return self._answer(self._REFRESHED)

        with self._fail_saves(1, truncate=truncate), patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
            assert auth_module.refresh_token() == "tok-new"
        assert len(posts) == 1
        assert self._stored()["refresh_token"] == "ref-new"
        assert auth_module._unsaved is None

    def test_a_held_grant_that_expires_is_refreshed_and_saved_over_its_own_empty_file(self):
        """The file is empty because this process's save failed half-way, not
        because another writer is mid-write, so the next refresh repairs it."""
        self._write(expiry=0)
        answers = [
            self._answer(dict(self._REFRESHED, expires_in=0)),
            self._answer(dict(self._REFRESHED, access_token="tok-long", refresh_token="ref-long")),
        ]
        with (
            self._fail_saves(2, truncate=True),
            patch("urllib.request.urlopen", lambda *a, **k: answers.pop(0)),
        ):
            auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-long"
        assert self._stored()["refresh_token"] == "ref-long"

    def test_a_held_grant_is_used_and_saved_while_the_file_has_gone(self):
        """A missing file counts as unreadable, and the held grant is the only
        live copy: dropping it leaves nothing to refresh from."""
        self._write(expiry=0)
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            return self._answer(self._REFRESHED)

        with self._fail_saves(1), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            self.path.unlink()
            assert auth_module.refresh_token() == "tok-new"
        assert len(posts) == 1
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_held_grant_is_keyed_to_the_file_as_it_was_when_the_save_failed(self):
        self._write(expiry=0)
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            self._write(access="tok-copy", refresh="ref-copy", expiry=-100)
            return self._answer(self._REFRESHED)

        with self._fail_saves(1), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-new"
        assert len(posts) == 1
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_held_grant_gives_way_to_a_file_written_since(self):
        self._write(expiry=0)
        with (
            self._fail_saves(1),
            patch("urllib.request.urlopen", lambda *a, **k: self._answer(self._REFRESHED)),
        ):
            auth_module.refresh_token()
        self._write(access="tok-auth", refresh="ref-auth", expiry=self._now() + 3600)
        assert auth_module.refresh_token() == "tok-auth"
        assert auth_module._unsaved is None

    @pytest.mark.parametrize("truncate", [False, True], ids=["file-intact", "file-emptied"])
    def test_a_refused_held_grant_is_a_refusal(self, truncate):
        """Otherwise its refusal reads as a change to the file, which still
        holds the token it replaced or was emptied by the failed write, and
        every call refreshes with a dead token."""
        self._write(expiry=0)
        answers = [self._answer(dict(self._REFRESHED, expires_in=0)), self._refusal(400)]

        def urlopen(request, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with self._fail_saves(9, truncate=truncate), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            with pytest.raises(auth_module.TokenRefused):
                auth_module.refresh_token()
        assert auth_module._unsaved is None

    def test_a_refusal_while_the_file_is_unreadable_is_not_a_refusal(self):
        """The file is most likely part-way through being replaced, which clears
        on its own; a refusal would tell the user to re-authorise over it."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(self._refusal(400), raw="{")
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()

    def test_concurrent_calls_refresh_once(self):
        """Tool calls run in worker threads. Unserialised, two spend the same
        refresh token."""
        import threading

        self._write(expiry=0)
        posts = []
        second = {}

        def call_from_another_thread():
            second["token"] = auth_module.refresh_token()

        other = threading.Thread(target=call_from_another_thread)

        def urlopen(request, timeout=None):
            posts.append(request)
            if len(posts) == 1:
                other.start()
                # Long enough for an unserialised second call to reach the
                # network; with the lock it is still waiting when this returns.
                other.join(timeout=0.5)
            return self._answer(dict(self._REFRESHED, access_token=f"tok-{len(posts)}"))

        with patch("urllib.request.urlopen", urlopen):
            first = auth_module.refresh_token()
            other.join(timeout=5)

        assert len(posts) == 1
        assert second["token"] == first


class TestTheRefreshBoundary:
    """Every exit from refresh_token is one of two types, by construction.

    Classifying by a list of exception types instead grades whatever nobody
    listed as a dead credential, which is the answer that rewrites the token
    file and spends a working refresh token.
    """

    def _refresh_with_worker_raising(self, exc, monkeypatch):
        monkeypatch.setattr(auth_module, "_refresh_token", MagicMock(side_effect=exc))
        return auth_module.refresh_token()

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("bare timeout"),
            ConnectionResetError("reset"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            KeyError("access_token"),
            ValueError("unparseable"),
            RuntimeError("something nobody classified"),
        ],
        ids=["timeout", "reset", "undecodable", "keyerror", "valueerror", "runtime"],
    )
    def test_an_unclassified_failure_becomes_a_network_error(self, exc, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_worker_raising(exc, monkeypatch)

    def test_a_non_http_response_becomes_a_network_error(self, monkeypatch):
        """http.client exceptions are not OSError, so they escaped every tuple."""
        import http.client

        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_worker_raising(http.client.BadStatusLine("garbage"), monkeypatch)

    def test_a_refusal_passes_through_unchanged(self, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_worker_raising(auth_module.TokenRefused("revoked"), monkeypatch)

    def test_a_successful_refresh_is_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(auth_module, "_refresh_token", MagicMock(return_value="a-token"))
        assert auth_module.refresh_token() == "a-token"


class TestRefusalsAndNetworkConditions:
    """Only a verdict on the credentials is a refusal."""

    def _refresh_with_urlopen(self, urlopen, tmp_path, monkeypatch):
        tokens = tmp_path / "bosch_tokens.json"
        tokens.write_text(
            json.dumps(
                {"access_token": "a", "refresh_token": "r", "expiry": 0, "client_id": CLIENT_ID}
            )
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens)
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", urlopen)
        return auth_module.refresh_token()

    def _http_error(self, code):
        import io
        import urllib.error

        return urllib.error.HTTPError("https://example.invalid", code, "no", {}, io.BytesIO(b""))

    def _responding(self, payload):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return MagicMock(return_value=resp)

    def test_a_revoked_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(400)), tmp_path, monkeypatch
            )

    def test_a_rate_limit_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(429)), tmp_path, monkeypatch
            )

    def test_a_server_side_failure_is_not_a_refusal(self, tmp_path, monkeypatch):
        """A 5xx says the server could not answer, not that the grant is bad."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(500)), tmp_path, monkeypatch
            )

    def test_a_waf_block_is_not_a_refusal(self, tmp_path, monkeypatch):
        """403 is what bot protection returns; it says nothing about the grant."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(403)), tmp_path, monkeypatch
            )

    def test_an_undecodable_body_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                self._responding(b"\xe9\xff not utf-8"), tmp_path, monkeypatch
            )

    def test_a_body_that_is_not_json_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                self._responding(b"<html>captive portal</html>"), tmp_path, monkeypatch
            )

    def test_a_response_without_a_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_urlopen(
                self._responding(b'{"token_type": "Bearer"}'), tmp_path, monkeypatch
            )

    def test_an_unreadable_token_file_is_a_refusal(self, tmp_path, monkeypatch):
        tokens = tmp_path / "bosch_tokens.json"
        tokens.write_text("{not json")
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()

    def test_an_unreachable_server_is_not_a_refusal(self, tmp_path, monkeypatch):
        import urllib.error

        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=urllib.error.URLError("name resolution failed")),
                tmp_path,
                monkeypatch,
            )

    def test_a_read_timeout_is_not_a_refusal(self, tmp_path, monkeypatch):
        """urlopen wraps only connect-phase errors, so this arrives bare."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=TimeoutError("timed out")), tmp_path, monkeypatch
            )

    def test_a_reset_connection_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=ConnectionResetError("reset")), tmp_path, monkeypatch
            )


class TestTheRoutingHelpersNeverRaise:
    """current_client_id and token_is_euda are called outside any handler.

    _load_json classifies an unusable credential file as TokenRefused, which
    is right for the refresh path. These two answer "which client is in use"
    for routing, and sync_tools calls one before its try block - so a corrupt
    token file used to take the whole sync out with an unclassified error.
    """

    @pytest.mark.parametrize(
        "contents",
        ["{not json", '["not", "an", "object"]', ""],
        ids=["unparseable", "wrong-shape", "empty"],
    )
    def test_current_client_id_falls_back(self, contents, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(contents)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        assert auth_module.current_client_id() == CLIENT_ID

    @pytest.mark.parametrize(
        "contents",
        ["{not json", '["not", "an", "object"]'],
        ids=["unparseable", "wrong-shape"],
    )
    def test_token_is_euda_falls_back(self, contents, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(contents)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        assert auth_module.token_is_euda() is False


class TestRefusalsAreNotNetworkConditions:
    """The refusal sites the commit named, each pinned to the right class."""

    def _refresh_with_response(self, payload, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry": 0}))
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", MagicMock(return_value=resp))
        return auth_module.refresh_token()

    def test_an_unauthorised_response_is_a_refusal(self, tmp_path, monkeypatch):
        import io
        import urllib.error

        path = tmp_path / "bosch_tokens.json"
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry": 0}))
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        monkeypatch.setattr(
            auth_module.urllib.request,
            "urlopen",
            MagicMock(side_effect=urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))),
        )
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()

    def test_a_response_of_the_wrong_shape_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_response(b'["not", "an", "object"]', tmp_path, monkeypatch)

    def test_a_response_without_a_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_response(b'{"token_type": "Bearer"}', tmp_path, monkeypatch)

    def test_a_token_file_of_the_wrong_shape_is_a_refusal(self, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text('["not", "an", "object"]')
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()


class TestTheFallbackKeepsTheConfiguredClient:
    """Which client the fallback picks, not merely that it does not raise.

    A EUDA user whose token file is half-written must still route as EUDA:
    falling back to the hardcoded client instead answers the Data-Act types
    with "register a euda client", which they already have, and nothing
    anywhere says the token file is the problem.
    """

    def _with_euda_config(self, tmp_path, monkeypatch, tokens_contents):
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text(
            json.dumps({"client_id": "euda-00000000-0000-0000-0000-000000000009"})
        )
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text(tokens_contents)
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)

    def test_a_corrupt_token_file_still_routes_as_euda(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, "{not json")
        assert auth_module.current_client_id().startswith("euda-")
        assert auth_module.token_is_euda() is True

    def test_a_readable_token_file_still_wins(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, json.dumps({"client_id": "one-bike-app"}))
        assert auth_module.current_client_id() == "one-bike-app"
        assert auth_module.token_is_euda() is False

    def test_no_token_file_still_routes_as_euda(self, tmp_path, monkeypatch):
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text(
            json.dumps({"client_id": "euda-00000000-0000-0000-0000-000000000009"})
        )
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tmp_path / "absent.json")
        assert auth_module.token_is_euda() is True

    def test_a_legacy_token_without_a_client_id_still_routes_as_euda(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, json.dumps({"access_token": "a"}))
        assert auth_module.token_is_euda() is True

    def test_a_malformed_config_file_does_not_raise(self, tmp_path, monkeypatch):
        """_get_client_id's own guard is what keeps the never-raises promise.

        current_client_id calls it from its except branch, outside any try.
        """
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text("{not json")
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text("{not json either")
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)

        assert auth_module.current_client_id() == CLIENT_ID
        assert auth_module.token_is_euda() is False


class TestAStoredTokenWithoutOneIsARefusal:
    def test_a_token_file_with_no_access_token(self, tmp_path, monkeypatch):
        """The expiry check ran first, so this escaped api.get as a KeyError.

        Reaching this state needs a writer outside this repo: _exchange_code and
        _refresh_token both always store an access_token, and a half-written file
        fails to parse and is refused before this check. Note the refusal is
        strict - a file holding a live refresh_token would have refreshed - so if
        a future writer can produce this state, guard the early return rather
        than relax this test.
        """
        path = tmp_path / "bosch_tokens.json"
        path.write_text(
            json.dumps({"refresh_token": "r", "expiry": 4102444800, "client_id": CLIENT_ID})
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
class TestTheTokenFileIsNeverBrieflyReadable:
    """Writing first and chmodding after left a new token world-readable.

    Only for the duration of the write, and only at creation - but the
    window is real, and the mode was untested either way.
    """

    def test_a_new_file_is_created_owner_only(self, tmp_path):
        import os as os_module

        seen = {}
        real_open = os_module.open

        def spy(path, flags, mode=0o777, **kwargs):
            seen["mode"] = mode
            return real_open(path, flags, mode, **kwargs)

        target = tmp_path / "bosch_tokens.json"
        with patch.object(auth_module.os, "open", spy):
            auth_module._save_json(target, {"refresh_token": "fictional"})

        assert oct(seen["mode"]) == "0o600"
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_an_existing_loose_file_is_tightened(self, tmp_path):
        """And tightened before the token is written, not after.

        On a pre-existing file the mode at open is whatever it already was,
        so writing first would put a real token on disk world-readable -
        which the final mode and the open mode both look right through.
        """
        import os as os_module

        real_fchmod = os_module.fchmod

        def spy(fd, mode):
            assert os_module.fstat(fd).st_size == 0, "content was written before tightening"
            return real_fchmod(fd, mode)

        target = tmp_path / "bosch_tokens.json"
        target.write_text("{}")
        os_module.chmod(target, 0o644)
        with patch.object(auth_module.os, "fchmod", spy):
            auth_module._save_json(target, {"refresh_token": "fictional"})
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_a_failure_to_tighten_does_not_take_the_token_with_it(self, tmp_path):
        def refuse(fd, mode):
            raise PermissionError(1, "Operation not permitted")

        target = tmp_path / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')
        with patch.object(auth_module.os, "fchmod", refuse):
            auth_module._save_json(target, {"refresh_token": "rotated"})

        assert json.loads(target.read_text())["refresh_token"] == "rotated"


class _RecordingSocket:
    """Stands in for the real socket so server_bind's Windows branch can run."""

    def __init__(self):
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def getsockname(self):
        return ("127.0.0.1", auth_module.EUDA_CALLBACK_PORT)


class TestTheCallbackPortIsNotShared:
    """The listener carries the authorisation code, so it must not be displaceable.

    `server_bind` chooses on `sys.platform` at call time, so the Windows branch
    is reachable from a POSIX runner against a recording socket. That is worth
    more than reading the source for it: source assertions here let the option
    be set after the bind, at the wrong level, on the wrong socket, or with a
    value of 0, all of which pass a check that only looks for the name.
    """

    def _bind_as(self, platform, monkeypatch):
        monkeypatch.setattr(auth_module.sys, "platform", platform)
        # Absent on POSIX, so it has to be created rather than replaced.
        monkeypatch.setattr(auth_module.socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFF, raising=False)
        monkeypatch.setattr(auth_module.socket, "getfqdn", lambda host: host)

        server = auth_module._CallbackServer.__new__(auth_module._CallbackServer)
        server.socket = _RecordingSocket()
        server.server_address = ("localhost", auth_module.EUDA_CALLBACK_PORT)
        # The class attribute is fixed at import against the real platform, so
        # the value the other test pins is supplied here rather than read.
        server.allow_reuse_address = platform != "win32"
        server.allow_reuse_port = False
        auth_module._CallbackServer.server_bind(server)
        return server.socket.calls

    def test_windows_asks_for_exclusive_use_before_binding(self, monkeypatch):
        calls = self._bind_as("win32", monkeypatch)
        assert calls[0] == (
            "setsockopt",
            socket.SOL_SOCKET,
            auth_module.socket.SO_EXCLUSIVEADDRUSE,
            1,
        ), calls
        assert ("bind", ("localhost", auth_module.EUDA_CALLBACK_PORT)) in calls
        # Never both: asking to share after asking not to is the configuration
        # Microsoft documents as insecure.
        assert not any(
            call[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR) for call in calls
        ), calls

    def test_posix_asks_for_nothing_exclusive(self, monkeypatch):
        calls = self._bind_as("linux", monkeypatch)
        assert not any(
            call[0] == "setsockopt" and call[2] == auth_module.socket.SO_EXCLUSIVEADDRUSE
            for call in calls
        ), calls

    def test_reuse_is_allowed_off_windows_and_refused_on_it(self):
        """Not asking to share is the fix; the exclusive option is the belt.

        The runtime value alone cannot see a revert to a bare `True`, because
        that is what this platform is supposed to have, so the source form is
        pinned with it.
        """
        import ast
        import inspect

        assert auth_module._CallbackServer.allow_reuse_address == (sys.platform != "win32")

        tree = ast.parse(inspect.getsource(auth_module._CallbackServer))
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allow_reuse_address" for t in node.targets)
        ]
        assert assigned, "allow_reuse_address is no longer set here"
        assert all("platform" in value for value in assigned), assigned

    def test_only_win32_is_named(self):
        """`sys.platform` is `win32` on 64-bit Windows too, so a `win64` test
        is a branch that never runs."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth_module._CallbackServer))
        compared = {
            const.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "sys.platform"
            for const in node.comparators
            if isinstance(const, ast.Constant)
        }
        assert compared == {"win32"}, compared

    @pytest.mark.skipif(sys.platform == "win32", reason="binds a real POSIX socket")
    def test_it_still_binds(self):
        """server_bind is overridden, so a mistake there breaks `auth` outright."""
        server = auth_module._CallbackServer(("localhost", 0), auth_module.BaseHTTPRequestHandler)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert server.server_address[1] != 0
        finally:
            server.server_close()

    def test_no_bare_httpserver_is_constructed_anywhere(self):
        """Scoped to the module, not to the EUDA flow: a second listener added
        elsewhere would carry the default this class exists to refuse."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth_module))
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPServer"
        ]
        assert not bare, bare

    def test_a_busy_port_is_reported_before_the_browser_opens(self, monkeypatch, capsys):
        """The only test that reaches the EUDA flow, and it patches the name the
        flow constructs: patching `HTTPServer` instead guards nothing.

        A port that is already held is a failure the flow could not have before,
        so it has to end in advice rather than a traceback - and it has to end
        there before an authorisation page is opened onto a redirect with
        nowhere to land.
        """
        opened = []
        monkeypatch.setattr(auth_module.webbrowser, "open", lambda url: opened.append(url))

        def refuse_to_bind(*args, **kwargs):
            raise OSError(98, "Address already in use")

        monkeypatch.setattr(auth_module, "_CallbackServer", refuse_to_bind)

        with pytest.raises(SystemExit) as exit_info:
            auth_module._setup_euda_auth()

        assert exit_info.value.code == 1
        assert opened == [], opened
        assert str(auth_module.EUDA_CALLBACK_PORT) in capsys.readouterr().err


def _responding(body):
    """A urlopen stand-in returning `body` with a 200."""

    def raiser(req, timeout=None):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        return _Resp()

    return raiser


class TestTheCodeExchangeReportsNoResponseContent:
    """`_exchange_code` runs at a terminal, and its failures are printed there.

    Some of these carry the server's own words. A refused exchange has an
    error body, and Bosch has no obligation to keep an account out of it; a
    response of the wrong shape is a token dict, which is the worse one,
    because a body carrying a `refresh_token` and no `access_token` is exactly
    the shape that reaches it. The rest reach this function as failures that
    would otherwise escape as a traceback, which names this install's paths.
    What a reader needs is the status and the fact it was refused, and none of
    it needs the response to say so.
    """

    def _run(self, monkeypatch, raiser, capsys, caplog, expected):
        """Drive one failure and hold the whole message to fixed text.

        Equality rather than the absence of a chosen string: a message that
        appends the server's `error_description`, or the HTTP reason phrase, or
        the response headers passes every "is this secret absent" assertion
        while carrying whatever the server chose to put there.

        `caplog` as well as `capsys`, because they are different channels. The
        logging plugin owns a root handler under pytest, so a `logger.error`
        carrying the token dict never reaches `capsys` - while in production
        `cli.main` calls `basicConfig(stream=sys.stderr)` and it lands on the
        terminal verbatim. Switching a print to a logger is the natural way to
        restore the diagnosis this function deliberately gives up.
        """
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", raiser)
        with caplog.at_level(logging.DEBUG, logger="bosch_flow_mcp"):
            with pytest.raises(SystemExit) as exit_info:
                auth_module._exchange_code(
                    "cid", "the-code", "the-verifier", "http://localhost:1/cb"
                )
        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.strip() == expected
        # stdout as well, and it is the likelier of the two: every other print
        # in this function and in both setup flows goes there, so a maintainer
        # restoring the diagnosis writes a bare `print`. Same terminal, same
        # scrollback, same disclosure.
        assert captured.out == "", captured.out
        assert caplog.records == [], [r.getMessage() for r in caplog.records]

    def test_a_refused_exchange_reports_the_status_not_the_body(self, monkeypatch, capsys, caplog):
        import urllib.error

        secret = "account_id=ACC-SECRET-42 owner=A Person"

        def raiser(req, timeout=None):
            raise urllib.error.HTTPError(
                "https://bosch.example/token",
                400,
                "Bad Request: ACC-SECRET-42",
                {"X-Account": "ACC-SECRET-42"},
                io.BytesIO(secret.encode()),
            )

        self._run(monkeypatch, raiser, capsys, caplog, "Error exchanging code: HTTP 400.")

    def test_a_response_of_the_wrong_shape_never_prints_the_tokens(
        self, monkeypatch, capsys, caplog
    ):
        # The realistic shape: a refresh token arrives, an access token does
        # not, and printing the dict to explain that puts the refresh token on
        # the terminal and into whatever scrollback keeps it.
        body = json.dumps(
            {
                "refresh_token": "rt-SECRET-VALUE",
                "token_type": "Bearer",
                "error_description": "account ACC-SECRET-42 is not provisioned",
            }
        ).encode()

        self._run(
            monkeypatch,
            _responding(body),
            capsys,
            caplog,
            "Error: the token response carried no access token.",
        )

    def test_a_transport_failure_reports_only_its_type(self, monkeypatch, capsys, caplog):
        """`str(e)` carries text the peer influences.

        Measured, a refused connection and a DNS failure name no path, so this
        is not about paths: a TLS name mismatch echoes an identity string back
        out of the handshake, and the type name is the only part of any of them
        that is ours.
        """
        import urllib.error

        def raiser(req, timeout=None):
            raise urllib.error.URLError("certificate is not valid for 'evil.example'")

        self._run(monkeypatch, raiser, capsys, caplog, "Error exchanging code: URLError.")

    def test_a_read_phase_failure_is_handled_rather_than_traced(self, monkeypatch, capsys, caplog):
        """urlopen wraps only connect-phase failures, so this arrives bare.

        Unhandled it escapes as a traceback, which names this install's
        absolute paths - the disclosure the rest of this class exists to stop.
        """

        def raiser(req, timeout=None):
            raise TimeoutError("timed out")

        self._run(monkeypatch, raiser, capsys, caplog, "Error exchanging code: TimeoutError.")

    def test_a_body_that_is_not_json_is_handled_rather_than_traced(
        self, monkeypatch, capsys, caplog
    ):
        # An interstitial from a proxy or WAF is the realistic shape, and
        # json.loads raises past both transport handlers.
        self._run(
            monkeypatch,
            _responding(b"<html>blocked</html>"),
            capsys,
            caplog,
            "Error exchanging code: the response was not readable.",
        )

    def test_a_json_scalar_is_refused_rather_than_subscripted(self, monkeypatch, capsys, caplog):
        # `"access_token" not in 42` is a TypeError, so the shape check needs
        # the isinstance guard to reach its own message.
        self._run(
            monkeypatch,
            _responding(b"42"),
            capsys,
            caplog,
            "Error: the token response carried no access token.",
        )

    def test_the_success_path_prints_neither_token(self, monkeypatch, capsys, caplog, tmp_path):
        """The exit nothing else drives, and the one holding both credentials.

        Without it there is nowhere for an assertion about the success path to
        live, so a diagnostic added there is unpinned however the failure
        branches are covered.
        """
        tokens_path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        body = json.dumps(
            {
                "access_token": "at-SECRET-VALUE",
                "refresh_token": "rt-SECRET-VALUE",
                "expires_in": 3600,
            }
        ).encode()
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", _responding(body))

        with caplog.at_level(logging.DEBUG, logger="bosch_flow_mcp"):
            auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        stored = json.loads(tokens_path.read_text())
        assert stored["access_token"] == "at-SECRET-VALUE"
        assert stored["refresh_token"] == "rt-SECRET-VALUE"

        captured = capsys.readouterr()
        for stream in (captured.out, captured.err):
            assert "at-SECRET-VALUE" not in stream, stream
            assert "rt-SECRET-VALUE" not in stream, stream
        assert not any("SECRET-VALUE" in r.getMessage() for r in caplog.records)
