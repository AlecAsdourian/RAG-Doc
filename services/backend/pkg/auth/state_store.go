package auth

import (
	"context"
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
	key := fmt.Sprintf("oauth:state:%s", state)

	// Store with TTL (value doesn't matter, we just check existence)
	err := s.client.Set(ctx, key, "1", s.ttl).Err()
	if err != nil {
		return fmt.Errorf("failed to store state: %w", err)
	}

	return nil
}

// ValidateState checks if a state token exists in Redis and deletes it (single-use)
// Returns true if state is valid, false otherwise
func (s *StateStore) ValidateState(ctx context.Context, state string) (bool, error) {
	key := fmt.Sprintf("oauth:state:%s", state)

	// Check if key exists
	exists, err := s.client.Exists(ctx, key).Result()
	if err != nil {
		return false, fmt.Errorf("failed to check state: %w", err)
	}

	if exists == 0 {
		// State doesn't exist (expired or never created)
		return false, nil
	}

	// Delete state (single-use token)
	err = s.client.Del(ctx, key).Err()
	if err != nil {
		// Log error but still return true (state was valid)
		// Worst case: state can be reused once (better than blocking valid user)
		return true, fmt.Errorf("state valid but failed to delete: %w", err)
	}

	return true, nil
}

// Close closes the Redis connection
func (s *StateStore) Close() error {
	return s.client.Close()
}
