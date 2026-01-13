package auth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"

	"golang.org/x/oauth2"
)

// HandleGitHubLogin redirects to GitHub OAuth
func HandleGitHubLogin(oauthConfig *OAuthConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Generate state token for CSRF protection
		state := generateSecureToken()
		// TODO: Store state in session/cookie for validation
		_ = state // TODO: implement state validation

		url := oauthConfig.GitHub.AuthCodeURL(state, oauth2.AccessTypeOffline)
		http.Redirect(w, r, url, http.StatusTemporaryRedirect)
	}
}

// HandleGitHubCallback processes OAuth callback
func HandleGitHubCallback(oauthConfig *OAuthConfig, provisioner *UserProvisioner) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		code := r.URL.Query().Get("code")
		state := r.URL.Query().Get("state")

		// TODO: Validate state token (check against stored value)
		_ = state // TODO: implement state validation

		if code == "" {
			http.Error(w, "Missing authorization code", http.StatusBadRequest)
			return
		}

		// Exchange code for token
		token, err := oauthConfig.GitHub.Exchange(context.Background(), code)
		if err != nil {
			http.Error(w, "Failed to exchange token", http.StatusInternalServerError)
			return
		}

		// Get user info from GitHub API
		client := oauthConfig.GitHub.Client(context.Background(), token)
		resp, err := client.Get("https://api.github.com/user")
		if err != nil {
			http.Error(w, "Failed to get user info", http.StatusInternalServerError)
			return
		}
		defer resp.Body.Close()

		var githubUser struct {
			ID    int    `json:"id"`
			Email string `json:"email"`
			Name  string `json:"name"`
			Login string `json:"login"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&githubUser); err != nil {
			http.Error(w, "Failed to decode user info", http.StatusInternalServerError)
			return
		}

		// Provision user in our database
		user, isNewUser, err := provisioner.ProvisionOAuthUser(
			r.Context(),
			"github",
			githubUser.Email,
			githubUser.Name,
			fmt.Sprintf("%d", githubUser.ID),
		)
		if err != nil {
			http.Error(w, "Failed to provision user", http.StatusInternalServerError)
			return
		}

		// Create Supabase session (sign in user to Supabase Auth)
		// TODO: Integrate with Supabase Auth (signInWithOAuth or custom JWT)

		// For new users, create organization
		if isNewUser {
			// TODO: Create organization, add user as owner
			// For now, prompt user for org name/slug or auto-generate
		}

		// Return JWT or redirect to frontend with session
		// TODO: Generate JWT or redirect with session cookie
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"user":      user,
			"is_new":    isNewUser,
			"message":   "Authentication successful",
			"note":      "TODO: Generate JWT and handle organization creation",
		})
	}
}

// HandleGitLabLogin redirects to GitLab OAuth
func HandleGitLabLogin(oauthConfig *OAuthConfig) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Generate state token for CSRF protection
		state := generateSecureToken()
		// TODO: Store state in session/cookie for validation
		_ = state // TODO: implement state validation

		url := oauthConfig.GitLab.AuthCodeURL(state, oauth2.AccessTypeOffline)
		http.Redirect(w, r, url, http.StatusTemporaryRedirect)
	}
}

// HandleGitLabCallback processes GitLab OAuth callback
func HandleGitLabCallback(oauthConfig *OAuthConfig, provisioner *UserProvisioner) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		code := r.URL.Query().Get("code")
		state := r.URL.Query().Get("state")

		// TODO: Validate state token (check against stored value)
		_ = state // TODO: implement state validation

		if code == "" {
			http.Error(w, "Missing authorization code", http.StatusBadRequest)
			return
		}

		// Exchange code for token
		token, err := oauthConfig.GitLab.Exchange(context.Background(), code)
		if err != nil {
			http.Error(w, "Failed to exchange token", http.StatusInternalServerError)
			return
		}

		// Get user info from GitLab API
		client := oauthConfig.GitLab.Client(context.Background(), token)
		resp, err := client.Get("https://gitlab.com/api/v4/user")
		if err != nil {
			http.Error(w, "Failed to get user info", http.StatusInternalServerError)
			return
		}
		defer resp.Body.Close()

		var gitlabUser struct {
			ID       int    `json:"id"`
			Email    string `json:"email"`
			Name     string `json:"name"`
			Username string `json:"username"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&gitlabUser); err != nil {
			http.Error(w, "Failed to decode user info", http.StatusInternalServerError)
			return
		}

		// Provision user in our database
		user, isNewUser, err := provisioner.ProvisionOAuthUser(
			r.Context(),
			"gitlab",
			gitlabUser.Email,
			gitlabUser.Name,
			fmt.Sprintf("%d", gitlabUser.ID),
		)
		if err != nil {
			http.Error(w, "Failed to provision user", http.StatusInternalServerError)
			return
		}

		// Create Supabase session (sign in user to Supabase Auth)
		// TODO: Integrate with Supabase Auth (signInWithOAuth or custom JWT)

		// For new users, create organization
		if isNewUser {
			// TODO: Create organization, add user as owner
		}

		// Return JWT or redirect to frontend with session
		// TODO: Generate JWT or redirect with session cookie
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"user":      user,
			"is_new":    isNewUser,
			"message":   "Authentication successful",
			"note":      "TODO: Generate JWT and handle organization creation",
		})
	}
}

func generateSecureToken() string {
	b := make([]byte, 32)
	rand.Read(b)
	return base64.URLEncoding.EncodeToString(b)
}
