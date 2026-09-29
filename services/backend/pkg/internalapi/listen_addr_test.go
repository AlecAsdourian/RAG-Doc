package internalapi_test

import (
	"testing"

	"github.com/stretchr/testify/require"

	"github.com/yourusername/smart-docs-platform/services/backend/pkg/internalapi"
)

// TestCheckListenAddr pins which INTERNAL_ADDR values main.go refuses to
// bind without the explicit override.
func TestCheckListenAddr(t *testing.T) {
	t.Run("loopback and named hosts are fine", func(t *testing.T) {
		for _, addr := range []string{
			internalapi.DefaultAddr,
			"127.0.0.1:8081",
			"[::1]:8081",
			"localhost:8081",
			"backend:8081",
			"10.0.3.7:8081",
			"[fd00::7]:8081",
			" 127.0.0.1:8081 ",
		} {
			all, err := internalapi.CheckListenAddr(addr)
			require.NoError(t, err, addr)
			require.False(t, all, "%s must not read as every interface", addr)
		}
	})

	t.Run("every-interface spellings are flagged", func(t *testing.T) {
		for _, addr := range []string{
			":8081",
			"0.0.0.0:8081",
			"[::]:8081",
			"[::0]:8081",
			"[0:0:0:0:0:0:0:0]:8081",
			"[::ffff:0.0.0.0]:8081",
			"0:8081",
			"0.0:8081",
			"0.0.0:8081",
		} {
			all, err := internalapi.CheckListenAddr(addr)
			require.NoError(t, err, addr)
			require.True(t, all, "%s binds every interface and must be flagged", addr)
		}
	})

	t.Run("not host:port is an error", func(t *testing.T) {
		for _, addr := range []string{
			"",
			"8081",
			"127.0.0.1",
			"127.0.0.1:",
			"::1:8081",
			"http://127.0.0.1:8081",
		} {
			_, err := internalapi.CheckListenAddr(addr)
			require.Error(t, err, addr)
		}
	})
}
