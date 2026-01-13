package auth

import "os"

type Config struct {
	SupabaseURL        string
	SupabaseAnonKey    string
	SupabaseServiceKey string
	JWKSUrl            string // Supabase JWKS endpoint
}

func LoadConfig() *Config {
	supabaseURL := os.Getenv("SUPABASE_URL")
	return &Config{
		SupabaseURL:        supabaseURL,
		SupabaseAnonKey:    os.Getenv("SUPABASE_ANON_KEY"),
		SupabaseServiceKey: os.Getenv("SUPABASE_SERVICE_ROLE_KEY"),
		JWKSUrl:            supabaseURL + "/auth/v1/jwks",
	}
}
