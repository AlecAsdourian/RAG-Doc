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
	"sort"
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

// Option adjusts a Client at construction.
type Option func(*Client)

// WithBaseURL points every API call at another host.
//
// Two callers: a test standing up a fake GitHub with httptest, which is
// how pkg/internalapi proves what the mint request's body carries without
// touching the real App; and, one day, a GitHub Enterprise host. Nothing
// in production sets it today.
func WithBaseURL(baseURL string) Option {
	return func(c *Client) {
		c.baseURL = strings.TrimRight(baseURL, "/")
	}
}

// NewClientFromEnv builds the client from GITHUB_APP_ID and
// GITHUB_APP_PRIVATE_KEY_PATH, or reports that there is nothing to build.
//
// (nil, nil) means the App is not configured — the degraded shape every
// test and most dev checkouts have, in which repository connection, the
// webhook receiver and the internal repository-token route are all
// unavailable and say so. An error means the credentials ARE set and
// cannot be used, which is not degraded-and-continue: a deployment that
// has credentials but cannot use them should refuse to start rather than
// fail at the first repository connect (19-01).
//
// Since 22-04 this is called once, in main.go, and the one client is
// handed to both listeners. It used to be built inside the public router,
// where the internal listener could not reach it.
func NewClientFromEnv() (*Client, error) {
	appID, keyPath := os.Getenv("GITHUB_APP_ID"), os.Getenv("GITHUB_APP_PRIVATE_KEY_PATH")
	if appID == "" || keyPath == "" {
		return nil, nil
	}
	return NewClient(appID, keyPath)
}

// NewClient loads the App private key and returns a client.
//
// Fails at construction on a missing or malformed key rather than at the
// first request — the 19-01 fail-closed pattern. A deployment with bad
// credentials should not start and then break the first time someone
// connects a repository.
func NewClient(appID, privateKeyPath string, opts ...Option) (*Client, error) {
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

	c := &Client{
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
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
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

// ScopedToken is an installation token narrowed to ONE repository with
// `contents: read`, and what that repository is called right now.
//
// It is what the worker fetches with (22-04, decision P10 / U4): the App
// private key stays in this process, and the process that parses untrusted
// code holds a credential good for one repository, read-only, for an hour.
//
// The token is excluded from JSON and from %v on purpose. The one place it
// belongs on the wire is pkg/internalapi's response, which copies it into
// its own struct explicitly; anything that logs or marshals this value by
// accident gets the repository name and the expiry, not the credential.
type ScopedToken struct {
	Token     string    `json:"-"`
	ExpiresAt time.Time `json:"expires_at"`

	// FullName and DefaultBranch come from a lookup made WITH the new
	// token, so they are current even after a rename and they prove the
	// token reaches the repository before the worker is handed it.
	FullName      string `json:"full_name"`
	DefaultBranch string `json:"default_branch"`
	Private       bool   `json:"private"`

	// ReportedRepositoryIDs and ReportedPermissions are the scope GitHub
	// REPORTED for the token in its mint reply, as RepositoryToken's
	// fail-closed checks accepted it: exactly the one repository, contents
	// read, and nothing beyond metadata. pkg/internalapi logs them on every
	// mint (22-05), so an operator and the live proof read what GitHub
	// granted rather than what was asked for. Neither carries the token.
	ReportedRepositoryIDs []int64           `json:"-"`
	ReportedPermissions   map[string]string `json:"-"`
}

// String renders the token without the token. fmt's %v and %s call it.
func (t ScopedToken) String() string {
	return fmt.Sprintf("ScopedToken{%s expires %s}", t.FullName, t.ExpiresAt.UTC().Format(time.RFC3339))
}

// ReportedRepositoryIDList renders ReportedRepositoryIDs for a log line:
// the ids in the order GitHub listed them, comma-separated.
func (t ScopedToken) ReportedRepositoryIDList() string {
	ids := make([]string, len(t.ReportedRepositoryIDs))
	for i, id := range t.ReportedRepositoryIDs {
		ids[i] = strconv.FormatInt(id, 10)
	}
	return strings.Join(ids, ",")
}

// ReportedPermissionList renders ReportedPermissions for a log line as
// name:level pairs, sorted by name, comma-separated. Names and levels are
// GitHub's permission vocabulary, never a credential.
func (t ScopedToken) ReportedPermissionList() string {
	names := make([]string, 0, len(t.ReportedPermissions))
	for name := range t.ReportedPermissions {
		names = append(names, name)
	}
	sort.Strings(names)
	pairs := make([]string, len(names))
	for i, name := range names {
		pairs[i] = name + ":" + t.ReportedPermissions[name]
	}
	return strings.Join(pairs, ",")
}

// GoString covers %#v, which bypasses String.
func (t ScopedToken) GoString() string { return t.String() }

// RepositoryToken mints an installation token scoped to one repository
// with `contents: read`, and proves it reaches that repository.
//
// NO CACHE, unlike InstallationToken, and that is the point rather than an
// omission: a job asks once per run, and a cached scoped token outliving
// its job is exactly the credential lifetime U4 exists to avoid. The
// caller is pkg/internalapi, which mints only for the holder of a live
// lease; the token then lives for GitHub's hour and no longer.
//
// The request body is what narrows the token (22-RESEARCH Q8, verified
// against GitHub's documentation): `repository_ids` with the one id, and a
// `permissions` object. GitHub returns the repositories and permissions
// the token actually carries, and both are checked here rather than
// trusted, because a token minted wider than asked for is precisely the
// failure this function exists to rule out. The checks fail closed: a
// response that does not list exactly the requested repository, or whose
// `contents` permission is anything but `read`, is an error. GitHub always
// adds `metadata: read`, so that one is allowed. 22-05's live proof is
// what validates this reading of the contract against the real API.
//
// Then `GET /repositories/{id}` WITH THE NEW TOKEN — by numeric id, which
// survives a rename — returns the current full name and default branch.
// The worker needs both, and a token that cannot read the repository's
// metadata cannot read its archive either, so failing here fails early.
func (c *Client) RepositoryToken(ctx context.Context, installationID, githubRepoID int64) (ScopedToken, error) {
	if githubRepoID <= 0 {
		return ScopedToken{}, fmt.Errorf("github: repository id %d is not a GitHub repository id", githubRepoID)
	}

	appJWT, err := c.AppJWT()
	if err != nil {
		return ScopedToken{}, err
	}

	body := map[string]any{
		"repository_ids": []int64{githubRepoID},
		"permissions":    map[string]string{"contents": "read"},
	}
	// Permissions and Repositories stay nil when GitHub omits the field
	// and are non-nil (possibly empty) when it sends one, so the errors
	// below can say WHICH happened. 22-05's live proof needs that: "no
	// permissions object" and "contents is not read" are different
	// conversations with the API.
	var out struct {
		Token        string            `json:"token"`
		ExpiresAt    time.Time         `json:"expires_at"`
		Permissions  map[string]string `json:"permissions"`
		Repositories []struct {
			ID       int64  `json:"id"`
			FullName string `json:"full_name"`
		} `json:"repositories"`
	}
	endpoint := fmt.Sprintf("%s/app/installations/%d/access_tokens", c.baseURL, installationID)
	if err := c.doJSON(ctx, http.MethodPost, endpoint, appJWT, body, &out); err != nil {
		return ScopedToken{}, fmt.Errorf(
			"github: mint repository token for installation %d repository %d: %w",
			installationID, githubRepoID, err)
	}
	if out.Token == "" {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned an empty token for repository %d",
			installationID, githubRepoID)
	}
	if out.ExpiresAt.IsZero() {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned a repository token with no expires_at", installationID)
	}

	// The scope, as GitHub reports it. Fail closed on anything wider than
	// what was asked for, and say which of the four things went wrong.
	if out.Repositories == nil {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned no repositories field for a request scoped to "+
				"repository %d; the token may be unscoped, refusing to hand it out",
			installationID, githubRepoID)
	}
	if len(out.Repositories) != 1 || out.Repositories[0].ID != githubRepoID {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned a token listing %d repositories, not exactly the "+
				"one requested (%d); refusing to hand it out",
			installationID, len(out.Repositories), githubRepoID)
	}
	if out.Permissions == nil {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned no permissions object; cannot confirm the token is "+
				"contents:read, refusing to hand it out", installationID)
	}
	if got := out.Permissions["contents"]; got != "read" {
		return ScopedToken{}, fmt.Errorf(
			"github: installation %d returned a token with contents=%q, not read; refusing to hand it out",
			installationID, got)
	}
	for name, level := range out.Permissions {
		if name != "contents" && name != "metadata" {
			return ScopedToken{}, fmt.Errorf(
				"github: installation %d returned a token carrying %s=%s beyond contents:read; "+
					"refusing to hand it out", installationID, name, level)
		}
	}

	// `GET /repositories/{id}` is the by-id alias of `GET /repos/{owner}/{repo}`.
	// It is what the rest of GitHub's API itself links to (`url` fields),
	// but it is not documented as an endpoint of its own. If it ever
	// stops answering, the fallback is `GET /repos/{full_name}` with
	// `out.Repositories[0].FullName` from the mint reply, which names the
	// repository as it was at mint time — a moment earlier, so a rename
	// in between is the only way it can miss. 22-05's live proof is what
	// tells us whether the alias holds.
	var repo Repository
	lookup := fmt.Sprintf("%s/repositories/%d", c.baseURL, githubRepoID)
	if err := c.do(ctx, http.MethodGet, lookup, out.Token, &repo); err != nil {
		// Redacted BY VALUE as well as by prefix: request already redacts
		// the `ghs_` shape, and this is the belt for a token GitHub some
		// day issues under a prefix that list does not know.
		return ScopedToken{}, fmt.Errorf(
			"github: repository %d is not reachable with its scoped token: %s",
			githubRepoID, redactValues(err.Error(), out.Token))
	}
	if repo.ID != githubRepoID {
		return ScopedToken{}, fmt.Errorf(
			"github: asked for repository %d and was answered about %d", githubRepoID, repo.ID)
	}
	if repo.FullName == "" || repo.DefaultBranch == "" {
		return ScopedToken{}, fmt.Errorf(
			"github: repository %d has no full_name or default_branch in its metadata", githubRepoID)
	}

	reportedIDs := make([]int64, len(out.Repositories))
	for i, r := range out.Repositories {
		reportedIDs[i] = r.ID
	}
	reportedPermissions := make(map[string]string, len(out.Permissions))
	for name, level := range out.Permissions {
		reportedPermissions[name] = level
	}

	return ScopedToken{
		Token:                 out.Token,
		ExpiresAt:             out.ExpiresAt,
		FullName:              repo.FullName,
		DefaultBranch:         repo.DefaultBranch,
		Private:               repo.Private,
		ReportedRepositoryIDs: reportedIDs,
		ReportedPermissions:   reportedPermissions,
	}, nil
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

// do issues an authenticated request with no body and decodes a JSON
// response.
//
// No retries. Phase 24 owns rate limiting, and a naive retry against
// GitHub's budget is worse than none — a 403 from a secondary rate limit
// answered with an immediate retry is how an App gets throttled harder.
func (c *Client) do(ctx context.Context, method, url, bearer string, out any) error {
	return c.request(ctx, method, url, bearer, nil, out)
}

// doJSON is do with a JSON request body.
//
// It exists because RepositoryToken has to SEND something — the
// `repository_ids` and `permissions` that narrow the token — and do sends
// nothing. Both go through request below, so there is exactly one place
// where a response body becomes an error string and exactly one
// redaction on that path. A second copy of that code is a second place
// for a credential to leak from.
func (c *Client) doJSON(ctx context.Context, method, url, bearer string, body, out any) error {
	encoded, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("encode request body: %w", err)
	}
	return c.request(ctx, method, url, bearer, encoded, out)
}

// request is the one HTTP path. A nil body sends no body.
func (c *Client) request(ctx context.Context, method, url, bearer string, body []byte, out any) error {
	req, err := http.NewRequestWithContext(ctx, method, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "rag-doc")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

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
// shape — and any run of at least eight characters that is a PREFIX of
// one.
//
// The prefix rule is not a refinement. The text this is applied to is a
// 300-byte snippet of an upstream body, so an echoed secret is routinely
// cut off, and a cut-off secret is most of a secret: measured while
// applying PR #52's review (N1), a 387-character token echoed by a failing
// lookup survived an exact-match redaction with its first 270 characters
// intact. Anchoring on the first eight characters and extending the match
// as far as the text and the secret agree catches the truncated echo too.
//
// Short values are skipped: redacting a two-character string would shred
// the surrounding text without protecting anything.
func redactValues(s string, values ...string) string {
	const anchorLen = 8
	for _, v := range values {
		if len(v) < anchorLen {
			continue
		}
		anchor := v[:anchorLen]
		for {
			i := strings.Index(s, anchor)
			if i < 0 {
				break
			}
			j, k := i, 0
			for j < len(s) && k < len(v) && s[j] == v[k] {
				j++
				k++
			}
			s = s[:i] + "[REDACTED]" + s[j:]
		}
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
