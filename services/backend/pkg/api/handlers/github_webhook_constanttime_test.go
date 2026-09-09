package handlers

// A source-level guard for a property no functional test can hold.

import (
	"os"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestSignatureComparisonIsConstantTime reads the source and asserts the
// comparison uses hmac.Equal.
//
// WHY THIS IS A SOURCE ASSERTION AND NOT A BEHAVIOURAL ONE. The plan's
// verification step says: "replace hmac.Equal with == and confirm a test
// fails." That mutation was run, and NO test failed — correctly, because
// `==` and `hmac.Equal` are functionally identical. They return the same
// answer for every input. The only difference is how long they take: `==`
// on byte slices short-circuits at the first differing byte, and that
// timing difference is enough to forge a signature one byte at a time
// against an endpoint anyone can reach.
//
// A timing test would be the behavioural equivalent, and it would be
// flaky in CI on a shared runner — a test that fails randomly gets
// deleted, and then the property is unguarded for real. So this reads the
// file instead. It is a weaker kind of test, and it is the strongest one
// available for this property.
func TestSignatureComparisonIsConstantTime(t *testing.T) {
	src, err := os.ReadFile("github_webhook.go")
	require.NoError(t, err)
	body := string(src)

	require.Contains(t, body, "hmac.Equal(",
		"the webhook signature comparison must use hmac.Equal; a plain == leaks how "+
			"many leading bytes matched, which is enough to forge a signature byte by byte")

	// And nothing sneaks a direct comparison of the two digests back in.
	for _, forbidden := range []string{
		"want == mac.Sum",
		"string(want) == string(",
		`header == "sha256="`,
	} {
		require.NotContains(t, body, forbidden,
			"a non-constant-time signature comparison reappeared: %s", forbidden)
	}
}
