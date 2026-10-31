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

    def test_a_refresh_saves_over_a_file_it_cannot_read(self):
        """Saves replace the file in one step, so an unreadable one is gone or
        corrupt rather than part-way through a save, and the refresh token
        just issued is the only live one."""
        self._write(expiry=0)
        urlopen = self._urlopen_writing(self._REFRESHED, raw="{")
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-new"

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

    @pytest.mark.parametrize(
        "unusable",
        [
            {},
            {"refresh_token": None},
            {"refresh_token": ""},
            {"refresh_token": "   "},
            {"refresh_token": "a\nb"},
            {"refresh_token": "a\tb"},
            {"refresh_token": "a\rb"},
            {"refresh_token": "a\x00b"},
            {"refresh_token": "a\x7fb"},
            {"refresh_token": "tök"},
            {"refresh_token": 12345},
        ],
        ids=[
            "omitted",
            "null",
            "blank",
            "spaces",
            "newline",
            "tab",
            "return",
            "nul",
            "delete",
            "not-ascii",
            "not-text",
        ],
    )
    def test_a_grant_with_no_usable_refresh_token_keeps_the_stored_one(self, unusable):
        """Saved over it, the live refresh token is gone and the next refresh is
        refused, with the credentials never judged."""
        self._write(expiry=0)
        answer = {"access_token": "tok-new", "expires_in": 7200, **unusable}
        with patch("urllib.request.urlopen", lambda *a, **k: self._answer(answer)):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-old"

    def _rotating(self, *answers):
        """A token endpoint that enforces rotation: a usable refresh token in an
        answer spends the one sent, and a spent one is refused with 400."""
        endpoint = {"live": "ref-old", "sent": []}

        def urlopen(request, timeout=None):
            sent = parse_qs(request.data.decode())["refresh_token"][0]
            endpoint["sent"].append(sent)
            if sent != endpoint["live"]:
                raise self._refusal(400)
            answer = answers[len(endpoint["sent"]) - 1]
            if isinstance(answer.get("refresh_token"), str) and answer["refresh_token"].strip():
                endpoint["live"] = answer["refresh_token"]
            return self._answer(answer)

        endpoint["urlopen"] = urlopen
        return endpoint

    _NEXT_GRANT = {"access_token": "tok-3", "refresh_token": "ref-3", "expires_in": 7200}

    @pytest.mark.parametrize(
        "access",
        [
            {},
            {"access_token": ""},
            {"access_token": "   "},
            {"access_token": None},
            {"access_token": 1},
        ],
        ids=["omitted", "blank", "spaces", "null", "not-text"],
    )
    def test_a_refresh_token_delivered_with_no_usable_access_token_is_kept(self, access):
        """Bosch has spent the token sent, so dropping this one leaves the next
        call to send a dead token and be told to re-authorise."""
        self._write(expiry=0)
        endpoint = self._rotating({**access, "refresh_token": "ref-2"}, self._NEXT_GRANT)
        with patch("urllib.request.urlopen", endpoint["urlopen"]):
            with pytest.raises(auth_module.RefreshNetworkError, match="no usable token"):
                auth_module.refresh_token()
            stored = self._stored()
            assert (stored["access_token"], stored["refresh_token"], stored["expiry"]) == (
                "",
                "ref-2",
                0,
            )
            assert stored["client_id"] == CLIENT_ID
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old", "ref-2"]
        assert self._stored()["refresh_token"] == "ref-3"

    def test_a_refresh_token_delivered_with_no_access_token_is_held_if_the_save_fails(self):
        self._write(expiry=0)
        endpoint = self._rotating({"refresh_token": "ref-2"}, self._NEXT_GRANT)
        with self._fail_saves(2), patch("urllib.request.urlopen", endpoint["urlopen"]):
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()
            assert auth_module._unsaved is not None
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old", "ref-2"]
        assert self._stored()["refresh_token"] == "ref-3"

    def test_a_refresh_token_delivered_to_a_held_grant_replaces_it(self):
        """The held grant's refresh token has just been spent."""
        self._write(expiry=0)
        endpoint = self._rotating(
            dict(self._REFRESHED, expires_in=0), {"refresh_token": "ref-2"}, self._NEXT_GRANT
        )
        with self._fail_saves(3), patch("urllib.request.urlopen", endpoint["urlopen"]):
            auth_module.refresh_token()
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old", "ref-new", "ref-2"]

    def test_a_later_grant_wins_over_a_refresh_token_delivered_alone(self):
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            {"refresh_token": "ref-2"},
            access="tok-auth",
            refresh="ref-auth",
            expiry=self._now() + 3600,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-auth"
        assert self._stored()["refresh_token"] == "ref-auth"

    @pytest.mark.parametrize(
        "lifetime",
        ["7200", None, [], {}, float("inf"), float("nan"), 0, -1, True, 10**400],
        ids=[
            "text",
            "null",
            "list",
            "object",
            "infinite",
            "nan",
            "zero",
            "negative",
            "true",
            "too-big-for-a-float",
        ],
    )
    def test_a_lifetime_that_is_not_a_finite_positive_number_means_already_expired(self, lifetime):
        """Raising here loses a good rotated token, and one that never expires
        is never refreshed."""
        self._write(expiry=0)
        endpoint = self._rotating(
            {"access_token": "tok-2", "refresh_token": "ref-2", "expires_in": lifetime},
            self._NEXT_GRANT,
        )
        with patch("urllib.request.urlopen", endpoint["urlopen"]):
            assert auth_module.refresh_token() == "tok-2"
            stored = self._stored()
            assert (stored["access_token"], stored["refresh_token"], stored["expiry"]) == (
                "tok-2",
                "ref-2",
                0,
            )
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old", "ref-2"]

    @pytest.mark.parametrize(
        "answer,seconds",
        [
            ({"expires_in": 3600}, 3600),
            ({"expires_in": 0.5}, 0.5),
            ({}, 7200),
            ({"expires_in": 1e300}, 1e300),
        ],
        ids=["whole", "fraction", "absent-keeps-the-default", "huge-but-finite"],
    )
    def test_a_usable_lifetime_sets_the_expiry(self, answer, seconds):
        self._write(expiry=0)
        grant = {"access_token": "tok-2", "refresh_token": "ref-2", **answer}
        before = self._now()
        with patch("urllib.request.urlopen", lambda *a, **k: self._answer(grant)):
            auth_module.refresh_token()
        assert before + seconds <= self._stored()["expiry"] <= self._now() + seconds

    _TOKEN_TEXT = pytest.mark.parametrize(
        "token",
        ["eyJh.bG-c_i~O/+=", "eyJh.bG-c i~O/+="],
        ids=["jwt-shaped", "inner-space"],
    )

    @_TOKEN_TEXT
    def test_a_stored_access_token_of_valid_token_text_is_used_without_a_refresh(self, token):
        self._write(access=token, expiry=self._now() + 3600)
        assert auth_module.refresh_token() == token

    @_TOKEN_TEXT
    def test_a_stored_access_token_of_valid_token_text_is_sent_on_a_request(
        self, token, monkeypatch
    ):
        from bosch_flow_mcp import api

        monkeypatch.setattr(api, "refresh_token", auth_module.refresh_token)
        self._write(access=token, expiry=self._now() + 3600)
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request.get_header("Authorization"))
            return self._answer({})

        with patch("urllib.request.urlopen", urlopen):
            api.get("/v1/fictional")
        assert sent == [f"Bearer {token}"]

    @_TOKEN_TEXT
    def test_a_stored_refresh_token_of_valid_token_text_is_sent(self, token):
        self._write(refresh=token, expiry=0)
        endpoint = []

        def urlopen(request, timeout=None):
            endpoint.append(parse_qs(request.data.decode())["refresh_token"][0])
            return self._answer(self._REFRESHED)

        with patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
        assert endpoint == [token]

    @_TOKEN_TEXT
    def test_an_answer_of_valid_token_text_is_saved(self, token):
        self._write(expiry=0)
        answer = {"access_token": token, "refresh_token": token, "expires_in": 7200}
        with patch("urllib.request.urlopen", lambda *a, **k: self._answer(answer)):
            assert auth_module.refresh_token() == token
        assert (self._stored()["access_token"], self._stored()["refresh_token"]) == (token, token)

    @pytest.mark.parametrize("stored", ["", 12345], ids=["blank", "not-text"])
    def test_a_grant_saved_while_an_unusable_access_token_was_refreshed_wins(self, stored):
        """A token that cannot be sent is refreshed whatever its expiry says, so
        a later grant can expire before it does."""
        self._write(access=stored, expiry=self._now() + 7200)
        urlopen = self._urlopen_writing(
            self._REFRESHED, access="tok-auth", refresh="ref-auth", expiry=self._now() + 3600
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-auth"
        assert self._stored()["refresh_token"] == "ref-auth"

    _UNSENDABLE = pytest.mark.parametrize(
        "stored",
        [12345, "   ", "a\nb", "a\tb", "a\rb", "a\x00b", "a\x7fb", "tök", None],
        ids=[
            "not-text",
            "spaces",
            "newline",
            "tab",
            "return",
            "nul",
            "delete",
            "not-ascii",
            "null",
        ],
    )

    @_UNSENDABLE
    def test_a_stored_access_token_that_cannot_be_sent_is_refreshed(self, stored):
        """Sent, it comes back 401, which says to re-authorise with the
        credentials never judged."""
        self._write(access=stored, refresh="ref-old", expiry=self._now() + 3600)
        endpoint = self._rotating(self._NEXT_GRANT)
        with patch("urllib.request.urlopen", endpoint["urlopen"]):
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old"]

    @_UNSENDABLE
    def test_a_stored_refresh_token_that_cannot_be_sent_is_not_sent(self, stored):
        self._write(refresh=stored, expiry=0)
        with pytest.raises(auth_module.TokenRefused, match="no refresh token"):
            auth_module.refresh_token()

    @pytest.mark.parametrize(
        "stored",
        [float("inf"), float("nan"), 10**400],
        ids=["infinite", "nan", "too-big-for-a-float"],
    )
    def test_a_stored_expiry_that_is_not_finite_counts_as_expired(self, stored):
        """A release that did not check the lifetime can have written one, and
        the token would never refresh."""
        self._write(expiry=stored)
        endpoint = self._rotating(self._NEXT_GRANT)
        with patch("urllib.request.urlopen", endpoint["urlopen"]):
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old"]

    def test_a_copy_with_an_infinite_expiry_is_not_a_later_grant(self):
        self._write(expiry=0)
        urlopen = self._urlopen_writing(
            self._REFRESHED, access="tok-copy", refresh="ref-copy", expiry=float("inf")
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth_module.refresh_token() == "tok-new"
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_token_file_left_with_a_blank_access_token_is_refreshed(self):
        """What a delivered refresh token is saved as. Only a file with no
        access_token key at all is refused."""
        self._write(access="", refresh="ref-old", expiry=self._now() + 3600)
        endpoint = self._rotating(self._NEXT_GRANT)
        with patch("urllib.request.urlopen", endpoint["urlopen"]):
            assert auth_module.refresh_token() == "tok-3"
        assert endpoint["sent"] == ["ref-old"]

    def test_a_refresh_token_delivered_with_no_access_token_logs_one_warning(self, caplog):
        self._write(expiry=0)
        endpoint = self._rotating({"refresh_token": "ref-2-SECRET"})
        with (
            patch("urllib.request.urlopen", endpoint["urlopen"]),
            caplog.at_level(logging.DEBUG, logger="bosch_flow_mcp.auth"),
            pytest.raises(auth_module.RefreshNetworkError),
        ):
            auth_module.refresh_token()
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.WARNING, "Token refresh answered 200 with no usable token")
        ]

    def test_a_held_grant_refreshed_with_no_new_refresh_token_keeps_its_own(self):
        """The file's is the one the held grant replaced, and may already be spent."""
        self._write(expiry=0)
        answers = [
            dict(self._REFRESHED, expires_in=0),
            {"access_token": "tok-long", "expires_in": 7200},
        ]
        with (
            self._fail_saves(2),
            patch("urllib.request.urlopen", lambda *a, **k: self._answer(answers.pop(0))),
        ):
            auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-long"
        assert self._stored()["refresh_token"] == "ref-new"

    def test_a_200_with_no_usable_token_leaves_a_held_grant_held(self):
        """It judges nothing, so the held grant's refresh token may still be
        live, and the file's may already be spent."""
        self._write(expiry=0)
        sent = []
        answers = [
            dict(self._REFRESHED, expires_in=0),
            {"token_type": "Bearer"},
            dict(self._REFRESHED, access_token="tok-long", refresh_token="ref-long"),
        ]

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["refresh_token"][0])
            return self._answer(answers.pop(0))

        with self._fail_saves(3), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()
            assert auth_module.refresh_token() == "tok-long"
        assert sent == ["ref-old", "ref-new", "ref-new"]
        assert self._stored()["refresh_token"] == "ref-long"

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
        """A `_save_json` that raises OSError for its first `times` calls.

        `truncate` empties the file as it fails, which this package's saves
        never do but a writer outside it can.
        """
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

    def test_a_held_grant_that_expires_is_refreshed_and_saved_over_an_empty_file(self):
        """The held grant is the only live copy, so an empty file, which holds
        nothing newer, is repaired by the next refresh."""
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

    def _hold_then_refuse(self):
        """An urlopen that grants a token expiring at once, then refuses."""
        answers = [self._answer(dict(self._REFRESHED, expires_in=0)), self._refusal(400)]
        posts = []

        def urlopen(request, timeout=None):
            posts.append(request)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return urlopen, posts

    def test_a_refused_held_grant_is_a_refusal(self):
        """Otherwise its refusal reads as a change to the file, which still
        holds the token it replaced, and every call refreshes with a dead
        token."""
        self._write(expiry=0)
        urlopen, _ = self._hold_then_refuse()
        with self._fail_saves(9), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            with pytest.raises(auth_module.TokenRefused):
                auth_module.refresh_token()
        assert auth_module._unsaved is None

    def test_a_refused_held_grant_with_the_file_unreadable_is_settled_by_the_next_call(self):
        """Unreadable may be another writer part-way through, so the refusal is
        not a verdict; the held grant is dead either way, and the next call
        reads the file without sending anything."""
        self._write(expiry=0)
        urlopen, posts = self._hold_then_refuse()
        with self._fail_saves(9, truncate=True), patch("urllib.request.urlopen", urlopen):
            auth_module.refresh_token()
            with pytest.raises(auth_module.RefreshNetworkError):
                auth_module.refresh_token()
            assert auth_module._unsaved is None
            with pytest.raises(auth_module.TokenRefused):
                auth_module.refresh_token()
        assert len(posts) == 2

    def test_a_failed_save_is_reported_once_while_it_is_retried(self, caplog):
        """The retry runs on every request, and a sync makes many."""
        self._write(expiry=0)
        with (
            self._fail_saves(9),
            patch("urllib.request.urlopen", lambda *a, **k: self._answer(self._REFRESHED)),
            caplog.at_level(logging.WARNING, logger="bosch_flow_mcp.auth"),
        ):
            for _ in range(4):
                auth_module.refresh_token()
        assert len([r for r in caplog.records if "save" in r.getMessage()]) == 1

    def test_a_refusal_while_the_file_is_unreadable_is_not_a_refusal(self):
        """An unreadable file may be another writer part-way through, so the
        refusal is not a verdict; telling the user to re-authorise over it
        would be wrong whenever that writer then finishes."""
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
        """BadStatusLine is not an OSError, so a catch limited to OSError would miss it."""
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

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"token_type": "Bearer"}',
            b'{"access_token": ""}',
            b'{"access_token": "   "}',
            b'{"access_token": "a\\nb"}',
            b'{"access_token": "t\\u00f6k"}',
            b'{"access_token": null}',
            b'{"access_token": 12345}',
            b'{"access_token": "", "refresh_token": ""}',
            b'{"refresh_token": "   "}',
            b'{"refresh_token": "a\\nb"}',
            b'{"refresh_token": "t\\u00f6k"}',
            b'{"refresh_token": 12345}',
            b'["not", "an", "object"]',
            b"42",
        ],
        ids=[
            "no-key",
            "blank",
            "spaces",
            "newline",
            "not-ascii",
            "null",
            "not-text",
            "both-blank",
            "refresh-spaces",
            "refresh-newline",
            "refresh-not-ascii",
            "refresh-not-text",
            "list",
            "scalar",
        ],
    )
    def test_a_200_with_no_usable_token_is_not_a_refusal(self, payload, tmp_path, monkeypatch):
        """A 200 judges nothing, and re-authorising in answer to it spends a
        refresh token that may still work. Saved, an unusable token comes back
        401, which says the same thing with the credentials still unjudged."""
        with pytest.raises(
            auth_module.RefreshNetworkError, match="^Bosch returned no usable token"
        ):
            self._refresh_with_urlopen(self._responding(payload), tmp_path, monkeypatch)
        stored = json.loads((tmp_path / "bosch_tokens.json").read_text())
        assert (stored["access_token"], stored["refresh_token"]) == ("a", "r")

    def test_a_200_with_no_usable_token_is_logged_once_in_fixed_text(
        self, tmp_path, monkeypatch, caplog
    ):
        """Nothing else records why a refresh keeps failing, and the body can
        carry anything, so none of it is logged."""
        payload = b'{"error_description": "account ACC-SECRET-42 is not provisioned"}'
        with (
            caplog.at_level(logging.DEBUG, logger="bosch_flow_mcp.auth"),
            pytest.raises(auth_module.RefreshNetworkError),
        ):
            self._refresh_with_urlopen(self._responding(payload), tmp_path, monkeypatch)
        assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
            (logging.WARNING, "Token refresh answered 200 with no usable token")
        ]

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
    for routing. sync_tools calls one before its try block, so a corrupt token
    file raising there would stop the whole sync.
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
    """Some of the sites that find the credentials unusable, pinned to the right class."""

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
        """A file with no access token is refused, even with a refresh token in it.

        Nothing in this repo writes a file without the key, so this one comes
        from a writer outside it. A blank value is a different file, which is
        refreshed. A half-written file fails to parse and is refused before this
        check. The refusal is strict: the check in _refresh_token runs before
        any refresh, so a live refresh token in the file goes unused, and
        `_no_network` fails this test if a refresh is attempted. Letting such a
        file refresh means removing that check.
        """
        path = tmp_path / "bosch_tokens.json"
        path.write_text(
            json.dumps({"refresh_token": "r", "expiry": 4102444800, "client_id": CLIENT_ID})
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()


class TestTheTokenFileIsReplacedInOneStep:
    """A reader sees the old file or the new one, never part of either.

    A truncate-and-write save shows every reader an empty or partial file for
    the length of the write, and a failure part-way leaves it that way, after
    the refresh token it held may already be spent.
    """

    def test_the_target_holds_the_whole_old_file_until_the_new_one_replaces_it(self, tmp_path):
        import os as os_module

        target = tmp_path / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')
        seen = []
        real_replace = os_module.replace

        def spy(src, dst):
            seen.append((target.read_text(), json.loads(open(src).read())))
            return real_replace(src, dst)

        with patch.object(auth_module.os, "replace", spy):
            auth_module._save_json(target, {"refresh_token": "new"})

        assert seen == [('{"refresh_token": "old"}', {"refresh_token": "new"})]
        assert json.loads(target.read_text()) == {"refresh_token": "new"}

    @staticmethod
    def _writes_that(action):
        """An `os.fdopen` whose handle calls `action(fd)` before each write."""
        import os as os_module

        real_fdopen = os_module.fdopen

        class _Handle:
            def __init__(self, handle):
                self._handle = handle

            def __enter__(self):
                self._handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self._handle.__exit__(*exc)

            def write(self, text):
                action(self._handle.fileno())
                return self._handle.write(text)

            def __getattr__(self, name):
                return getattr(self._handle, name)

        return patch.object(
            auth_module.os, "fdopen", lambda fd, *a, **k: _Handle(real_fdopen(fd, *a, **k))
        )

    @pytest.fixture
    def folder(self, tmp_path):
        """A directory of the test's own, resolved: conftest keeps a file in
        tmp_path, and the save follows symlinks, which on macOS the temp
        directory sits behind."""
        folder = tmp_path.resolve() / "config"
        folder.mkdir()
        return folder

    @pytest.mark.parametrize(
        "failure",
        [OSError(28, "No space left on device"), KeyboardInterrupt()],
        ids=["disk-full", "interrupted"],
    )
    def test_a_failed_write_leaves_the_old_file_and_nothing_else(self, folder, failure):
        """The temp file holds the new refresh token, so one left behind is a
        live credential lying in the config directory."""
        target = folder / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')

        def fail(fd):
            raise failure

        with self._writes_that(fail), pytest.raises(type(failure)):
            auth_module._save_json(target, {"refresh_token": "new"})

        assert target.read_text() == '{"refresh_token": "old"}'
        assert list(target.parent.iterdir()) == [target]

    def test_a_failed_replace_leaves_the_old_file_and_nothing_else(self, folder):
        target = folder / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')

        def refuse(src, dst):
            raise OSError(13, "Permission denied")

        with patch.object(auth_module.os, "replace", refuse), pytest.raises(OSError):
            auth_module._save_json(target, {"refresh_token": "new"})

        assert target.read_text() == '{"refresh_token": "old"}'
        assert list(target.parent.iterdir()) == [target]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
    def test_the_new_file_is_owner_only_before_the_token_is_written(self, tmp_path):
        """Checked at the write: a chmod at any point after it gives the same
        final mode while leaving the token readable in between."""
        import os as os_module

        seen = []
        target = tmp_path / "bosch_tokens.json"
        with self._writes_that(lambda fd: seen.append(oct(os_module.fstat(fd).st_mode & 0o777))):
            auth_module._save_json(target, {"refresh_token": "fictional"})

        assert seen == ["0o600"]

    def test_each_save_publishes_its_own_temp_file_from_the_same_directory(self, folder):
        """A fixed temp name lets one writer publish another's half-written
        file, and a rename from another directory is not atomic."""
        import os as os_module
        from pathlib import Path

        published = []
        real_replace = os_module.replace

        def spy(src, dst):
            published.append(Path(src))
            return real_replace(src, dst)

        target = folder / "bosch_tokens.json"
        with patch.object(auth_module.os, "replace", spy):
            auth_module._save_json(target, {"refresh_token": "a"})
            auth_module._save_json(target, {"refresh_token": "b"})

        assert len(set(published)) == 2
        assert all(p.parent == folder and p != target for p in published)

    def test_a_failed_cleanup_does_not_hide_why_the_save_failed(self, folder):
        """A scanner can block the unlink as well as the replace on Windows."""

        def refuse_replace(src, dst):
            raise OSError(28, "No space left on device")

        def refuse_unlink(path):
            raise PermissionError(13, "Access is denied")

        with (
            patch.object(auth_module.os, "replace", refuse_replace),
            patch.object(auth_module.os, "unlink", refuse_unlink),
            pytest.raises(OSError) as raised,
        ):
            auth_module._save_json(folder / "bosch_tokens.json", {"refresh_token": "new"})
        assert raised.value.errno == 28

    def test_the_new_file_is_synced_before_it_replaces_the_old_one(self, folder):
        """Unsynced, a crash soon after the rename can leave an empty file on
        some filesystems, after the refresh token it replaced was spent."""
        import os as os_module

        data = {"refresh_token": "new"}
        events = []
        real_fsync, real_replace = os_module.fsync, os_module.replace

        def fsync(fd):
            # The whole file has to be in it by now, or the sync covers less.
            events.append(("fsync", os_module.fstat(fd).st_size))
            return real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", None))
            return real_replace(src, dst)

        target = folder / "bosch_tokens.json"
        with (
            patch.object(auth_module.os, "fsync", fsync),
            patch.object(auth_module.os, "replace", replace),
        ):
            auth_module._save_json(target, data)

        # Against the saved file rather than the JSON: Windows writes `\r\n`.
        assert events == [("fsync", target.stat().st_size), ("replace", None)]
        assert target.stat().st_size > 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
    def test_a_loose_existing_file_is_replaced_by_an_owner_only_one(self, folder):
        """Files written by a release that set no mode can be world-readable."""
        import os as os_module

        target = folder / "bosch_tokens.json"
        target.write_text("{}")
        os_module.chmod(target, 0o644)
        auth_module._save_json(target, {"refresh_token": "fictional"})
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_a_symlinked_token_file_is_saved_where_the_link_points(self, folder):
        """Replacing the link would leave its target, which anything else using
        the real path reads, on a refresh token already spent."""
        import os as os_module
        from pathlib import Path

        real = folder / "secrets" / "bosch_tokens.json"
        real.parent.mkdir()
        real.write_text('{"refresh_token": "old"}')
        link = folder / "bosch_tokens.json"
        link.symlink_to(real)
        published = []
        real_replace = os_module.replace

        def spy(src, dst):
            published.append(Path(src).parent)
            return real_replace(src, dst)

        with patch.object(auth_module.os, "replace", spy):
            auth_module._save_json(link, {"refresh_token": "new"})

        assert link.is_symlink()
        assert json.loads(real.read_text()) == {"refresh_token": "new"}
        # Beside the real file, since a rename cannot cross filesystems.
        assert published == [real.parent]

    @pytest.mark.skipif(sys.platform != "win32", reason="the sharing rule retried is Windows'")
    def test_a_save_waits_out_a_reader_holding_the_file_open(self, folder):
        """The real refusal, not a stand-in for it: a reader holds the file
        while the save runs, and lets go within the retries."""
        import os as os_module
        import threading

        refused = []
        real_replace = os_module.replace

        def watch(src, dst):
            try:
                return real_replace(src, dst)
            except PermissionError:
                refused.append(src)
                raise

        target = folder / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')
        reader = open(target)
        timer = threading.Timer(0.1, reader.close)
        timer.start()
        try:
            with patch.object(auth_module.os, "replace", watch):
                auth_module._save_json(target, {"refresh_token": "new"})
        finally:
            timer.join()
            reader.close()
        assert json.loads(target.read_text()) == {"refresh_token": "new"}
        # Without a refusal this proves nothing about the retry.
        assert refused, "Windows replaced a file another process had open"

    def test_windows_retries_a_replace_another_reader_is_blocking(self, folder):
        """Windows refuses to replace a file another process has open, and a
        server sharing the file opens it on every request; `auth` cannot redo
        the save once the authorisation code is spent."""
        import os as os_module

        attempts = []
        real_replace = os_module.replace

        def busy_twice(src, dst):
            attempts.append(src)
            if len(attempts) <= 2:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        target = folder / "bosch_tokens.json"
        with (
            patch.object(auth_module.sys, "platform", "win32"),
            patch.object(auth_module.os, "replace", busy_twice),
            patch.object(auth_module.time, "sleep") as sleep,
        ):
            auth_module._save_json(target, {"refresh_token": "new"})

        assert len(attempts) == 3
        assert json.loads(target.read_text()) == {"refresh_token": "new"}
        # Back-to-back retries would be over before a reader let go.
        delays = [call.args[0] for call in sleep.call_args_list]
        assert len(delays) == 2 and 0 < delays[0] < delays[1], delays

    @pytest.mark.parametrize("platform,expected", [("linux", 1), ("win32", 5)])
    def test_a_refused_replace_is_not_retried_elsewhere_or_forever(
        self, folder, platform, expected
    ):
        """Off Windows the refusal is a real permission fault, and on Windows a
        file that stays locked has to fail rather than hang the server."""
        attempts = []

        def refuse(src, dst):
            attempts.append(src)
            raise PermissionError(13, "Access is denied")

        with (
            patch.object(auth_module.sys, "platform", platform),
            patch.object(auth_module.os, "replace", refuse),
            patch.object(auth_module.time, "sleep"),
            pytest.raises(PermissionError),
        ):
            auth_module._save_json(folder / "bosch_tokens.json", {"refresh_token": "new"})
        assert len(attempts) == expected
        assert list(folder.iterdir()) == []


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
        """Address reuse is asked for off Windows and not on it.

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

        A port that is already held has to end in advice rather than a traceback.
        It has to end there before an authorisation page is opened onto a
        redirect with nowhere to land.
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

    @pytest.mark.parametrize(
        "refresh",
        [
            {},
            {"refresh_token": None},
            {"refresh_token": ""},
            {"refresh_token": "   "},
            {"refresh_token": "a\nb"},
            {"refresh_token": "a\tb"},
            {"refresh_token": "a\rb"},
            {"refresh_token": "a\x00b"},
            {"refresh_token": "a\x7fb"},
            {"refresh_token": "tök"},
            {"refresh_token": 12345},
        ],
        ids=[
            "omitted",
            "null",
            "blank",
            "spaces",
            "newline",
            "tab",
            "return",
            "nul",
            "delete",
            "not-ascii",
            "not-text",
        ],
    )
    @pytest.mark.parametrize(
        "existing",
        [
            None,
            "{not json",
            json.dumps({"refresh_token": "rt-other", "client_id": "cid"}),
            json.dumps({"refresh_token": "rt-other"}),
            json.dumps({"refresh_token": "rt-other", "client_id": "other"}),
        ],
        ids=["no-file", "unreadable", "same-client", "file-names-no-client", "other-client"],
    )
    def test_a_sign_in_with_no_usable_refresh_token_saves_none(
        self, refresh, existing, monkeypatch, capsys, tmp_path
    ):
        """The same client is not the same account. A stored refresh token kept
        beside this sign-in's access token may be another account's, and once
        the access token expires the server silently serves that account's data."""
        tokens_path = tmp_path / "bosch_tokens.json"
        if existing is not None:
            tokens_path.write_text(existing)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        monkeypatch.setattr(auth_module, "_get_client_id", lambda: "cid")
        body = json.dumps({"access_token": "at-new", **refresh}).encode()
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", _responding(body))

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        stored = json.loads(tokens_path.read_text())
        assert (stored["access_token"], stored["refresh_token"]) == ("at-new", "")
        assert stored["client_id"] == "cid"
        out = capsys.readouterr().out
        assert "Auth complete" in out
        assert auth_module.NO_REFRESH_TOKEN_MSG in out

    @pytest.mark.parametrize(
        "lifetime,keeps_expiry",
        [("7200", False), (None, False), ([], False), (float("inf"), False), (3600, True)],
        ids=["text", "null", "list", "infinite", "usable"],
    )
    def test_a_lifetime_that_cannot_be_used_saves_the_sign_in_as_already_expired(
        self, lifetime, keeps_expiry, monkeypatch, capsys, tmp_path
    ):
        """The authorisation code is spent by now, so a traceback here costs the
        whole browser flow, and the next call can refresh instead."""
        tokens_path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        grant = {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": lifetime}
        monkeypatch.setattr(
            auth_module.urllib.request, "urlopen", _responding(json.dumps(grant).encode())
        )

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        stored = json.loads(tokens_path.read_text())
        assert (stored["access_token"], stored["refresh_token"]) == ("at-new", "rt-new")
        assert (stored["expiry"] > 0) is keeps_expiry
        assert "Auth complete" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "lifetime,seconds",
        [("7200", 7200), (None, 7200), ([], 7200), (float("inf"), 7200), (3600, 3600)],
        ids=["text", "null", "list", "infinite", "usable"],
    )
    def test_a_sign_in_with_nothing_to_refresh_from_keeps_a_usable_expiry(
        self, lifetime, seconds, monkeypatch, tmp_path
    ):
        """Saved as already expired, the good access token is never used: the
        next call finds it expired, with no refresh token to replace it."""
        tokens_path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        grant = {"access_token": "at-new", "expires_in": lifetime}
        monkeypatch.setattr(
            auth_module.urllib.request, "urlopen", _responding(json.dumps(grant).encode())
        )
        before = datetime.now(timezone.utc).timestamp()

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        expiry = json.loads(tokens_path.read_text())["expiry"]
        assert before + seconds <= expiry <= datetime.now(timezone.utc).timestamp() + seconds

    def test_a_sign_in_with_no_lifetime_keeps_the_default(self, monkeypatch, tmp_path):
        tokens_path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        monkeypatch.setattr(
            auth_module.urllib.request, "urlopen", _responding(b'{"access_token": "at-new"}')
        )
        before = datetime.now(timezone.utc).timestamp()

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        stored = json.loads(tokens_path.read_text())
        assert stored["expiry"] >= before + 7200

    @pytest.mark.parametrize(
        "token",
        ["eyJh.bG-c_i~O/+=", "eyJh.bG-c i~O/+="],
        ids=["jwt-shaped", "inner-space"],
    )
    def test_a_sign_in_of_valid_token_text_is_saved(self, token, monkeypatch, tmp_path):
        tokens_path = tmp_path / "bosch_tokens.json"
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        body = json.dumps({"access_token": token, "refresh_token": token}).encode()
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", _responding(body))

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        stored = json.loads(tokens_path.read_text())
        assert (stored["access_token"], stored["refresh_token"]) == (token, token)

    def test_a_refresh_token_in_the_answer_replaces_the_stored_one(
        self, monkeypatch, capsys, tmp_path
    ):
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text(json.dumps({"refresh_token": "rt-old", "client_id": "cid"}))
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        body = json.dumps({"access_token": "at-new", "refresh_token": "rt-new"}).encode()
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", _responding(body))

        auth_module._exchange_code("cid", "the-code", "the-verifier", "http://localhost:1/cb")

        assert json.loads(tokens_path.read_text())["refresh_token"] == "rt-new"
        assert auth_module.NO_REFRESH_TOKEN_MSG not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "token",
        ["", "   ", "a\nb", "a\tb", "a\rb", "a\x00b", "a\x7fb", "tök", None, 12345],
        ids=[
            "blank",
            "spaces",
            "newline",
            "tab",
            "return",
            "nul",
            "delete",
            "not-ascii",
            "null",
            "not-text",
        ],
    )
    def test_a_token_that_cannot_be_sent_fails_and_saves_nothing(
        self, token, monkeypatch, capsys, caplog, tmp_path
    ):
        """Saved, it reports success, replaces a working file, and every later
        call is refused."""
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text('{"access_token": "at-old", "refresh_token": "rt-old"}')
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)
        body = json.dumps({"access_token": token, "refresh_token": "rt-SECRET-VALUE"}).encode()

        self._run(
            monkeypatch,
            _responding(body),
            capsys,
            caplog,
            "Error: the token response carried no access token.",
        )
        assert tokens_path.read_text() == '{"access_token": "at-old", "refresh_token": "rt-old"}'

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
        # `42` has no `.get`, so the shape check needs the isinstance guard to
        # reach its own message.
        self._run(
            monkeypatch,
            _responding(b"42"),
            capsys,
            caplog,
            "Error: the token response carried no access token.",
        )

    def test_a_save_that_fails_reports_its_type_not_its_paths(self, monkeypatch, capsys, caplog):
        """The save needs write access to the directory and, on Windows, a file
        nobody holds open, and an OSError's text names the install's paths."""
        body = json.dumps(
            {"access_token": "at-SECRET-VALUE", "refresh_token": "rt-SECRET-VALUE"}
        ).encode()

        def refuse(path, data):
            raise PermissionError(13, "Permission denied", "/home/someone/config/.tmpfile")

        monkeypatch.setattr(auth_module, "_save_json", refuse)
        self._run(
            monkeypatch,
            _responding(body),
            capsys,
            caplog,
            "Error: the tokens could not be saved (PermissionError).",
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
