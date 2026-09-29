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
//
// ONE CALL IS ONE SESSION. golang-migrate's postgres driver pins a single
// connection for the life of the migrate instance, so every migration this
// applies runs on the same backend, and whatever one leaves in a session
// setting the next one inherits. That is the shape ISS-031 lives in, and the
// seeded-migration gate relies on it.
func applyMigrations(dsn string, migrationsPath string) error {
	m, err := openMigrate(dsn, migrationsPath)
	if err != nil {
		return err
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

// applyMigrationsTo migrates dsn to exactly version, up or down, in a
// migrate instance of its own and therefore a session of its own.
func applyMigrationsTo(dsn string, migrationsPath string, version uint) error {
	m, err := openMigrate(dsn, migrationsPath)
	if err != nil {
		return err
	}
	defer func() { _, _ = m.Close() }()

	if err := m.Migrate(version); err != nil && !errors.Is(err, migrate.ErrNoChange) {
		return fmt.Errorf("migrate to %d: %w", version, err)
	}
	return nil
}

// forceMigrationVersion records version as applied and clean without
// running anything: `migrate force`, the operator's way out of a dirty
// version once its cause is fixed.
func forceMigrationVersion(dsn string, migrationsPath string, version int) error {
	m, err := openMigrate(dsn, migrationsPath)
	if err != nil {
		return err
	}
	defer func() { _, _ = m.Close() }()

	if err := m.Force(version); err != nil {
		return fmt.Errorf("force version %d: %w", version, err)
	}
	return nil
}

func openMigrate(dsn string, migrationsPath string) (*migrate.Migrate, error) {
	abs, err := filepath.Abs(migrationsPath)
	if err != nil {
		return nil, fmt.Errorf("resolve migrations path: %w", err)
	}
	m, err := migrate.New("file://"+filepath.ToSlash(abs), dsn)
	if err != nil {
		return nil, fmt.Errorf("open migrate: %w", err)
	}
	return m, nil
}
