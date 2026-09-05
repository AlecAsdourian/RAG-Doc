package isolation

import (
	"errors"
	"fmt"
	"path/filepath"

	"github.com/golang-migrate/migrate/v4"
	_ "github.com/golang-migrate/migrate/v4/database/postgres" // driver
	_ "github.com/golang-migrate/migrate/v4/source/file"       // source
)

// applyMigrations runs all up migrations from the given directory against dsn.
// It is idempotent — reusing the container across runs is safe because
// migrate.Up returns ErrNoChange when nothing new needs applying.
func applyMigrations(dsn string, migrationsPath string) error {
	abs, err := filepath.Abs(migrationsPath)
	if err != nil {
		return fmt.Errorf("resolve migrations path: %w", err)
	}
	sourceURL := "file://" + filepath.ToSlash(abs)

	m, err := migrate.New(sourceURL, dsn)
	if err != nil {
		return fmt.Errorf("open migrate: %w", err)
	}
	defer func() {
		// m.Close returns (source error, database error) — we've already run to
		// completion at this point, so surface only fatal source errors.
		_, _ = m.Close()
	}()

	if err := m.Up(); err != nil && !errors.Is(err, migrate.ErrNoChange) {
		return fmt.Errorf("run migrations up: %w", err)
	}
	return nil
}
