package auth

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestStateStore_StoreAndValidate verifies basic state storage and validation
func TestStateStore_StoreAndValidate(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err, "Failed to create state store")
	defer store.Close()

	ctx := context.Background()
	state := "test-state-token-12345"

	// Store state
	err = store.StoreState(ctx, state)
	require.NoError(t, err, "Failed to store state")

	// Validate state (should succeed)
	valid, err := store.ValidateState(ctx, state)
	require.NoError(t, err, "Failed to validate state")
	assert.True(t, valid, "State should be valid")
}

// TestStateStore_SingleUse verifies state tokens can only be used once
func TestStateStore_SingleUse(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err, "Failed to create state store")
	defer store.Close()

	ctx := context.Background()
	state := "single-use-token"

	// Store state
	err = store.StoreState(ctx, state)
	require.NoError(t, err)

	// First validation should succeed
	valid, err := store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.True(t, valid, "First validation should succeed")

	// Second validation should fail (token deleted after first use)
	valid, err = store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.False(t, valid, "Second validation should fail (single-use token)")
}

// TestStateStore_InvalidToken verifies unknown tokens are rejected
func TestStateStore_InvalidToken(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err, "Failed to create state store")
	defer store.Close()

	ctx := context.Background()

	// Validate state that was never stored
	valid, err := store.ValidateState(ctx, "never-stored-token")
	require.NoError(t, err)
	assert.False(t, valid, "Unknown token should be invalid")
}

// TestStateStore_ExpiredToken verifies tokens expire after TTL
func TestStateStore_ExpiredToken(t *testing.T) {
	// Skip this test in normal runs (takes 6+ seconds)
	if testing.Short() {
		t.Skip("Skipping expiration test in short mode")
	}

	store, err := NewStateStore()
	require.NoError(t, err, "Failed to create state store")
	defer store.Close()

	// Override TTL to 1 second for faster test
	store.ttl = 1 * time.Second

	ctx := context.Background()
	state := "expiring-token"

	// Store state with 1-second TTL
	err = store.StoreState(ctx, state)
	require.NoError(t, err)

	// Validate immediately (should succeed)
	valid, err := store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.True(t, valid, "Token should be valid immediately")

	// Store again for expiration test
	err = store.StoreState(ctx, state)
	require.NoError(t, err)

	// Wait for expiration
	time.Sleep(2 * time.Second)

	// Validate after expiration (should fail)
	valid, err = store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.False(t, valid, "Token should be invalid after TTL expiration")
}

// TestStateStore_CSRFProtection verifies CSRF attack prevention
func TestStateStore_CSRFProtection(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err, "Failed to create state store")
	defer store.Close()

	ctx := context.Background()

	// Simulate legitimate OAuth flow
	legitimateState := "user-initiated-state"
	err = store.StoreState(ctx, legitimateState)
	require.NoError(t, err)

	// Attacker tries to use different state token (CSRF attack)
	attackerState := "attacker-controlled-state"

	// Attacker's state should be invalid
	valid, err := store.ValidateState(ctx, attackerState)
	require.NoError(t, err)
	assert.False(t, valid, "Attacker's state should be rejected")

	// Legitimate state should still work
	valid, err = store.ValidateState(ctx, legitimateState)
	require.NoError(t, err)
	assert.True(t, valid, "Legitimate state should be accepted")
}

// TestStateStore_ConsumeStateReturnsThePayload verifies that the value
// bound at store time survives the round trip.
//
// The GitHub App install flow (20-04) puts the caller's organization in
// here. If the payload did not come back, the callback would have nothing
// to link the installation to except caller-supplied input.
func TestStateStore_ConsumeStateReturnsThePayload(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err)
	defer store.Close()

	ctx := context.Background()
	const state = "test-consume-payload"
	const payload = `{"organization_id":"11111111-1111-1111-1111-111111111111"}`

	require.NoError(t, store.StoreStateValue(ctx, state, payload))

	got, ok, err := store.ConsumeState(ctx, state)
	require.NoError(t, err)
	require.True(t, ok, "a stored state must be consumable")
	assert.Equal(t, payload, got)
}

// TestStateStore_ConsumeStateIsSingleUseUnderConcurrency is the reason
// ConsumeState uses GETDEL rather than EXISTS-then-DEL.
//
// The earlier implementation was check-then-act: two callbacks presenting
// the same token could both observe it present and both proceed. For a
// login button that is a nuisance; for a token that authorises linking a
// tenant to a GitHub installation it is the whole vulnerability. Exactly
// one of N racing consumers must win.
func TestStateStore_ConsumeStateIsSingleUseUnderConcurrency(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err)
	defer store.Close()

	ctx := context.Background()
	const state = "test-consume-race"
	require.NoError(t, store.StoreStateValue(ctx, state, "payload"))

	const racers = 16
	var (
		start   = make(chan struct{})
		wg      sync.WaitGroup
		mu      sync.Mutex
		winners int
	)
	for i := 0; i < racers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, ok, err := store.ConsumeState(ctx, state)
			mu.Lock()
			defer mu.Unlock()
			if err == nil && ok {
				winners++
			}
		}()
	}
	close(start)
	wg.Wait()

	assert.Equal(t, 1, winners,
		"exactly one consumer may win the race; got %d", winners)
}

// TestStateStore_ValidateStateIsAlsoSingleUse pins that the legacy entry
// point inherits the atomic behaviour rather than keeping its own.
func TestStateStore_ValidateStateIsAlsoSingleUse(t *testing.T) {
	store, err := NewStateStore()
	require.NoError(t, err)
	defer store.Close()

	ctx := context.Background()
	const state = "test-validate-single-use"
	require.NoError(t, store.StoreState(ctx, state))

	first, err := store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.True(t, first)

	second, err := store.ValidateState(ctx, state)
	require.NoError(t, err)
	assert.False(t, second, "a state token must not validate twice")
}
