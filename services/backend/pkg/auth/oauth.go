package auth

import (
	"os"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/github"
	"golang.org/x/oauth2/gitlab"
)

type OAuthConfig struct {
	GitHub *oauth2.Config
	GitLab *oauth2.Config
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
		GitLab: &oauth2.Config{
			ClientID:     os.Getenv("GITLAB_CLIENT_ID"),
			ClientSecret: os.Getenv("GITLAB_CLIENT_SECRET"),
			RedirectURL:  baseURL + "/auth/gitlab/callback",
			Scopes:       []string{"read_user", "email"},
			Endpoint:     gitlab.Endpoint,
		},
	}
}
