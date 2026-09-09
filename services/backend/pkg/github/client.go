// Package github talks to the GitHub App API as the App and as an
// installation.
//
// Everything here is shaped by what the live App actually returned on
// 2026-09-08, recorded under "VERIFIED CONTRACT" in 20-02-PLAN.md, not by
// what the documentation promises. Where the two differed, the recorded
// observation wins and says so.
package github

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	defaultBaseURL = "https://api.github.com"

	// The OAuth endpoints live on github.com, not api.github.com.
	defaultOAuthBaseURL = "https://github.com"

	// GitHub rejects an App JWT with more than 10 minutes of life. Nine
	// leaves room for clock skew without brushing the limit.
	appJWTLifetime = 9 * time.Minute

	// Backdate `iat`. GitHub rejects a JWT issued in its future, and a
	// client clock a few seconds fast is common enough to be worth
	// absorbing.
	appJWTBackdate = 60 * time.Second

	// Installation tokens last an hour (verified). Refresh early so a
	// long operation cannot straddle the expiry.
	installationTokenMargin = 5 * time.Minute

	requestTimeout = 15 * time.Second
)

// Client talks to GitHub as the App.
type Client struct {
	appID        string
	privateKey   *rsa.PrivateKey
	baseURL      string
	oauthBaseURL string
	httpClient   *http.Client

	// Client credentials for the user-authorization leg. Optional at
	// construction; without them the callback refuses to link anything,
	// because it cannot prove the person completing it has any authority
	// over the installation they named.
	clientID     string
	clientSecret string

	mu        sync.Mutex
	tokens    map[int64]cachedToken
	mintLocks map[int64]*sync.Mutex
}

// cachedToken returns a cached token that is not close to expiring.
func (c *Client) cachedToken(installationID int64) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	cached, ok := c.tokens[installationID]
	if !ok || !time.Now().Add(installationTokenMargin).Before(cached.expiresAt) {
		return "", false
	}
	return cached.token, true
}

// mintLockFor returns the per-installation mint lock, creating it once.
func (c *Client) mintLockFor(installationID int64) *sync.Mutex {
	c.mu.Lock()
	defer c.mu.Unlock()
	if l, ok := c.mintLocks[installationID]; ok {
		return l
	}
	l := &sync.Mutex{}
	c.mintLocks[installationID] = l
	return l
}

type cachedToken struct {
	token     string
	expiresAt time.Time
}

// NewClient loads the App private key and returns a client.
//
// Fails at construction on a missing or malformed key rather than at the
// first request — the 19-01 fail-closed pattern. A deployment with bad
// credentials should not start and then break the first time someone
// connects a repository.
func NewClient(appID, privateKeyPath string) (*Client, error) {
	if strings.TrimSpace(appID) == "" {
		return nil, errors.New("github: app id is empty; set GITHUB_APP_ID")
	}
	if _, err := strconv.ParseInt(appID, 10, 64); err != nil {
		return nil, fmt.Errorf("github: app id %q is not numeric: %w", appID, err)
	}
	if strings.TrimSpace(privateKeyPath) == "" {
		return nil, errors.New("github: private key path is empty; set GITHUB_APP_PRIVATE_KEY_PATH")
	}

	pemBytes, err := os.ReadFile(privateKeyPath)
	if err != nil {
		// The path, not the contents. A missing-file error should name the
		// file; a key that failed to parse should not be echoed.
		return nil, fmt.Errorf("github: read private key at %s: %w", privateKeyPath, err)
	}

	key, err := parseRSAPrivateKey(pemBytes)
	if err != nil {
		return nil, fmt.Errorf("github: private key at %s is not a usable RSA key: %w",
			privateKeyPath, err)
	}

	return &Client{
		appID:        appID,
		privateKey:   key,
		baseURL:      defaultBaseURL,
		oauthBaseURL: defaultOAuthBaseURL,
		clientID:     os.Getenv("GITHUB_APP_CLIENT_ID"),
		clientSecret: os.Getenv("GITHUB_APP_CLIENT_SECRET"),
		httpClient: &http.Client{
			Timeout: requestTimeout,
			// Never follow a redirect. Requests carry an App JWT or an
			// installation token in the Authorization header; Go strips
			// that across hosts, but not for a same-host redirect to a
			// different path, and api.github.com has no reason to redirect.
			// The same guard as pkg/auth/supabase_admin.go, added there
			// after a reviewer demonstrated a credential walking out via a
			// redirect.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		tokens:    make(map[int64]cachedToken),
		mintLocks: make(map[int64]*sync.Mutex),
	}, nil
}

func parseRSAPrivateKey(pemBytes []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("no PEM block found")
	}
	// GitHub issues PKCS#1 ("RSA PRIVATE KEY"). Accept PKCS#8 too, since
	// converting a key with openssl is a normal thing for someone to have
	// done.
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, errors.New("not a PKCS#1 or PKCS#8 private key")
	}
	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("key is %T, expected RSA", parsed)
	}
	return key, nil
}

// AppJWT mints a short-lived RS256 token identifying the App itself.
//
// Used for App-level calls (`GET /app`, listing installations, minting
// installation tokens). It cannot read repository contents — that needs an
// installation token.
func (c *Client) AppJWT() (string, error) {
	now := time.Now()
	header := map[string]string{"alg": "RS256", "typ": "JWT"}
	payload := map[string]any{
		"iat": now.Add(-appJWTBackdate).Unix(),
		"exp": now.Add(appJWTLifetime).Unix(),
		"iss": c.appID,
	}

	enc := func(v any) (string, error) {
		b, err := json.Marshal(v)
		if err != nil {
			return "", err
		}
		return base64.RawURLEncoding.EncodeToString(b), nil
	}

	h, err := enc(header)
	if err != nil {
		return "", fmt.Errorf("github: encode jwt header: %w", err)
	}
	p, err := enc(payload)
	if err != nil {
		return "", fmt.Errorf("github: encode jwt payload: %w", err)
	}

	signingInput := h + "." + p
	digest := sha256.Sum256([]byte(signingInput))
	sig, err := rsa.SignPKCS1v15(rand.Reader, c.privateKey, crypto.SHA256, digest[:])
	if err != nil {
		return "", fmt.Errorf("github: sign jwt: %w", err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

// InstallationToken returns a token scoped to one installation, minting a
// new one when the cached token is missing or near expiry.
//
// NEVER PERSIST THE RETURNED TOKEN. It lives about an hour (verified) and
// a stored one is a credential that outlives its usefulness. The in-memory
// cache below exists so a burst of calls does not mint a token each time,
// not to keep tokens around.
func (c *Client) InstallationToken(ctx context.Context, installationID int64) (string, error) {
	if tok, ok := c.cachedToken(installationID); ok {
		return tok, nil
	}

	// Serialize minting PER INSTALLATION, and re-check the cache once the
	// lock is held.
	//
	// The first version released the read lock before the HTTP call, which
	// is a check-then-act gap: measured at 25 concurrent callers producing
	// 25 mint requests and 25 distinct tokens, last writer winning the
	// cache. No data race — just no deduplication, while the comment
	// claimed the cache existed so "a burst of calls does not mint a token
	// each time". Phase 21's parallel repository syncs are exactly that
	// burst.
	//
	// Per-installation rather than one global lock, so a slow mint for one
	// installation does not stall every other one.
	mintLock := c.mintLockFor(installationID)
	mintLock.Lock()
	defer mintLock.Unlock()

	// Someone may have minted while we waited.
	if tok, ok := c.cachedToken(installationID); ok {
		return tok, nil
	}

	appJWT, err := c.AppJWT()
	if err != nil {
		return "", err
	}

	var out struct {
		Token     string    `json:"token"`
		ExpiresAt time.Time `json:"expires_at"`
	}
	endpoint := fmt.Sprintf("%s/app/installations/%d/access_tokens", c.baseURL, installationID)
	if err := c.do(ctx, http.MethodPost, endpoint, appJWT, &out); err != nil {
		return "", fmt.Errorf("github: mint installation token for %d: %w", installationID, err)
	}
	if out.Token == "" {
		return "", fmt.Errorf("github: installation %d returned an empty token", installationID)
	}
	if out.ExpiresAt.IsZero() {
		// Without an expiry the cache check below treats the token as
		// already stale, so every call re-mints — silently, and only
		// visible as unexplained API volume.
		return "", fmt.Errorf(
			"github: installation %d returned a token with no expires_at", installationID)
	}

	c.mu.Lock()
	c.tokens[installationID] = cachedToken{token: out.Token, expiresAt: out.ExpiresAt}
	c.mu.Unlock()

	return out.Token, nil
}

// Installation is what GitHub reports about an App installation.
type Installation struct {
	ID                  int64      `json:"id"`
	AccountLogin        string     `json:"-"`
	AccountType         string     `json:"-"`
	RepositorySelection string     `json:"repository_selection"`
	SuspendedAt         *time.Time `json:"suspended_at"`

	Account struct {
		Login string `json:"login"`
		Type  string `json:"type"`
	} `json:"account"`
}

// GetInstallation fetches one installation by id.
//
// Used by the 20-04 callback to prove that an `installation_id` arriving
// in a query string is real and ours, BEFORE anything is written.
func (c *Client) GetInstallation(ctx context.Context, installationID int64) (*Installation, error) {
	appJWT, err := c.AppJWT()
	if err != nil {
		return nil, err
	}

	var inst Installation
	endpoint := fmt.Sprintf("%s/app/installations/%d", c.baseURL, installationID)
	if err := c.do(ctx, http.MethodGet, endpoint, appJWT, &inst); err != nil {
		return nil, fmt.Errorf("github: get installation %d: %w", installationID, err)
	}
	inst.AccountLogin = inst.Account.Login
	inst.AccountType = inst.Account.Type
	return &inst, nil
}

// Repository is the subset of GitHub's repository payload this project
// stores.
//
// Note SizeKB. GitHub's field is `size` and it is in KILOBYTES — verified
// 2026-09-08 against a real repository reporting 75. Naming it for the
// unit is the whole point; `Size` invites someone to store it in a column
// called bytes.
type Repository struct {
	ID            int64  `json:"id"`
	Name          string `json:"name"`
	FullName      string `json:"full_name"`
	Private       bool   `json:"private"`
	Visibility    string `json:"visibility"`
	SizeKB        int64  `json:"size"`
	DefaultBranch string `json:"default_branch"`
	Archived      bool   `json:"archived"`
	Disabled      bool   `json:"disabled"`
	CloneURL      string `json:"clone_url"`
}

// ListInstallationRepositories returns every repository an installation
// can reach, following pagination.
func (c *Client) ListInstallationRepositories(ctx context.Context, installationID int64) ([]Repository, error) {
	token, err := c.InstallationToken(ctx, installationID)
	if err != nil {
		return nil, err
	}

	var all []Repository
	// Bounded rather than `for {}`. A pagination bug on either side turns
	// an unbounded loop into an outage; 100 pages at 100 per page is
	// 10,000 repositories, far past anything this product handles today.
	const maxPages = 100
	for page := 1; page <= maxPages; page++ {
		var out struct {
			TotalCount   int          `json:"total_count"`
			Repositories []Repository `json:"repositories"`
		}
		endpoint := fmt.Sprintf("%s/installation/repositories?per_page=100&page=%d", c.baseURL, page)
		if err := c.do(ctx, http.MethodGet, endpoint, token, &out); err != nil {
			return nil, fmt.Errorf("github: list repositories for installation %d: %w",
				installationID, err)
		}
		all = append(all, out.Repositories...)
		if len(out.Repositories) < 100 {
			return all, nil
		}
	}

	// Reaching the bound means the list is TRUNCATED. Returning it with a
	// nil error would tell the caller these are all the repositories the
	// installation can see, which is the kind of wrong answer that gets
	// acted on. An error is the honest result even though the data is
	// partially good.
	return nil, fmt.Errorf(
		"github: installation %d has more than %d repositories; refusing to return a "+
			"truncated list", installationID, maxPages*100)
}

// ListInstallationRepositoriesPage returns ONE page, and whether another
// follows.
//
// The accumulating variant above is right when the caller needs the whole
// set to search it (connecting a repository by id). It is wrong when the
// caller is feeding a picker: an installation with 5,000 repositories
// would become a 5,000-row response built from 50 sequential round trips
// to GitHub, on one HTTP request's budget.
//
// `hasNext` comes from the page being full rather than from parsing the
// Link header. GitHub sends `rel="next"` and that would be more precise,
// but a full last page then costs one extra empty request — which is the
// cheap failure. Misreading a Link header is the expensive one.
func (c *Client) ListInstallationRepositoriesPage(
	ctx context.Context, installationID int64, page, perPage int,
) (repos []Repository, hasNext bool, err error) {
	if page < 1 {
		page = 1
	}
	if perPage < 1 || perPage > 100 {
		perPage = 100
	}

	token, err := c.InstallationToken(ctx, installationID)
	if err != nil {
		return nil, false, err
	}

	var out struct {
		TotalCount   int          `json:"total_count"`
		Repositories []Repository `json:"repositories"`
	}
	endpoint := fmt.Sprintf("%s/installation/repositories?per_page=%d&page=%d",
		c.baseURL, perPage, page)
	if err := c.do(ctx, http.MethodGet, endpoint, token, &out); err != nil {
		return nil, false, fmt.Errorf("github: list repositories page %d for installation %d: %w",
			page, installationID, err)
	}
	return out.Repositories, len(out.Repositories) == perPage, nil
}

// ErrUserAuthUnavailable means the App has no client credentials, so the
// user-authorization leg cannot run.
var ErrUserAuthUnavailable = errors.New(
	"github: GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET are not set; " +
		"cannot verify that a user controls the installation they named")

// UserAuthConfigured reports whether the client can run the
// user-authorization leg at all.
func (c *Client) UserAuthConfigured() bool {
	return c.clientID != "" && c.clientSecret != ""
}

// VerifyUserControlsInstallation is the check that makes the install
// callback an authorization boundary rather than a form.
//
// WHY THIS EXISTS. `GET /app/installations/{id}` authenticates as the APP,
// so it succeeds for every installation of our App — it proves the
// installation is real, and nothing whatsoever about who is asking. A
// callback that stopped there let any authenticated user claim any
// installation that was not yet linked, simply by naming its id: install
// from GitHub's own button (which sends no `state`, so we refuse and
// leave it unlinked), then have an attacker complete the callback with
// their own state token and the victim's installation id. The attacker's
// organization then owns the link, and 20-03's connect endpoint will
// happily ingest the victim's private repositories through it.
//
// The fix is the leg GitHub provides for exactly this: with "Request user
// authorization (OAuth) during installation" enabled, the setup redirect
// also carries a `code`. Exchanging it yields a USER-to-server token, and
// `GET /user/installations` under that token lists only the installations
// that user can actually see. If the named installation is not in it, the
// person completing the callback does not control it.
//
// Returns nil only when the user demonstrably controls the installation.
func (c *Client) VerifyUserControlsInstallation(
	ctx context.Context, code string, installationID int64,
) error {
	if !c.UserAuthConfigured() {
		return ErrUserAuthUnavailable
	}
	if strings.TrimSpace(code) == "" {
		return errors.New("github: no user authorization code on the callback")
	}

	userToken, err := c.exchangeUserCode(ctx, code)
	if err != nil {
		return err
	}

	ok, err := c.userHasInstallation(ctx, userToken, installationID)
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf(
			"github: the authorizing user does not have access to installation %d",
			installationID)
	}
	return nil
}

// exchangeUserCode trades a setup `code` for a user-to-server token.
func (c *Client) exchangeUserCode(ctx context.Context, code string) (string, error) {
	form := url.Values{
		"client_id":     {c.clientID},
		"client_secret": {c.clientSecret},
		"code":          {code},
	}
	endpoint := c.oauthBaseURL + "/login/oauth/access_token"

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint,
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("github: build token exchange request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("github: token exchange: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("github: read token exchange response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		// REDACT BY VALUE, not just by prefix.
		//
		// This request carries its credentials in the BODY, not a header,
		// and `redactSecrets` only knows token prefixes — a GitHub App
		// client secret has none. Measured: an upstream that echoes the
		// request body (the proxy/WAF case redactSecrets exists for) put
		// `client_secret=…&code=…` verbatim into this error, which the
		// callback then logs.
		return "", fmt.Errorf("github: token exchange returned %d: %s",
			resp.StatusCode,
			redactValues(c.redactSecrets(string(body)), c.clientSecret, c.clientID, code))
	}

	var out struct {
		AccessToken      string `json:"access_token"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("github: decode token exchange response: %w", err)
	}
	// GitHub answers 200 with an `error` field for a bad or reused code.
	if out.Error != "" {
		return "", fmt.Errorf("github: token exchange refused: %s", out.Error)
	}
	if out.AccessToken == "" {
		return "", errors.New("github: token exchange returned no access token")
	}
	return out.AccessToken, nil
}

// userHasInstallation asks whether the token's owner can see the
// installation.
func (c *Client) userHasInstallation(
	ctx context.Context, userToken string, installationID int64,
) (bool, error) {
	// Bounded like ListInstallationRepositories, and for the same reason.
	const maxPages = 20
	for page := 1; page <= maxPages; page++ {
		var out struct {
			TotalCount    int            `json:"total_count"`
			Installations []Installation `json:"installations"`
		}
		endpoint := fmt.Sprintf("%s/user/installations?per_page=100&page=%d", c.baseURL, page)
		if err := c.do(ctx, http.MethodGet, endpoint, userToken, &out); err != nil {
			return false, fmt.Errorf("github: list user installations: %w", err)
		}
		for _, inst := range out.Installations {
			if inst.ID == installationID {
				return true, nil
			}
		}
		if len(out.Installations) < 100 {
			return false, nil
		}
	}
	// FAIL CLOSED. Returning true here would accept an installation we
	// never actually found; returning false would claim absence from a
	// list we know is truncated. Neither is honest, so this errors.
	return false, fmt.Errorf(
		"github: user has more than %d installations; cannot confirm access to %d",
		maxPages*100, installationID)
}

// do issues an authenticated request and decodes a JSON response.
//
// No retries. Phase 24 owns rate limiting, and a naive retry against
// GitHub's budget is worse than none — a 403 from a secondary rate limit
// answered with an immediate retry is how an App gets throttled harder.
func (c *Client) do(ctx context.Context, method, url, bearer string, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(nil))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "rag-doc")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return fmt.Errorf("github returned %d: %s",
			resp.StatusCode, c.redactSecrets(strings.TrimSpace(string(snippet))))
	}

	if out == nil {
		return nil
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("decode response: %w", err)
	}
	return nil
}

// redactSecrets removes credentials from text about to be embedded in an
// error, and therefore written to a log.
//
// Not paranoia about our own format string: the response body is written
// by whatever answered, which on a bad day is a proxy or WAF error page
// echoing the request headers back — and those headers carry an App JWT
// or an installation token. The same failure a reviewer demonstrated
// against pkg/auth/supabase_admin.go.
//
// Installation tokens are matched by their `ghs_` prefix (verified
// 2026-09-08) rather than by value, because the token in flight is not
// necessarily the one cached.
func (c *Client) redactSecrets(s string) string {
	// `gho_` is a user-to-server OAuth token; `ghr_` its refresh token,
	// which the exchange returns when "expire user authorization tokens"
	// is enabled on the App. A prefix this list does not know about is a
	// prefix that reaches a log intact.
	for _, prefix := range []string{"ghs_", "ghu_", "gho_", "ghr_"} {
		s = redactPrefixed(s, prefix)
	}
	// An App JWT is three base64url segments; redact anything that looks
	// like one rather than trying to match the exact string.
	return redactJWTs(s)
}

// redactValues removes exact strings, for secrets with no recognisable
// shape. Short values are skipped: redacting a two-character string would
// shred the surrounding text without protecting anything.
func redactValues(s string, values ...string) string {
	for _, v := range values {
		if len(v) < 8 {
			continue
		}
		s = strings.ReplaceAll(s, v, "[REDACTED]")
	}
	return s
}

func redactPrefixed(s, prefix string) string {
	for {
		i := strings.Index(s, prefix)
		if i < 0 {
			return s
		}
		end := i + len(prefix)
		for end < len(s) && (isAlphaNum(s[end]) || s[end] == '_') {
			end++
		}
		s = s[:i] + "[REDACTED]" + s[end:]
	}
}

func redactJWTs(s string) string {
	// eyJ is the base64url of `{"` — the start of every JWT header.
	for {
		i := strings.Index(s, "eyJ")
		if i < 0 {
			return s
		}
		end := i
		for end < len(s) && (isAlphaNum(s[end]) || s[end] == '-' || s[end] == '_' || s[end] == '.') {
			end++
		}
		s = s[:i] + "[REDACTED]" + s[end:]
	}
}

func isAlphaNum(b byte) bool {
	return (b >= '0' && b <= '9') || (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')
}
