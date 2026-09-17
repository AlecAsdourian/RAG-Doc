package jobs

// The guard for doc.go's loudest rule, which until now was only a docstring.
//
// `claimSQL` and `sweepSQL` are QUEUE-WIDE AND CROSS-TENANT BY CONSTRUCTION:
// neither carries an organization filter, `ingestion_jobs` has no row-level
// security to supply one, and PR #38's review measured an unscoped session
// claiming another organization's job. That is the design — a worker learns
// its tenant FROM the row it claimed — and it means neither statement may ever
// run inside a request handler, whatever tenant scope that handler holds.
//
// ⚠ WHY THIS EXISTS NOW AND DID NOT BEFORE. PR #41's review raised it as a nit
// against 21-05 and the answer was "not built here: there is no handler to
// guard yet, and a gate with nothing to catch is a gate nobody maintains."
// 21-07 is the plan that puts the first HTTP handler over this table, so the
// gate now has something to catch — and what it catches is a PASTE, which is
// the only way this SQL can reach `pkg/api`: both constants are unexported and
// live in an internal test file, so a handler cannot reference them.
//
// ⚠ IT READS CODE, NOT PROSE, and that is not a detail. The first cut scanned
// raw bytes and failed immediately — on `jobs.go`'s own comment explaining why
// the claim must not run in a handler. A gate that forbids describing the rule
// it enforces gets deleted, so this one tokenizes with `go/scanner` and
// inspects identifiers and string literals only.
//
// ⚠ WHAT IT IS NOT. A text scan, not a proof. It cannot see SQL assembled at
// run time, read from a file, or spelled differently, and its scope is one
// directory. It is the cheap half of a rule whose expensive half is the
// reasoning in doc.go — recorded as such rather than claimed as coverage,
// following 21-06's practice for a partial guard.

import (
	"go/scanner"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// forbiddenInHandlers is what must not appear in code under pkg/api.
//
// Two identifiers and three SQL fragments, and the fragments are the half that
// matters: the constants are unexported, so a handler cannot REFERENCE them.
// What it can do is copy the statement, which is how a rule like this is
// actually broken.
var forbiddenInHandlers = []struct {
	needle string
	why    string
}{
	{"claimSQL", "the claim statement is queue-wide and cross-tenant"},
	{"sweepSQL", "the sweeper is queue-wide and cross-tenant"},
	{"FOR UPDATE SKIP LOCKED", "the claim's signature clause, pasted"},
	{"attempts >= max_attempts", "the sweeper's signature predicate, pasted"},
	{"attempts < max_attempts", "the claim's poison-job guard, pasted"},
}

// codeText returns every identifier and string-literal value in a Go source
// file, with comments discarded.
func codeText(t *testing.T, path string, src []byte) []string {
	t.Helper()
	fset := token.NewFileSet()
	file := fset.AddFile(path, fset.Base(), len(src))

	var s scanner.Scanner
	// Mode 0: comments are not emitted. That is the whole point — see the
	// file comment above.
	s.Init(file, src, nil, 0)

	var out []string
	for {
		_, tok, lit := s.Scan()
		if tok == token.EOF {
			return out
		}
		switch tok {
		case token.IDENT:
			out = append(out, lit)
		case token.STRING:
			if unquoted, err := strconv.Unquote(lit); err == nil {
				out = append(out, unquoted)
			} else {
				out = append(out, lit)
			}
		}
	}
}

func TestClaimAndSweepNeverReachARequestHandler(t *testing.T) {
	// pkg/jobs -> services/backend/pkg/api
	root, err := filepath.Abs(filepath.Join("..", "api"))
	require.NoError(t, err)

	// PREMISE FIRST. A typo in the path, a package move, or a run from an
	// unexpected working directory would make this walk nothing and pass —
	// the quietest possible way for a gate to stop being one.
	info, err := os.Stat(root)
	require.NoError(t, err, "the directory this guard scans must exist")
	require.True(t, info.IsDir())

	scanned := 0
	require.NoError(t, filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") {
			return nil
		}
		src, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		scanned++
		for _, text := range codeText(t, path, src) {
			for _, f := range forbiddenInHandlers {
				if strings.Contains(text, f.needle) {
					t.Errorf("%s contains %q in CODE: %s.\n"+
						"`ingestion_jobs` has no row-level security, so this statement "+
						"reaches every tenant's rows whatever scope the handler holds "+
						"(pkg/jobs/doc.go). The read a handler wants is jobByIDSQL, "+
						"filtered on organization_id.",
						path, f.needle, f.why)
				}
			}
		}
		return nil
	}))

	// And the walk actually walked. `pkg/api` holds the router, the handlers
	// and their tests; anything under twenty files means the traversal
	// silently stopped short.
	require.Greater(t, scanned, 20,
		"the guard scanned only %d files; it is not looking where it thinks it is", scanned)

	// ⚠ AND THE SCAN CAN SEE WHAT IT CLAIMS TO SEE. Without this, a mistake
	// in `codeText` — the wrong scanner mode, a missing token case, an early
	// return — would make every file above yield nothing, and the loop would
	// pass by examining no text at all. That is the failure mode a text gate
	// has, so it is asserted rather than assumed.
	probe := []byte("package p\n// claimSQL in a comment is fine\n" +
		"const q = `SELECT 1 FOR UPDATE SKIP LOCKED`\nvar claimSQL = 1\n")
	got := codeText(t, "probe.go", probe)
	require.Contains(t, got, "SELECT 1 FOR UPDATE SKIP LOCKED",
		"the scan must see string literals")
	require.Contains(t, got, "claimSQL", "the scan must see identifiers")
	for _, text := range got {
		require.NotContains(t, text, "in a comment is fine",
			"the scan must NOT see comments")
	}
}
