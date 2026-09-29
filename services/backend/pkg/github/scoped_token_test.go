package github

// 22-04: repository-scoped tokens. Beside client_test.go rather than in it,
// because these tests share one fake (scopedFake) and one measured token
// shape, and client_test.go's fakes are per-test closures.

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// A token of the MEASURED shape. 20-02 recorded the live App's installation
// token as `ghs_` plus 383 characters and said not to assume a fixed length,
// so the tests below use this one and a short one.
var longToken = "ghs_" + strings.Repeat("Ab3xYz9Q", 48)[:383]

func TestLongTokenHasTheMeasuredShape(t *testing.T) {
	require.Len(t, longToken, 4+383)
	require.True(t, strings.HasPrefix(longToken, "ghs_"))
}

// absentField tells scopedFake to leave a field out of the mint reply.
const absentField = "ABSENT"

// scopedFake is a fake GitHub for RepositoryToken's two calls, recording
// what it was asked and answering what it is told to.
type scopedFake struct {
	mu         sync.Mutex
	mintBodies [][]byte
	mintAuth   []string
	lookupAuth []string
	lookupPath []string

	token         string
	repoID        int64
	repositories  string // JSON array override; "" => exactly the requested repository
	permissions   string // JSON object override; "" => contents:read + metadata:read
	mintStatus    int    // 0 => 201
	mintEchoAuth  bool   // echo the Authorization header into a failing body
	lookupStatus  int    // 0 => 200
	lookupEchoTok bool   // echo the bearer token into a failing body
}

func (f *scopedFake) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/access_tokens"):
			body, _ := io.ReadAll(r.Body)
			f.mintBodies = append(f.mintBodies, body)
			f.mintAuth = append(f.mintAuth, r.Header.Get("Authorization"))
			if f.mintStatus != 0 {
				w.WriteHeader(f.mintStatus)
				if f.mintEchoAuth {
					fmt.Fprintf(w, `<html>502 Bad Gateway. Request headers: authorization=%s</html>`,
						r.Header.Get("Authorization"))
				}
				return
			}
			repos := f.repositories
			if repos == "" {
				repos = fmt.Sprintf(`[{"id":%d,"full_name":"acme/widgets"}]`, f.repoID)
			}
			perms := f.permissions
			if perms == "" {
				perms = `{"contents":"read","metadata":"read"}`
			}
			// absentField makes the fake OMIT a field, which is a different
			// response from sending it empty, and the client tells them apart.
			fields := []string{
				fmt.Sprintf(`"token":%q`, f.token),
				fmt.Sprintf(`"expires_at":%q`, time.Now().Add(time.Hour).UTC().Format(time.RFC3339)),
				`"repository_selection":"selected"`,
			}
			if perms != absentField {
				fields = append(fields, `"permissions":`+perms)
			}
			if repos != absentField {
				fields = append(fields, `"repositories":`+repos)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			fmt.Fprintf(w, "{%s}", strings.Join(fields, ","))
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/repositories/"):
			f.lookupAuth = append(f.lookupAuth, r.Header.Get("Authorization"))
			f.lookupPath = append(f.lookupPath, r.URL.Path)
			if f.lookupStatus != 0 {
				w.WriteHeader(f.lookupStatus)
				if f.lookupEchoTok {
					fmt.Fprintf(w, `<html>WAF: rejected request bearing %s</html>`,
						strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
				}
				return
			}
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"id":%d,"name":"widgets","full_name":"acme/widgets-renamed","private":true,"default_branch":"trunk","size":75}`,
				f.repoID)
		default:
			t.Errorf("unexpected call %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// TestRepositoryToken_ScopesToOneRepositoryReadOnly asserts the DECODED
// request body — exactly one repository id and exactly `contents: read` —
// rather than the presence of a substring, and that the follow-up lookup
// is made with the minted token, by numeric id.
func TestRepositoryToken_ScopesToOneRepositoryReadOnly(t *testing.T) {
	fake := &scopedFake{token: longToken, repoID: 1103353668}
	srv := fake.server(t)

	c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(srv.URL+"/"))
	require.NoError(t, err)
	require.Equal(t, srv.URL, c.baseURL, "WithBaseURL must trim the trailing slash")

	scoped, err := c.RepositoryToken(context.Background(), 160225622, 1103353668)
	require.NoError(t, err)
	require.Equal(t, longToken, scoped.Token)
	require.Equal(t, "acme/widgets-renamed", scoped.FullName,
		"the name comes from the lookup made WITH the token, so a rename is reflected")
	require.Equal(t, "trunk", scoped.DefaultBranch)
	require.True(t, scoped.Private)
	require.WithinDuration(t, time.Now().Add(time.Hour), scoped.ExpiresAt, 2*time.Minute)

	fake.mu.Lock()
	defer fake.mu.Unlock()
	require.Len(t, fake.mintBodies, 1)

	// The decoded body: the key SET, then each value exactly.
	var body map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(fake.mintBodies[0], &body), "body=%s", fake.mintBodies[0])
	keys := make([]string, 0, len(body))
	for k := range body {
		keys = append(keys, k)
	}
	require.ElementsMatch(t, []string{"repository_ids", "permissions"}, keys)

	var ids []int64
	require.NoError(t, json.Unmarshal(body["repository_ids"], &ids))
	require.Equal(t, []int64{1103353668}, ids, "exactly one repository id, the one asked for")

	var perms map[string]string
	require.NoError(t, json.Unmarshal(body["permissions"], &perms))
	require.Equal(t, map[string]string{"contents": "read"}, perms,
		"exactly contents:read; anything else is a wider token than U4 allows")

	// The mint was signed as the App; the lookup used the NEW token.
	require.True(t, strings.HasPrefix(fake.mintAuth[0], "Bearer eyJ"), "the mint carries the App JWT")
	require.Equal(t, []string{"Bearer " + longToken}, fake.lookupAuth)
	require.Equal(t, []string{"/repositories/1103353668"}, fake.lookupPath, "looked up by id, not by name")
}

// TestRepositoryToken_NeverCaches: a job asks once per run, and a cached
// scoped token outliving its job is what U4 exists to avoid.
func TestRepositoryToken_NeverCaches(t *testing.T) {
	fake := &scopedFake{token: "ghs_short1", repoID: 7}
	c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(fake.server(t).URL))
	require.NoError(t, err)

	for i := 0; i < 3; i++ {
		_, err := c.RepositoryToken(context.Background(), 1, 7)
		require.NoError(t, err)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	require.Len(t, fake.mintBodies, 3, "three asks must be three mints")
}

// TestRepositoryToken_RefusesAScopeWiderThanAsked fails closed on what
// GitHub REPORTS the token carries, rather than trusting the request.
func TestRepositoryToken_RefusesAScopeWiderThanAsked(t *testing.T) {
	// Each refusal names its own cause, so 22-05's live proof can read
	// which of them the real API produced if the first mint is refused.
	cases := []struct {
		name         string
		repositories string
		permissions  string
		want         string
	}{
		{"two repositories", `[{"id":7},{"id":8}]`, "", "listing 2 repositories, not exactly the one requested (7)"},
		{"a different repository", `[{"id":8}]`, "", "listing 1 repositories, not exactly the one requested (7)"},
		{"no repositories listed", `[]`, "", "listing 0 repositories"},
		{"repositories field absent", absentField, "", "no repositories field"},
		{"contents write", "", `{"contents":"write","metadata":"read"}`, `contents="write"`},
		{"no contents at all", "", `{"metadata":"read"}`, `contents=""`},
		{"permissions object absent", "", absentField, "no permissions object"},
		{"an extra permission", "", `{"contents":"read","metadata":"read","issues":"read"}`, "issues=read"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := &scopedFake{token: longToken, repoID: 7,
				repositories: tc.repositories, permissions: tc.permissions}
			c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(fake.server(t).URL))
			require.NoError(t, err)

			_, err = c.RepositoryToken(context.Background(), 1, 7)
			require.Error(t, err)
			require.Contains(t, err.Error(), tc.want)
			require.NotContains(t, err.Error(), "ghs_", "a refused token must not reach the error")
			fake.mu.Lock()
			require.Empty(t, fake.lookupAuth, "a refused token must not be USED for the lookup either")
			fake.mu.Unlock()
		})
	}
}

// TestRepositoryToken_NeverLeaksCredentials covers the two calls' error
// paths with an upstream that echoes the request back — the proxy/WAF case
// redactSecrets exists for — using the measured token length and a short
// one, and the value's own renderings.
func TestRepositoryToken_NeverLeaksCredentials(t *testing.T) {
	for _, token := range []string{longToken, "ghs_x1"} {
		t.Run(fmt.Sprintf("token length %d", len(token)), func(t *testing.T) {
			t.Run("mint fails echoing the App JWT", func(t *testing.T) {
				fake := &scopedFake{token: token, repoID: 7, mintStatus: http.StatusBadGateway, mintEchoAuth: true}
				c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(fake.server(t).URL))
				require.NoError(t, err)

				_, err = c.RepositoryToken(context.Background(), 1, 7)
				require.Error(t, err)
				require.NotContains(t, err.Error(), "eyJ", "the App JWT must not survive into the error")
				require.Contains(t, err.Error(), "[REDACTED]")
				require.Contains(t, err.Error(), "502", "redaction must not cost the status code")
			})

			t.Run("lookup fails echoing the token", func(t *testing.T) {
				fake := &scopedFake{token: token, repoID: 7, lookupStatus: http.StatusBadGateway, lookupEchoTok: true}
				c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(fake.server(t).URL))
				require.NoError(t, err)

				_, err = c.RepositoryToken(context.Background(), 1, 7)
				require.Error(t, err)
				require.NotContains(t, err.Error(), token)
				require.NotContains(t, err.Error(), "ghs_")
				require.Contains(t, err.Error(), "[REDACTED]")
			})

			t.Run("the value renders without its token", func(t *testing.T) {
				st := ScopedToken{Token: token, ExpiresAt: time.Now(), FullName: "acme/widgets", DefaultBranch: "main"}
				for name, rendered := range map[string]string{
					"%v":         fmt.Sprintf("%v", st),
					"%+v":        fmt.Sprintf("%+v", st),
					"%s":         fmt.Sprintf("%s", st),
					"%#v":        fmt.Sprintf("%#v", st),
					"String()":   st.String(),
					"Sprint":     fmt.Sprint(st),
					"pointer %v": fmt.Sprintf("%v", &st),
				} {
					require.NotContains(t, rendered, token, "%s rendered the token", name)
					require.NotContains(t, rendered, "ghs_", "%s rendered the token", name)
					require.Contains(t, rendered, "acme/widgets", "%s should still say which repository", name)
				}
				encoded, err := json.Marshal(st)
				require.NoError(t, err)
				require.NotContains(t, string(encoded), token, "the token is json:\"-\"")
				require.NotContains(t, string(encoded), "ghs_")
			})
		})
	}
}

// TestRepositoryToken_LookupErrorRedactsTheTokenByValue is the belt under
// the prefix-based braces: a token under a prefix redactSecrets does not
// know, echoed by the failing lookup, must still not reach the error.
// Without redactValues on that path this leaks, which is what makes the
// mutation observable.
func TestRepositoryToken_LookupErrorRedactsTheTokenByValue(t *testing.T) {
	unknownPrefix := "zz_" + strings.Repeat("Q7wErTyU", 48)[:383]
	fake := &scopedFake{token: unknownPrefix, repoID: 7,
		lookupStatus: http.StatusBadGateway, lookupEchoTok: true}
	c, err := NewClient("4880866", writeTestKey(t), WithBaseURL(fake.server(t).URL))
	require.NoError(t, err)

	_, err = c.RepositoryToken(context.Background(), 1, 7)
	require.Error(t, err)
	// The visible PREFIX, not the whole token: request() keeps 300 bytes of
	// the body, so the echo is truncated, and an exact-match redaction let
	// 270 characters of it through on this test's first run.
	require.NotContains(t, err.Error(), unknownPrefix[:32],
		"a token of a shape the prefix list does not know must be redacted by value, "+
			"truncated echo included")
	require.Contains(t, err.Error(), "[REDACTED]")
	require.Contains(t, err.Error(), "502")
}

// TestRedactValues_CoversATruncatedEcho pins the prefix rule on its own.
func TestRedactValues_CoversATruncatedEcho(t *testing.T) {
	secret := "zz_" + strings.Repeat("Q7wErTyU", 48)[:383]

	whole := redactValues("bearer "+secret+" rejected", secret)
	require.Equal(t, "bearer [REDACTED] rejected", whole)

	truncated := redactValues("<html>bearer "+secret[:270], secret)
	require.Equal(t, "<html>bearer [REDACTED]", truncated,
		"an echo cut off mid-token must be redacted from the first eight characters on")

	twice := redactValues(secret[:40]+" and again "+secret[:100]+"!", secret)
	require.Equal(t, "[REDACTED] and again [REDACTED]!", twice)

	require.Equal(t, "nothing here", redactValues("nothing here", secret),
		"text without the anchor is untouched")
	require.Equal(t, "zz_Q7wE short", redactValues("zz_Q7wE short", secret),
		"fewer than eight matching characters is not an echo")
	require.Equal(t, "ab ab", redactValues("ab ab", "ab"),
		"a value shorter than eight characters is never used")
}

// TestNewClientFromEnv pins the three shapes main.go relies on.
func TestNewClientFromEnv(t *testing.T) {
	t.Run("unset is degraded, not an error", func(t *testing.T) {
		t.Setenv("GITHUB_APP_ID", "")
		t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", "")
		c, err := NewClientFromEnv()
		require.NoError(t, err)
		require.Nil(t, c)
	})
	t.Run("half set is degraded too", func(t *testing.T) {
		t.Setenv("GITHUB_APP_ID", "4880866")
		t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", "")
		c, err := NewClientFromEnv()
		require.NoError(t, err)
		require.Nil(t, c)
	})
	t.Run("set but unusable fails closed", func(t *testing.T) {
		t.Setenv("GITHUB_APP_ID", "4880866")
		t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", filepath.Join(t.TempDir(), "absent.pem"))
		_, err := NewClientFromEnv()
		require.Error(t, err)
	})
	t.Run("set and usable builds the client", func(t *testing.T) {
		t.Setenv("GITHUB_APP_ID", "4880866")
		t.Setenv("GITHUB_APP_PRIVATE_KEY_PATH", writeTestKey(t))
		c, err := NewClientFromEnv()
		require.NoError(t, err)
		require.NotNil(t, c)
		require.Equal(t, defaultBaseURL, c.baseURL)
	})
}
