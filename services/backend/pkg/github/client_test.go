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
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
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
