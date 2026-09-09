package auth

import (
	"os"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/github"
)

// OAuthConfig holds direct-OAuth provider configuration.
//
// GitHub only. The GitLab provider was removed in 20-04 per 20-CONTEXT
// decision 2 — its handlers had been unmounted since the ISS-011 cleanup,
// so it was advertising an integration that did not exist. The GitHub
// entry stays because ISS-011 records reviving direct GitHub OAuth as a
// real, if unlikely, option; it does not conflict with the GitHub App.
type OAuthConfig struct {
	GitHub *oauth2.Config
}

func NewOAuthConfig() *OAuthConfig {
	baseURL := os.Getenv("BASE_URL")
	if baseURL == "" {
		baseURL = "http://localhost:8080"
	}

	return &OAuthConfig{
		GitHub: &oauth2.Config{
			ClientID:     os.Getenv("GITHUB_CLIENT_ID"),
			ClientSecret: os.Getenv("GITHUB_CLIENT_SECRET"),
			RedirectURL:  baseURL + "/auth/github/callback",
			Scopes:       []string{"user:email", "read:user"},
			Endpoint:     github.Endpoint,
		},
	}
}
