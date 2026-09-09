package handlers

// Internal tests for pieces the HTTP-level suite cannot reach.

import (
	"testing"

	"github.com/stretchr/testify/require"
)

// TestNewStateToken_IsNotPredictable is the guard for the bug this
// function's own doc comment indicts `pkg/auth.generateSecureToken` for:
// ignoring rand.Read's error yields 32 zero bytes, which is a fully
// predictable CSRF token — that is, no CSRF token at all.
//
// Review found that returning a constant survived the entire suite, so
// the fix was untested while the comment claimed it mattered.
func TestNewStateToken_IsNotPredictable(t *testing.T) {
	const samples = 64
	seen := make(map[string]bool, samples)

	for i := 0; i < samples; i++ {
		token, err := newStateToken()
		require.NoError(t, err)
		require.NotEmpty(t, token)
		require.GreaterOrEqual(t, len(token), 40,
			"a 256-bit token is ~43 base64url characters; %q is too short to be one", token)
		require.False(t, seen[token], "newStateToken repeated a value: %q", token)
		seen[token] = true

		// A zero-byte token encodes to a run of 'A's. Catch that shape
		// specifically, since it is what a swallowed rand.Read error
		// produces.
		allSame := true
		for j := 1; j < len(token); j++ {
			if token[j] != token[0] {
				allSame = false
				break
			}
		}
		require.False(t, allSame, "token is a constant run of %q", token[0])
	}
	require.Len(t, seen, samples)
}
