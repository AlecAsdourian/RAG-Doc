package internalapi

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestParseLeaseBody pins the accepted shape exactly: one object, one
// `lease_owner` key, one non-empty string, nothing after it.
func TestParseLeaseBody(t *testing.T) {
	accepted := map[string]string{
		"the contract":             `{"lease_owner": "6f1c9a2e-3b4d-4c5e-8f6a-7b8c9d0e1f2a"}`,
		"whitespace around it":     "  \n {\"lease_owner\":\"abc\"} \n\t",
		"escapes in the value":     `{"lease_owner": "a-b"}`,
		"a value with inner space": `{"lease_owner": "a b"}`,
	}
	for name, body := range accepted {
		owner, ok := parseLeaseBody(strings.NewReader(body))
		require.True(t, ok, "%s: %s", name, body)
		require.NotEmpty(t, owner, name)
	}

	refused := map[string]string{
		"empty":                  ``,
		"not json":               `lease_owner=abc`,
		"an array":               `["abc"]`,
		"a bare string":          `"abc"`,
		"empty object":           `{}`,
		"empty value":            `{"lease_owner": ""}`,
		"blank value":            `{"lease_owner": "   "}`,
		"null value":             `{"lease_owner": null}`,
		"a number":               `{"lease_owner": 42}`,
		"a nested object":        `{"lease_owner": {"id": "abc"}}`,
		"an array value":         `{"lease_owner": ["abc"]}`,
		"an unknown field":       `{"lease_owner": "abc", "extra": 1}`,
		"an unknown field first": `{"extra": 1, "lease_owner": "abc"}`,
		"a duplicate key":        `{"lease_owner": "abc", "lease_owner": "def"}`,
		"trailing garbage":       `{"lease_owner": "abc"} garbage`,
		"a second object":        `{"lease_owner": "abc"} {"lease_owner": "def"}`,
		"a trailing comma":       `{"lease_owner": "abc",}`,
		"unterminated":           `{"lease_owner": "abc"`,
		"over the size limit":    `{"lease_owner": "` + strings.Repeat("a", maxBodyBytes) + `"}`,
	}
	for name, body := range refused {
		owner, ok := parseLeaseBody(strings.NewReader(body))
		require.False(t, ok, "%s must be refused: %s", name, body)
		require.Empty(t, owner, name)
	}
}
