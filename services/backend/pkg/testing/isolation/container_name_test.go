package isolation

import (
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// These tests cover ISS-037's fix: the harness container's name is derived
// from the checkout, with ISOLATION_CONTAINER_NAME as an override. None of
// them starts a container or calls SetupTestDB.

// dockerNameRule is the requirement every name handed to Docker must meet:
// Docker's own rule, [a-zA-Z0-9][a-zA-Z0-9_.-]+, narrowed to lowercase. It
// is the test's own copy on purpose. validContainerName in container.go is
// one of the things under test, so it cannot also be the ruler.
var dockerNameRule = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]+$`)

// listingFilter is the filter docs/local-development.md gives for listing
// the harness containers (`docker ps -a --filter name=...`). Every derived
// name must keep matching it.
const listingFilter = "rag-doc-isolation-tests-"

func TestContainerName_DifferentCheckoutsGetDifferentNames(t *testing.T) {
	// Sibling worktrees differ only in their last path element, which is
	// the case ISS-037 was filed on.
	roots := []string{
		`/home/dev/RAG-Doc`,
		`/home/dev/RAG-Doc/.claude/worktrees/agent-a`,
		`/home/dev/RAG-Doc/.claude/worktrees/agent-b`,
		`/home/dev/rag-doc-22-04`,
		`C:\Users\dev\RAG-Doc`,
		`C:\Users\dev\RAG-Doc\.claude\worktrees\agent-a`,
		`C:\Users\dev\RAG-Doc\.claude\worktrees\agent-b`,
	}
	seen := make(map[string]string, len(roots))
	for _, root := range roots {
		name, err := containerNameFor("", root)
		require.NoError(t, err)
		if other, taken := seen[name]; taken {
			t.Fatalf("checkouts %q and %q both derive %q: two worktrees would share one "+
				"container again, the collision ISS-037 is about", other, root, name)
		}
		seen[name] = root
	}
}

func TestContainerName_SameCheckoutIsStable(t *testing.T) {
	// GitHub Actions' default workspace for this repository.
	const root = `/home/runner/work/RAG-Doc/RAG-Doc`

	first, err := containerNameFor("", root)
	require.NoError(t, err)
	second, err := containerNameFor("", root)
	require.NoError(t, err)
	assert.Equal(t, first, second, "one checkout must derive one name on every call")

	// Pinned, because every test process of a run derives the name on its
	// own and must arrive at the same container. A change to the scheme
	// renames every developer's container, which strands the old ones, so
	// it should be a decision that updates this line.
	assert.Equal(t, containerNamePrefix+"-6f5ea25ff02f", first)
}

func TestContainerName_OverrideWins(t *testing.T) {
	for _, root := range []string{`/home/dev/RAG-Doc`, `C:\Users\dev\RAG-Doc`, ``} {
		name, err := containerNameFor("rag-doc-shared-pg", root)
		require.NoError(t, err)
		assert.Equal(t, "rag-doc-shared-pg", name, "the override must win over root %q", root)
	}

	// The pre-ISS-037 shared name is a legal override: that is how a
	// developer opts back into one container for several checkouts.
	name, err := containerNameFor(containerNamePrefix, `/home/dev/RAG-Doc`)
	require.NoError(t, err)
	assert.Equal(t, containerNamePrefix, name)
}

func TestContainerName_RefusesAnOverrideDockerCouldNotUse(t *testing.T) {
	for _, bad := range []string{
		"Rag-Doc-Pg",   // uppercase
		"rag doc",      // space
		"-rag-doc",     // leading dash
		".rag-doc",     // leading dot
		"_rag-doc",     // leading underscore
		"rag/doc",      // slash
		"rag:doc",      // colon
		"r",            // Docker needs at least two characters
		"rag-doc\n",    // control character
		"r\u00e1g-doc", // non-ASCII
	} {
		require.False(t, dockerNameRule.MatchString(bad), "premise: %q is outside the rule", bad)

		name, err := containerNameFor(bad, `/home/dev/RAG-Doc`)
		require.Error(t, err, "override %q must be refused, got name %q", bad, name)
		assert.Contains(t, err.Error(), containerNameEnv, "the error must name the variable to fix")
	}
}

func TestContainerName_IsAValidDockerName(t *testing.T) {
	// Roots Docker could not take as names themselves: uppercase, spaces,
	// backslashes, a drive colon, non-ASCII, and nothing at all.
	for _, root := range []string{
		`/home/runner/work/RAG-Doc/RAG-Doc`,
		`C:\Users\Dev\Desktop\RAG Doc\.claude\worktrees\Agent-X`,
		"/tmp/\u00fcn\u00efc\u00f6d\u00e9/RAG-Doc",
		``,
	} {
		name, err := containerNameFor("", root)
		require.NoError(t, err)
		assert.Regexp(t, dockerNameRule, name, "root %q", root)
		assert.True(t, strings.HasPrefix(name, listingFilter),
			"%q must keep the prefix the documented listing filters on", name)
	}

	for _, good := range []string{"rag-doc-shared-pg", "a1", "pg_16.test-x"} {
		name, err := containerNameFor(good, `/home/dev/RAG-Doc`)
		require.NoError(t, err, "override %q is a valid name and must be accepted", good)
		assert.Regexp(t, dockerNameRule, name)
	}
}

// TestCheckoutRoot_IsThisCheckout pins the five-directory climb from
// container.go. One directory too far would hash the directory that HOLDS
// the checkouts, and every worktree under it would share a container again.
func TestCheckoutRoot_IsThisCheckout(t *testing.T) {
	root := checkoutRoot()
	require.True(t, filepath.IsAbs(root), "checkout root %q is not absolute", root)

	for _, rel := range []string{
		filepath.Join("services", "backend", "go.mod"),
		filepath.Join("services", "backend", "pkg", "testing", "isolation", "container.go"),
	} {
		_, err := os.Stat(filepath.Join(root, rel))
		require.NoError(t, err, "checkout root %q must hold %s", root, rel)
	}

	ups, err := filepath.Glob(filepath.Join(migrationsDir(), "*.up.sql"))
	require.NoError(t, err)
	require.NotEmpty(t, ups, "migrationsDir %q holds no migrations", migrationsDir())
}

// TestCheckoutRoot_OneCheckoutTwoSpellings covers the canonicalisation: a
// checkout reached through a symlink, a dot-dot segment or (on Windows) a
// different letter case derives the same name as its plain path.
func TestCheckoutRoot_OneCheckoutTwoSpellings(t *testing.T) {
	root := checkoutRoot()
	want, err := containerNameFor("", root)
	require.NoError(t, err)

	sep := string(filepath.Separator)
	spellings := []string{root + sep + "services" + sep + ".."}
	if runtime.GOOS == "windows" {
		spellings = append(spellings, strings.ToLower(root), strings.ToUpper(root))
	}
	link := filepath.Join(t.TempDir(), "checkout-link")
	if err := os.Symlink(root, link); err == nil {
		spellings = append(spellings, link)
	} else {
		t.Logf("no symlink spelling on this machine (%v); the others still run", err)
	}

	for _, s := range spellings {
		got, err := containerNameFor("", canonicalPath(s))
		require.NoError(t, err)
		assert.Equal(t, want, got, "spelling %q of checkout %q", s, root)
	}
}

func TestResolveContainerName_ReadsTheEnvironment(t *testing.T) {
	t.Setenv(containerNameEnv, "rag-doc-override-test")
	name, err := resolveContainerName()
	require.NoError(t, err)
	assert.Equal(t, "rag-doc-override-test", name)

	t.Setenv(containerNameEnv, "")
	name, err = resolveContainerName()
	require.NoError(t, err)
	want, err := containerNameFor("", checkoutRoot())
	require.NoError(t, err)
	assert.Equal(t, want, name, "an empty override counts as unset")

	t.Logf("this checkout (%s) uses container %s", checkoutRoot(), name)
}
