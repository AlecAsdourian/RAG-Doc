"""Tests for metadata-based ranking boosts."""

import pytest
from workers.retrieval.metadata_booster import MetadataBooster


class TestMetadataBooster:
    """Test suite for MetadataBooster class."""

    @pytest.fixture
    def booster(self):
        """Create a MetadataBooster instance with default config."""
        return MetadataBooster()

    @pytest.fixture
    def custom_booster(self):
        """Create a MetadataBooster instance with custom config."""
        config = {
            "chunk_type_boosts": {
                "docs": 2.0,
                "function": 1.5,
            },
            "breadcrumb_match_boost": 2.0,
            "noise_penalty": 0.1,
        }
        return MetadataBooster(config)

    @pytest.fixture
    def parsed_query(self):
        """Sample parsed query."""
        return {
            "raw_query": 'find AuthService "login error"',
            "quoted_terms": ["login error"],
            "identifiers": ["AuthService"],
            "clean_query": "find  ",
        }

    def test_no_boosts_neutral_metadata(self, booster, parsed_query):
        """Test that neutral metadata results in unchanged score."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "function",  # neutral 1.0x
                "file_path": "src/utils.py",
                "breadcrumb": "helpers.format_date",
                "content": "def format_date(date): pass",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        assert len(result) == 1
        assert result[0]["boosted_score"] == 0.5
        assert result[0]["boost_multiplier"] == 1.0

    def test_chunk_type_boost_docs(self, booster, parsed_query):
        """Test chunk_type='docs' gets 1.5x boost."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "docs",
                "file_path": "README.md",
                "content": "Authentication documentation",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        assert result[0]["boosted_score"] == pytest.approx(0.75)  # 0.5 * 1.5
        assert result[0]["boost_multiplier"] == 1.5

    def test_chunk_type_penalty_test(self, booster, parsed_query):
        """Test chunk_type='test' gets 0.8x penalty."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "test",
                "file_path": "tests/test_auth.py",
                "content": "def test_login(): pass",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        assert result[0]["boosted_score"] == pytest.approx(0.4)  # 0.5 * 0.8
        assert result[0]["boost_multiplier"] == 0.8

    def test_breadcrumb_exact_match(self, booster, parsed_query):
        """Test breadcrumb matching identifier gets 1.3x boost."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "function",
                "file_path": "src/auth.py",
                "breadcrumb": "AuthService.login",
                "content": "def login(user): pass",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Should get breadcrumb match boost for "AuthService"
        assert result[0]["boosted_score"] == pytest.approx(0.65)  # 0.5 * 1.3
        assert result[0]["boost_multiplier"] == 1.3

    def test_quoted_term_match(self, booster, parsed_query):
        """Test quoted term in content gets 1.4x boost."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "function",
                "file_path": "src/auth.py",
                "breadcrumb": "validate_user",
                "content": "raise Exception('login error')",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Should get quoted match boost for "login error"
        assert result[0]["boosted_score"] == pytest.approx(0.7)  # 0.5 * 1.4
        assert result[0]["boost_multiplier"] == 1.4

    def test_noise_penalty_node_modules(self, booster, parsed_query):
        """Test node_modules path gets 0.3x penalty."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.6,
                "chunk_type": "function",
                "file_path": "node_modules/express/lib/router.js",
                "content": "function route() {}",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        assert result[0]["boosted_score"] == pytest.approx(0.18)  # 0.6 * 0.3
        assert result[0]["boost_multiplier"] == 0.3

    def test_noise_penalty_vendor(self, booster, parsed_query):
        """Test vendor path gets 0.3x penalty."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.6,
                "chunk_type": "function",
                "file_path": "vendor/github.com/pkg/errors/errors.go",
                "content": "func New() {}",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        assert result[0]["boosted_score"] == pytest.approx(0.18)  # 0.6 * 0.3
        assert result[0]["boost_multiplier"] == 0.3

    def test_noise_penalty_lockfile(self, booster, parsed_query):
        """Test .lock file gets 0.3x penalty."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.6,
                "chunk_type": "file_summary",
                "file_path": "package-lock.json",
                "content": '{"name": "pkg"}',
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Gets file_summary boost (1.4) AND noise penalty (0.3)
        # 0.6 * 1.4 * 0.3 = 0.252
        assert result[0]["boosted_score"] == pytest.approx(0.252)
        assert result[0]["boost_multiplier"] == pytest.approx(0.42)  # 1.4 * 0.3

    def test_combined_boosts_multiply(self, booster, parsed_query):
        """Test multiple boosts multiply together."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "docs",  # 1.5x
                "file_path": "docs/auth.md",
                "breadcrumb": "AuthService Documentation",  # 1.3x for AuthService match
                "content": "Authentication using login error handling",  # 1.4x for quoted match
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Combined: 0.5 * 1.5 * 1.3 * 1.4 = 1.365
        assert result[0]["boosted_score"] == pytest.approx(1.365)
        assert result[0]["boost_multiplier"] == pytest.approx(2.73)  # 1.5 * 1.3 * 1.4

    def test_custom_weights(self, custom_booster, parsed_query):
        """Test custom boost weights override defaults."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "docs",  # Custom 2.0x instead of 1.5x
                "file_path": "README.md",
                "content": "Documentation",
            }
        ]

        result = custom_booster.boost(chunks, parsed_query)

        assert result[0]["boosted_score"] == pytest.approx(1.0)  # 0.5 * 2.0
        assert result[0]["boost_multiplier"] == 2.0

    def test_missing_metadata_graceful(self, booster, parsed_query):
        """Test missing metadata fields default to neutral."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                # No chunk_type, file_path, breadcrumb, or content
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Should default to neutral (1.0x) for missing fields
        assert result[0]["boosted_score"] == 0.5
        assert result[0]["boost_multiplier"] == 1.0

    def test_identifier_match_in_content(self, booster, parsed_query):
        """Test identifier appearing in content gets 1.3x boost."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "function",
                "file_path": "src/service.py",
                "breadcrumb": "ServiceClass.method",
                "content": "Initialize AuthService and call login",
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Should get identifier match boost for "AuthService" in content
        assert result[0]["boosted_score"] == pytest.approx(0.65)  # 0.5 * 1.3
        assert result[0]["boost_multiplier"] == 1.3

    def test_multiple_chunks_independent(self, booster, parsed_query):
        """Test that multiple chunks are boosted independently."""
        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "docs",
                "file_path": "README.md",
                "content": "Documentation",
            },
            {
                "chunk_id": "2",
                "rrf_score": 0.3,
                "chunk_type": "test",
                "file_path": "tests/test_auth.py",
                "content": "def test_auth(): pass",
            },
            {
                "chunk_id": "3",
                "rrf_score": 0.4,
                "chunk_type": "function",
                "file_path": "node_modules/pkg/index.js",
                "content": "export function x() {}",
            },
        ]

        result = booster.boost(chunks, parsed_query)

        assert len(result) == 3
        assert result[0]["boosted_score"] == pytest.approx(0.75)  # 0.5 * 1.5
        assert result[1]["boosted_score"] == pytest.approx(0.24)  # 0.3 * 0.8
        assert result[2]["boosted_score"] == pytest.approx(0.12)  # 0.4 * 0.3

    def test_case_insensitive_matching(self, booster):
        """Test that term matching is case-insensitive."""
        parsed_query = {
            "raw_query": "AuthService",
            "quoted_terms": [],
            "identifiers": ["AuthService"],
            "clean_query": "AuthService",
        }

        chunks = [
            {
                "chunk_id": "1",
                "rrf_score": 0.5,
                "chunk_type": "function",
                "file_path": "src/auth.py",
                "breadcrumb": "authservice.login",  # lowercase
                "content": "AUTHSERVICE class",  # uppercase
            }
        ]

        result = booster.boost(chunks, parsed_query)

        # Should match both breadcrumb and content despite case differences
        # Breadcrumb: 1.3x, Content: 1.3x = 1.3 * 1.3 = 1.69x
        assert result[0]["boosted_score"] == pytest.approx(0.845)  # 0.5 * 1.69
        assert result[0]["boost_multiplier"] == pytest.approx(1.69)

    def test_all_noise_patterns(self, booster, parsed_query):
        """Test all noise patterns apply penalty."""
        noise_paths = [
            "node_modules/pkg/index.js",
            "vendor/github.com/pkg/errors.go",
            "dist/bundle.js",
            "build/output.js",
            "Cargo.lock",
            "yarn.lock",
            "migrations/001_initial.sql",
            "__pycache__/module.pyc",
            ".git/objects/abc123",
        ]

        chunks = [
            {
                "chunk_id": str(i),
                "rrf_score": 0.6,
                "chunk_type": "function",
                "file_path": path,
                "content": "code",
            }
            for i, path in enumerate(noise_paths)
        ]

        result = booster.boost(chunks, parsed_query)

        # All should get noise penalty
        for chunk in result:
            assert chunk["boosted_score"] == pytest.approx(0.18)  # 0.6 * 0.3
            assert chunk["boost_multiplier"] == 0.3
