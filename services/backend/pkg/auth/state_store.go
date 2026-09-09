package auth

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/redis/go-redis/v9"
)

// StateStore manages OAuth state tokens in Redis for CSRF protection
type StateStore struct {
	client *redis.Client
	ttl    time.Duration
}

// NewStateStore creates a new state store connected to Redis
func NewStateStore() (*StateStore, error) {
	redisURL := os.Getenv("REDIS_URL")
	if redisURL == "" {
		redisURL = "redis://localhost:6379"
	}

	opt, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("invalid REDIS_URL: %w", err)
	}

	client := redis.NewClient(opt)

	// Test connection
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("failed to connect to Redis: %w", err)
	}

	return &StateStore{
		client: client,
		ttl:    5 * time.Minute, // State tokens expire after 5 minutes
	}, nil
}

// StoreState stores an OAuth state token in Redis with TTL
// Key format: "oauth:state:{token}"
func (s *StateStore) StoreState(ctx context.Context, state string) error {
	// Store with TTL. Value is a placeholder; StoreStateValue is the
	// variant that carries one.
	err := s.client.Set(ctx, stateKey(state), "1", s.ttl).Err()
	if err != nil {
		return fmt.Errorf("failed to store state: %w", err)
	}

	return nil
}

// StoreStateValue stores a state token carrying a payload.
//
// The GitHub App install flow (20-04) binds the caller's organization to
// the token here, so the callback can recover it WITHOUT trusting a query
// parameter or the caller's current claim. The token is the only thing
// that survives the round trip through GitHub.
func (s *StateStore) StoreStateValue(ctx context.Context, state, value string) error {
	if err := s.client.Set(ctx, stateKey(state), value, s.ttl).Err(); err != nil {
		return fmt.Errorf("failed to store state: %w", err)
	}
	return nil
}

// ConsumeState atomically reads a state token and destroys it, returning
// the payload stored with it.
//
// GETDEL, not EXISTS-then-DEL. The earlier implementation did the latter,
// which is check-then-act: two callbacks presenting the same token could
// both observe it as present and both proceed, which is precisely the
// replay that single-use exists to prevent. It also returned `true` when
// the delete failed, with a comment reasoning that reuse-once beat
// blocking a valid user — a defensible trade for a login button, and the
// wrong one for a token that authorises linking a tenant to a GitHub
// installation.
func (s *StateStore) ConsumeState(ctx context.Context, state string) (string, bool, error) {
	value, err := s.client.GetDel(ctx, stateKey(state)).Result()
	if errors.Is(err, redis.Nil) {
		// Expired, never issued, or already used. Indistinguishable on
		// purpose: all three mean "do not proceed".
		return "", false, nil
	}
	if err != nil {
		return "", false, fmt.Errorf("failed to consume state: %w", err)
	}
	return value, true, nil
}

// ValidateState reports whether a state token was valid, consuming it.
//
// Retained for the direct-OAuth handlers. It is now a thin wrapper over
// ConsumeState so those inherit the atomic single-use guarantee too.
func (s *StateStore) ValidateState(ctx context.Context, state string) (bool, error) {
	_, ok, err := s.ConsumeState(ctx, state)
	return ok, err
}

func stateKey(state string) string {
	return fmt.Sprintf("oauth:state:%s", state)
}

// Close closes the Redis connection
func (s *StateStore) Close() error {
	return s.client.Close()
}
