"""Unit tests for the two PURE functions of the job state machine.

`next_run_after_delay` lives in `workers.jobs.backoff`; `sanitize_error`
lives in `workers.jobs.transitions` beside the `fail` transition that
writes its output. Both are tested here rather than in
`tests/isolation/test_job_transitions.py` because neither touches a
database, and a test that needs a container to check a regular expression
is a test people stop running.

Everything else in the state machine is proven against a real PostgreSQL
16 in `tests/isolation/test_job_transitions.py`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from workers.jobs.backoff import (
    BASE_DELAY,
    JITTER_FLOOR,
    MAX_DELAY,
    MULTIPLIER,
    next_run_after_delay,
)
from workers.jobs.transitions import MAX_ERROR_LENGTH, sanitize_error

# rng() is documented as returning a value in [0, 1), so these two are the
# ends of the interval the jitter can actually reach.
#
# The high end is 1 - 1e-6 rather than something nearer 1: `timedelta`
# resolves to MICROSECONDS, so 60s * (1 - 1e-12) rounds back to exactly 60s
# and "strictly below the cap" would be untestable at the top of the range.
_LOWEST = lambda: 0.0  # noqa: E731 - a stub, not a function worth a def
_HIGHEST = lambda: 1.0 - 1e-6  # noqa: E731


class TestNextRunAfterDelay:
    """The capped, jittered exponential of 21-CONTEXT's open question."""

    def test_first_attempt_waits_about_the_base_delay(self):
        assert next_run_after_delay(1, _HIGHEST) < BASE_DELAY
        assert next_run_after_delay(1, _HIGHEST) > BASE_DELAY * 0.999
        assert next_run_after_delay(1, _LOWEST) == BASE_DELAY * JITTER_FLOOR

    def test_it_is_exponential_before_the_cap(self):
        """Each attempt multiplies the previous one by MULTIPLIER.

        Measured at a FIXED rng so the comparison is about the exponential
        and not about two jitter draws.
        """
        at_one = next_run_after_delay(1, _LOWEST)
        at_two = next_run_after_delay(2, _LOWEST)
        at_three = next_run_after_delay(3, _LOWEST)

        assert at_two == at_one * MULTIPLIER
        assert at_three == at_two * MULTIPLIER

    def test_it_is_monotonic_before_the_cap(self):
        """Strictly increasing, even across the jitter's two extremes.

        The worst case for monotonicity is attempt n drawing the HIGHEST
        factor and attempt n+1 drawing the LOWEST. With a floor of 0.5 and
        a multiplier of 4 that is still an increase; with a floor of 0.5
        and a multiplier of 2 it would be a tie, which is why the two
        constants are checked against each other here rather than only the
        delays.
        """
        assert MULTIPLIER * JITTER_FLOOR > 1.0, (
            "a multiplier times the jitter floor of 1.0 or less lets a "
            "later attempt retry SOONER than an earlier one"
        )
        for attempts in (1, 2, 3):
            assert next_run_after_delay(attempts, _HIGHEST) < next_run_after_delay(
                attempts + 1, _LOWEST
            )

    def test_the_cap_holds(self):
        """⚠ The guard mutation E removes.

        Attempt 4 is 60s * 4**3 = 3840s uncapped, which is already past the
        hour; attempt 5 would be 15360s, and attempt 40 would be a number
        with 24 digits in it.
        """
        for attempts in (4, 5, 6, 40, 10_000):
            assert next_run_after_delay(attempts, _HIGHEST) < MAX_DELAY
            assert next_run_after_delay(attempts, _LOWEST) == MAX_DELAY * JITTER_FLOOR

    def test_the_jitter_stays_inside_its_interval(self):
        """Every draw lands in [cap/2, cap) -- never above, never at zero."""
        for attempts in (1, 2, 3, 4, 5):
            uncapped = BASE_DELAY.total_seconds() * MULTIPLIER ** (attempts - 1)
            capped = min(uncapped, MAX_DELAY.total_seconds())
            for _ in range(200):
                delay = next_run_after_delay(attempts)  # the real random
                assert timedelta(seconds=capped * JITTER_FLOOR) <= delay
                # `<=`, not `<`: a real draw within a microsecond of 1.0
                # rounds up to the cap at `timedelta`'s resolution. That
                # the top end is strictly below the cap is pinned
                # deterministically by `test_the_cap_holds` instead.
                assert delay <= timedelta(seconds=capped)

    def test_the_jitter_actually_varies(self):
        """A constant "jitter" would satisfy every bound above."""
        draws = {next_run_after_delay(3).total_seconds() for _ in range(50)}
        assert len(draws) > 40, f"the jitter is not random enough: {len(draws)} distinct draws"

    def test_a_degenerate_attempt_count_is_clamped_not_raised(self):
        """A failure write must never be lost to an argument check."""
        assert next_run_after_delay(0, _LOWEST) == next_run_after_delay(1, _LOWEST)
        assert next_run_after_delay(-7, _LOWEST) == next_run_after_delay(1, _LOWEST)

    def test_the_total_wait_for_five_attempts_is_about_81_minutes(self):
        """The number 21-CONTEXT's open question asked for, as a test.

        Four delays separate five attempts; the fifth attempt writes `dead`
        rather than a sixth `run_after`.
        """
        worst_case = sum(
            next_run_after_delay(n, _HIGHEST).total_seconds() for n in (1, 2, 3, 4)
        )
        assert 4859 < worst_case <= 4860
        assert abs(worst_case / 60 - 81) < 0.1


class TestSanitizeError:
    """⚠ `last_error` is persisted AND returned by 21-07's admin endpoint."""

    def test_it_keeps_the_exception_class(self):
        assert sanitize_error(ValueError("bad ref")) == "ValueError: bad ref"

    def test_it_redacts_an_installation_token(self):
        """`ghs_` -- the one a clone URL carries, and the likeliest leak.

        The shape is exactly what a failing `git clone` prints back.
        """
        error = RuntimeError(
            "clone failed: https://x-access-token:ghs_16C7e42F292c6912E7710c838347Ae178B4a@"
            "github.com/acme/widgets.git exited 128"
        )
        result = sanitize_error(error)
        assert "ghs_" not in result
        assert "16C7e42F292c6912E7710c838347Ae178B4a" not in result
        assert "[REDACTED]" in result
        # The surrounding context survives, which is the point of redacting
        # rather than dropping the message.
        assert "github.com/acme/widgets.git" in result
        assert "exited 128" in result

    def test_it_redacts_a_classic_personal_access_token(self):
        result = sanitize_error(Exception("token ghp_ABCDEFghijkl0123456789 rejected"))
        assert "ghp_" not in result
        assert "ABCDEFghijkl0123456789" not in result
        assert "rejected" in result

    def test_it_redacts_a_fine_grained_personal_access_token(self):
        """`github_pat_` carries an underscore INSIDE the secret part."""
        secret = "github_pat_11ABCDEFG0aBcDeFgHiJkL_MnOpQrStUvWxYz0123456789"
        result = sanitize_error(Exception(f"auth failed for {secret}"))
        assert "github_pat_" not in result
        assert "MnOpQrStUvWxYz0123456789" not in result
        assert "auth failed for [REDACTED]" == result.split(": ", 1)[1]

    def test_it_redacts_an_openai_key(self):
        """Both the classic and the `sk-proj-` project-scoped shapes."""
        for secret in ("sk-abcDEF123456789", "sk-proj-Ab_1-cD2eF3gH4iJ5kL6"):
            result = sanitize_error(Exception(f"401 from OpenAI using {secret}"))
            assert "sk-" not in result
            assert "[REDACTED]" in result
            assert "401 from OpenAI" in result

    def test_it_redacts_several_tokens_in_one_message(self):
        result = sanitize_error(
            Exception("ghs_aaaaaaaaaa then ghp_bbbbbbbbbb then sk-cccccccccc")
        )
        assert result.count("[REDACTED]") == 3
        for prefix in ("ghs_", "ghp_", "sk-"):
            assert prefix not in result

    def test_it_truncates(self):
        result = sanitize_error(Exception("x" * 50_000))
        assert len(result) == MAX_ERROR_LENGTH
        assert result.endswith("[truncated]")

    def test_a_token_straddling_the_truncation_point_is_still_redacted(self):
        """The boundary case, which is the one worth pinning.

        NOT a test of the redact-then-truncate ORDER: measured, the other
        order is equally safe, because a truncated token's prefix still
        matches the pattern. See `_sanitize_text`'s docstring and mutation
        X. What this holds is the property both orders must have -- that a
        token overlapping the cut leaves nothing behind.
        """
        secret = "ghs_" + "Z" * 300
        error = Exception("y" * (MAX_ERROR_LENGTH - 100) + secret + " trailing")
        result = sanitize_error(error)
        assert len(result) <= MAX_ERROR_LENGTH
        assert "ghs_" not in result
        assert "ZZZZ" not in result

    def test_a_message_with_no_token_is_untouched_apart_from_the_class(self):
        """Redaction must not eat ordinary text a human needs."""
        message = "parse error in pkg/skeleton/sk.go at line 12 (ghost branch)"
        result = sanitize_error(SyntaxError(message))
        assert result == f"SyntaxError: {message}"

    def test_the_cap_counts_characters_not_bytes(self):
        """Stated so the comment and the code cannot drift apart.

        2,000 characters of accented text is 3,974 UTF-8 bytes. That is
        deliberate — the column is `TEXT`, and the budget this bounds is
        what a human reads in 21-07's response, not what the row occupies.
        """
        result = sanitize_error(Exception("é" * 50_000))
        assert len(result) == MAX_ERROR_LENGTH
        assert len(result.encode("utf-8")) > MAX_ERROR_LENGTH

    # -- the prefixes PR #41's review measured surviving ------------------

    @pytest.mark.parametrize("prefix", ["gho_", "ghu_", "ghr_"])
    def test_it_redacts_githubs_other_documented_prefixes(self, prefix):
        """All six of GitHub's prefixes, not the three the first cut had.

        `gho_` (OAuth), `ghu_` (user-to-server) and `ghr_` (refresh) were
        measured passing through unredacted.
        """
        secret = prefix + "16C7e42F292c6912E7710c838347Ae178B4a"
        result = sanitize_error(Exception(f"request rejected for {secret}"))
        assert prefix not in result
        assert "16C7e42F" not in result
        assert "request rejected for [REDACTED]" == result.split(": ", 1)[1]

    def test_it_redacts_a_jwt(self):
        """⚠ Not reachable from this module today; 21-06 makes it so.

        Covers BOTH the GitHub App JWT that mints an installation token —
        which is what the claim-time installation read will hold — and
        Supabase's service-role key, which is also a JWT.
        """
        jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJpc3MiOiI0ODgwODY2IiwiZXhwIjoxNzAwMDAwMDAwfQ"
            ".sIgNaTuRe-part_0123456789"
        )
        result = sanitize_error(Exception(f"401 from GitHub with {jwt}"))
        assert "eyJ" not in result
        assert "sIgNaTuRe" not in result
        assert "401 from GitHub" in result

    def test_it_redacts_a_jwt_with_an_empty_signature(self):
        """An `alg: none` token is still a credential-shaped thing."""
        result = sanitize_error(Exception("token eyJhbGciOiJub25lIn0.eyJhIjoxfQ. seen"))
        assert "eyJ" not in result
        assert "[REDACTED]" in result

    def test_it_redacts_a_pem_private_key_block_whole(self):
        """One marker for the block, not one per base64 line."""
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxNotARealKey0123456789+/abcdef\n"
            "ghijklmnopqrstuvwxyz0123456789ABCDEF+/==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = sanitize_error(Exception(f"failed to sign: {pem} (app 4880866)"))
        assert "BEGIN" not in result
        assert "MIIEowIBAAKCAQEA" not in result
        assert result.count("[REDACTED]") == 1
        assert "app 4880866" in result

    def test_a_commit_sha_is_deliberately_not_redacted(self):
        """The 40-hex arm was considered and declined; this pins the choice.

        A bare 40-hex pattern would catch the App client secret AND every
        git commit SHA — which is legitimate, useful context in exactly
        these messages. Redacting it would blind 21-07 to which commit
        failed. If someone adds that arm, this test says why not to.
        """
        sha = "0c639171a2b3c4d5e6f708192a3b4c5d6e7f8091"
        result = sanitize_error(Exception(f"clone of {sha} failed"))
        assert sha in result

    # -- NUL: the finding that lost the whole write ----------------------

    def test_it_strips_a_nul_byte(self):
        """⚠ A storability fix, not a disclosure one.

        psycopg2 refuses a NUL in a text parameter CLIENT-SIDE, so without
        this the `ValueError` fires inside `require_tenant`, the failure
        write rolls back, and the job is stranded `running` with
        `last_error` NULL. Measured on the real container by PR #41's
        review; `test_a_nul_byte_in_an_error_still_records_the_failure`
        is the end-to-end half.
        """
        result = sanitize_error(Exception("parse failed:\x00\x00 binary content"))
        assert "\x00" not in result
        assert "parse failed: binary content" in result

    def test_a_nul_inside_a_token_does_not_split_the_redaction(self):
        """⚠ THIS ordering is load-bearing, unlike redact-before-truncate.

        A NUL is in none of the pattern's character classes, so it ENDS a
        match. Redacting before stripping leaves the tail of the token
        visible; stripping first hands the pattern one contiguous token.
        """
        result = sanitize_error(Exception("ghs_ABCDEF\x00GHIJKL0123 rejected"))
        assert "GHIJKL" not in result, (
            "the NUL split the token and its tail survived the redaction"
        )
        assert "ghs_" not in result
        assert "rejected" in result

    def test_it_strips_a_nul_only_and_keeps_every_other_control_character(self):
        """⚠ Exactly one character, measured — not "control characters".

        Probed on a scratch `postgres:16-alpine`: 0x01-0x1F, 0x7F and the
        C1 range all insert into a TEXT column and read back unchanged; only
        0x00 is refused. Stripping more would throw away the tab or newline
        that makes a subprocess dump readable.
        """
        result = sanitize_error(Exception("line one\n\tindented\r\x0b still here"))
        assert "\n" in result and "\t" in result and "\r" in result and "\x0b" in result
