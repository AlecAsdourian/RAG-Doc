package github

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// writeTestKey generates an RSA key and writes it in the PKCS#1 PEM form
// GitHub issues.
func writeTestKey(t *testing.T) string {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	require.NoError(t, err)

	path := filepath.Join(t.TempDir(), "test-key.pem")
	require.NoError(t, os.WriteFile(path, pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(key),
	}), 0o600))
	return path
}

// newTestClient points a real client at a stub server.
func newTestClient(t *testing.T, srv *httptest.Server) *Client {
	t.Helper()
	c, err := NewClient("4880866", writeTestKey(t))
	require.NoError(t, err)
	c.baseURL = srv.URL
	return c
}

func TestNewClient_FailsClosedOnBadCredentials(t *testing.T) {
	valid := writeTestKey(t)

	t.Run("empty app id", func(t *testing.T) {
		_, err := NewClient("", valid)
		require.Error(t, err)
	})

	t.Run("non-numeric app id", func(t *testing.T) {
		// Catching this at construction matters: GitHub would reject the
		// JWT's `iss` at request time with a generic 401, and the operator
		// would go looking at the key.
		_, err := NewClient("rag-doc-dev", valid)
		require.Error(t, err)
		require.Contains(t, err.Error(), "not numeric")
	})

	t.Run("missing key file", func(t *testing.T) {
		_, err := NewClient("4880866", filepath.Join(t.TempDir(), "absent.pem"))
		require.Error(t, err)
	})

	t.Run("file that is not a key", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "junk.pem")
		require.NoError(t, os.WriteFile(path, []byte("definitely not a pem"), 0o600))

		_, err := NewClient("4880866", path)
		require.Error(t, err)
		require.NotContains(t, err.Error(), "definitely not a pem",
			"a malformed key's CONTENTS must not reach the error; the path is enough")
	})
}

func TestAppJWT_ShapeMatchesWhatGitHubAccepts(t *testing.T) {
	c, err := NewClient("4880866", writeTestKey(t))
	require.NoError(t, err)

	token, err := c.AppJWT()
	require.NoError(t, err)

	parts := strings.Split(token, ".")
	require.Len(t, parts, 3)

	decode := func(seg string) map[string]any {
		raw, derr := base64.RawURLEncoding.DecodeString(seg)
		require.NoError(t, derr)
		var m map[string]any
		require.NoError(t, json.Unmarshal(raw, &m))
		return m
	}

	require.Equal(t, "RS256", decode(parts[0])["alg"])

	claims := decode(parts[1])
	require.Equal(t, "4880866", claims["iss"])

	iat := int64(claims["iat"].(float64))
	exp := int64(claims["exp"].(float64))

	// GitHub rejects a JWT whose exp is more than 10 minutes out, and one
	// whose iat is in its future. Both were established by its docs and
	// both are cheap to get wrong.
	require.LessOrEqual(t, exp-iat, int64(11*60),
		"lifetime must stay inside GitHub's 10-minute ceiling (plus backdate)")
	require.Greater(t, exp-iat, int64(60))
}

func TestInstallationToken_MintsAndCaches(t *testing.T) {
	var mints int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, http.MethodPost, r.Method)
		require.Contains(t, r.URL.Path, "/app/installations/160225622/access_tokens")
		require.True(t, strings.HasPrefix(r.Header.Get("Authorization"), "Bearer "))
		mints++
		w.WriteHeader(http.StatusCreated)
		_, _ = fmt.Fprintf(w, `{"token":"ghs_faketoken","expires_at":%q}`,
			"2999-01-01T00:00:00Z")
	}))
	t.Cleanup(srv.Close)

	c := newTestClient(t, srv)
	ctx := context.Background()

	first, err := c.InstallationToken(ctx, 160225622)
	require.NoError(t, err)
	require.Equal(t, "ghs_faketoken", first)

	second, err := c.InstallationToken(ctx, 160225622)
	require.NoError(t, err)
	require.Equal(t, first, second)
	require.Equal(t, 1, mints, "a cached, unexpired token must not be re-minted")
}

func TestInstallationToken_RefreshesNearExpiry(t *testing.T) {
	var mints int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mints++
		w.WriteHeader(http.StatusCreated)
		// Already expired, so the margin logic must not serve it twice.
		_, _ = fmt.Fprintf(w, `{"token":"ghs_expiring%d","expires_at":%q}`,
			mints, "2000-01-01T00:00:00Z")
	}))
	t.Cleanup(srv.Close)

	c := newTestClient(t, srv)
	ctx := context.Background()

	_, err := c.InstallationToken(ctx, 1)
	require.NoError(t, err)
	_, err = c.InstallationToken(ctx, 1)
	require.NoError(t, err)

	require.Equal(t, 2, mints, "an expired cached token must be re-minted")
}

// TestInstallationToken_DeduplicatesConcurrentMints pins the property the
// cache's doc comment claims.
//
// The first version released its lock before the HTTP call: 25 concurrent
// callers produced 25 mint requests and 25 distinct tokens, last writer
// winning the cache. No data race — just no deduplication, which is
// exactly the burst Phase 21's parallel repository syncs will produce.
func TestInstallationToken_DeduplicatesConcurrentMints(t *testing.T) {
	var (
		mu    sync.Mutex
		mints int
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		mints++
		n := mints
		mu.Unlock()
		// A slow mint widens the window the old code raced through.
		time.Sleep(20 * time.Millisecond)
		w.WriteHeader(http.StatusCreated)
		_, _ = fmt.Fprintf(w, `{"token":"ghs_tok%d","expires_at":%q}`,
			n, "2999-01-01T00:00:00Z")
	}))
	t.Cleanup(srv.Close)

	c := newTestClient(t, srv)
	ctx := context.Background()

	const callers = 25
	var wg sync.WaitGroup
	tokens := make([]string, callers)
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			tok, err := c.InstallationToken(ctx, 42)
			if err == nil {
				tokens[i] = tok
			}
		}(i)
	}
	wg.Wait()

	mu.Lock()
	got := mints
	mu.Unlock()
	require.Equal(t, 1, got,
		"%d concurrent callers should mint ONE token, not %d", callers, got)

	for i, tok := range tokens {
		require.Equalf(t, tokens[0], tok, "caller %d got a different token", i)
	}
}

// TestInstallationToken_RejectsMissingExpiry: a response with no
// expires_at leaves the cache entry permanently stale, so every call
// re-mints — silently, visible only as unexplained API volume.
func TestInstallationToken_RejectsMissingExpiry(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = fmt.Fprint(w, `{"token":"ghs_noexpiry"}`)
	}))
	t.Cleanup(srv.Close)

	_, err := newTestClient(t, srv).InstallationToken(context.Background(), 1)
	require.Error(t, err)
	require.Contains(t, err.Error(), "no expires_at")
}

func TestListInstallationRepositories_FollowsPagination(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "access_tokens") {
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprint(w, `{"token":"ghs_x","expires_at":"2999-01-01T00:00:00Z"}`)
			return
		}
		page := r.URL.Query().Get("page")
		w.WriteHeader(http.StatusOK)
		if page == "1" {
			repos := make([]string, 100)
			for i := range repos {
				repos[i] = fmt.Sprintf(`{"id":%d,"name":"r%d","size":75}`, i, i)
			}
			_, _ = fmt.Fprintf(w, `{"total_count":101,"repositories":[%s]}`,
				strings.Join(repos, ","))
			return
		}
		_, _ = fmt.Fprint(w, `{"total_count":101,"repositories":[{"id":999,"name":"last","size":75}]}`)
	}))
	t.Cleanup(srv.Close)

	repos, err := newTestClient(t, srv).ListInstallationRepositories(context.Background(), 1)
	require.NoError(t, err)
	require.Len(t, repos, 101, "a short final page must end the loop, and both pages must be kept")
	require.Equal(t, int64(999), repos[100].ID)
}

// TestListInstallationRepositories_RefusesToTruncateSilently.
//
// The page bound is right — a pagination bug on either side would
// otherwise be an unbounded loop. Returning the partial list with a nil
// error is not: it tells the caller "these are all the repositories this
// installation can see", which is a wrong answer that gets acted on.
func TestListInstallationRepositories_RefusesToTruncateSilently(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "access_tokens") {
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprint(w, `{"token":"ghs_x","expires_at":"2999-01-01T00:00:00Z"}`)
			return
		}
		// Always a full page — the caller never sees an end.
		repos := make([]string, 100)
		for i := range repos {
			repos[i] = fmt.Sprintf(`{"id":%d,"name":"r","size":1}`, i)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprintf(w, `{"total_count":99999,"repositories":[%s]}`,
			strings.Join(repos, ","))
	}))
	t.Cleanup(srv.Close)

	repos, err := newTestClient(t, srv).ListInstallationRepositories(context.Background(), 1)

	require.Error(t, err,
		"hitting the page bound means the list is truncated; returning it with a nil "+
			"error would report a partial list as complete")
	require.Nil(t, repos, "a truncated list must not be handed back alongside the error")
	require.Contains(t, err.Error(), "truncated")
}

// TestRepository_SizeIsKilobytes pins the unit.
//
// GitHub's field is `size` and its unit is KILOBYTES — verified against a
// real repository reporting 75. The struct field is SizeKB and the column
// is size_kb for this reason: a `Size` field invites storage in a column
// named bytes, which under-reports by ~1000x with nothing ever erroring.
func TestRepository_SizeIsKilobytes(t *testing.T) {
	var repo Repository
	require.NoError(t, json.Unmarshal(
		[]byte(`{"id":1103353668,"size":75,"default_branch":"main","private":true,"visibility":"private"}`),
		&repo))

	require.Equal(t, int64(75), repo.SizeKB)
	require.Equal(t, int64(1103353668), repo.ID)
	require.Equal(t, "main", repo.DefaultBranch)
	require.Equal(t, "private", repo.Visibility)
}

// TestDo_NeverLeaksCredentials is the incident-hygiene test.
//
// The failing server echoes the request's Authorization header into its
// body, which is not contrived — proxies, WAFs and CDN error pages do
// exactly that, and this client sends an App JWT or an installation token
// there. The equivalent hole was found by a reviewer in
// pkg/auth/supabase_admin.go; this is the same guard, built in rather
// than retrofitted.
func TestDo_NeverLeaksCredentials(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = fmt.Fprintf(w,
			`<html>502 Bad Gateway. Request headers: authorization=%s</html>`,
			r.Header.Get("Authorization"))
	}))
	t.Cleanup(srv.Close)

	c := newTestClient(t, srv)

	t.Run("app jwt", func(t *testing.T) {
		jwt, err := c.AppJWT()
		require.NoError(t, err)

		_, err = c.GetInstallation(context.Background(), 1)
		require.Error(t, err)
		assert.NotContains(t, err.Error(), jwt,
			"the App JWT must not survive into an error, and thence the log")
		assert.Contains(t, err.Error(), "[REDACTED]")
		assert.Contains(t, err.Error(), "502", "redaction must not cost the status code")
	})

	t.Run("installation token", func(t *testing.T) {
		c2 := newTestClient(t, srv)
		c2.mu.Lock()
		c2.tokens[1] = cachedToken{
			token:     "ghs_supersecrettoken",
			expiresAt: time.Now().Add(time.Hour),
		}
		c2.mu.Unlock()

		_, err := c2.ListInstallationRepositories(context.Background(), 1)
		require.Error(t, err)
		assert.NotContains(t, err.Error(), "ghs_supersecrettoken",
			"an installation token must not survive into an error")
		assert.Contains(t, err.Error(), "[REDACTED]")
	})
}

// TestDo_DoesNotFollowRedirects guards the second way a credential walks
// out. Go strips Authorization across hosts, but not on a same-host
// redirect — and api.github.com has no reason to redirect at all.
func TestDo_DoesNotFollowRedirects(t *testing.T) {
	var targetSawAuth bool
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			targetSawAuth = true
		}
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprint(w, `{}`)
	}))
	t.Cleanup(target.Close)

	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	t.Cleanup(redirector.Close)

	c := newTestClient(t, redirector)

	_, err := c.GetInstallation(context.Background(), 1)
	require.Error(t, err, "a redirect must surface as an error, not be followed")
	assert.False(t, targetSawAuth, "credentials must never reach a redirect target")
}

// --- 20-04: the user-authorization leg and page-wise listing ---

// TestVerifyUserControlsInstallation_RefusesAnInstallationTheUserCannotSee
// is the unit-level guard for the takeover found in PR #22's review.
//
// The app-level endpoint proves an installation is real; only this check
// proves the caller controls it.
func TestVerifyUserControlsInstallation_RefusesAnInstallationTheUserCannotSee(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/login/oauth/access_token":
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"access_token":"gho_usertoken","token_type":"bearer"}`)
		case "/user/installations":
			// The user can see 42; they are asking about 99.
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"total_count":1,"installations":[{"id":42}]}`)
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer srv.Close()

	c := newTestClient(t, srv)
	c.oauthBaseURL = srv.URL
	c.clientID, c.clientSecret = "iv1.test", "secret"

	require.NoError(t, c.VerifyUserControlsInstallation(context.Background(), "code", 42),
		"an installation the user can see must be accepted")

	err := c.VerifyUserControlsInstallation(context.Background(), "code", 99)
	require.Error(t, err, "an installation the user cannot see must be refused")
	require.Contains(t, err.Error(), "does not have access")
}

func TestVerifyUserControlsInstallation_FailsClosedWithoutCredentials(t *testing.T) {
	c := newTestClient(t, httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			t.Error("must not call GitHub without client credentials")
		})))
	c.clientID, c.clientSecret = "", ""

	require.False(t, c.UserAuthConfigured())
	require.ErrorIs(t,
		c.VerifyUserControlsInstallation(context.Background(), "code", 1),
		ErrUserAuthUnavailable)
}

func TestVerifyUserControlsInstallation_RefusesAReusedCode(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// GitHub answers 200 with an `error` field for a bad or reused
		// code. Treating that as success would accept any code at all.
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"error":"bad_verification_code","error_description":"expired"}`)
	}))
	defer srv.Close()

	c := newTestClient(t, srv)
	c.oauthBaseURL = srv.URL
	c.clientID, c.clientSecret = "iv1.test", "secret"

	err := c.VerifyUserControlsInstallation(context.Background(), "reused", 42)
	require.Error(t, err)
	require.Contains(t, err.Error(), "bad_verification_code")
}

// TestListInstallationRepositoriesPage_HasNextAtTheBoundaries pins the
// value docs/api-github-install.md documents. It was computed here and
// stubbed away everywhere else, so nothing held it.
func TestListInstallationRepositoriesPage_HasNextAtTheBoundaries(t *testing.T) {
	total := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/access_tokens") {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"token":"ghs_x","expires_at":%q}`,
				time.Now().Add(time.Hour).Format(time.RFC3339))
			return
		}
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		start := (page - 1) * per
		var repos []string
		for i := start; i < total && i < start+per; i++ {
			repos = append(repos, fmt.Sprintf(`{"id":%d,"name":"r%d"}`, i, i))
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"total_count":%d,"repositories":[%s]}`, total, strings.Join(repos, ","))
	}))
	defer srv.Close()
	c := newTestClient(t, srv)

	cases := []struct {
		name        string
		total, page int
		perPage     int
		wantLen     int
		wantHasNext bool
	}{
		{"partial page is the last one", 31, 2, 30, 1, false},
		{"full first page of many", 31, 1, 30, 30, true},
		{"exactly divisible looks like more", 30, 1, 30, 30, true},
		{"and the next page is empty", 30, 2, 30, 0, false},
		{"past the end", 5, 9, 30, 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			total = tc.total
			repos, hasNext, err := c.ListInstallationRepositoriesPage(
				context.Background(), 1, tc.page, tc.perPage)
			require.NoError(t, err)
			require.Len(t, repos, tc.wantLen)
			require.Equal(t, tc.wantHasNext, hasNext)
		})
	}
}

// TestRedactSecrets_CoversOAuthTokens pins the gho_ prefix added in
// 20-04. A prefix this list does not know is a prefix that reaches a log.
func TestRedactSecrets_CoversOAuthTokens(t *testing.T) {
	c := newTestClient(t, httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {})))

	for _, secret := range []string{"ghs_installation", "ghu_user", "gho_oauth"} {
		got := c.redactSecrets("upstream said: Authorization: Bearer " + secret + "ABC123")
		require.NotContains(t, got, secret, "%s prefix must be redacted", secret)
		require.Contains(t, got, "[REDACTED]")
	}
}

// TestVerifyUserControlsInstallation_RefusesAnEmptyCodeWithoutCallingGitHub
// is defence in depth: real GitHub refuses an empty code anyway, but
// spending a round trip to learn that is a free way for an unauthenticated
// caller to make us talk to GitHub.
func TestVerifyUserControlsInstallation_RefusesAnEmptyCodeWithoutCallingGitHub(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer srv.Close()

	c := newTestClient(t, srv)
	c.oauthBaseURL = srv.URL
	c.clientID, c.clientSecret = "iv1.test", "secret"

	for _, code := range []string{"", "   ", "\t", "\n"} {
		err := c.VerifyUserControlsInstallation(context.Background(), code, 42)
		require.Error(t, err, "empty code %q must be refused", code)
	}
	require.False(t, called, "an empty code must not reach GitHub at all")
}

// TestUserHasInstallation_FailsClosedAtThePageBound pins the direction of
// the truncation answer. Returning true would accept an installation we
// never found.
func TestUserHasInstallation_FailsClosedAtThePageBound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/login/oauth/access_token" {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"access_token":"gho_x"}`)
			return
		}
		// Always a full page, so the bound is always reached.
		var items []string
		for i := 0; i < 100; i++ {
			items = append(items, fmt.Sprintf(`{"id":%d}`, i))
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"total_count":100000,"installations":[%s]}`, strings.Join(items, ","))
	}))
	defer srv.Close()

	c := newTestClient(t, srv)
	c.oauthBaseURL = srv.URL
	c.clientID, c.clientSecret = "iv1.test", "secret"

	err := c.VerifyUserControlsInstallation(context.Background(), "code", 424242)
	require.Error(t, err, "a truncated list must not be read as proof of access")
	require.Contains(t, err.Error(), "cannot confirm access")
}

// TestExchangeUserCode_DoesNotLeakTheClientSecret covers the credential
// this request carries in its BODY, where prefix-based redaction cannot
// see it — an upstream that echoes the request is the case redactSecrets
// exists for.
func TestExchangeUserCode_DoesNotLeakTheClientSecret(t *testing.T) {
	const secret = "1f2e3d4c5b6a7988776655443322110099aabbcc"
	const code = "thecodethatwassent"

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintf(w, "<html>WAF blocked request. body was: %s</html>", body)
	}))
	defer srv.Close()

	c := newTestClient(t, srv)
	c.oauthBaseURL = srv.URL
	c.clientID, c.clientSecret = "Iv1.probeclientid", secret

	err := c.VerifyUserControlsInstallation(context.Background(), code, 1)
	require.Error(t, err)
	require.NotContains(t, err.Error(), secret, "the client secret reached an error string")
	require.NotContains(t, err.Error(), code, "the authorization code reached an error string")
	require.Contains(t, err.Error(), "[REDACTED]")
}
