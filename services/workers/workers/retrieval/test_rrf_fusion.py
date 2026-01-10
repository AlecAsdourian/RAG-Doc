"""Tests for RRF (Reciprocal Rank Fusion) algorithm."""

import pytest

from workers.retrieval.rrf_fusion import RRFFusion


class TestRRFFusion:
    """Test suite for RRF fusion algorithm."""

    def test_empty_input(self):
        """Test that empty input returns empty output."""
        fusion = RRFFusion()
        result = fusion.fuse({})
        assert result == []

    def test_single_result_list(self):
        """Test fusion with single system's results."""
        fusion = RRFFusion()

        # Single system with 3 results
        results = {
            "fts": [
                {"chunk_id": "A", "content": "chunk A"},
                {"chunk_id": "B", "content": "chunk B"},
                {"chunk_id": "C", "content": "chunk C"},
            ]
        }

        output = fusion.fuse(results)

        # Should have 3 results
        assert len(output) == 3

        # Check that all chunks are present
        chunk_ids = [item["chunk_id"] for item in output]
        assert set(chunk_ids) == {"A", "B", "C"}

        # Check that RRF scores are present and ordered
        assert all("rrf_score" in item for item in output)
        scores = [item["rrf_score"] for item in output]
        assert scores == sorted(scores, reverse=True)  # Descending order

        # Check sources tracking
        assert all(item["sources"] == ["fts"] for item in output)

    def test_two_lists_no_overlap(self):
        """Test fusion with two systems, no overlapping chunks."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {"chunk_id": "A", "content": "chunk A"},
                {"chunk_id": "B", "content": "chunk B"},
            ],
            "vector": [
                {"chunk_id": "C", "content": "chunk C"},
                {"chunk_id": "D", "content": "chunk D"},
            ]
        }

        output = fusion.fuse(results)

        # Should have 4 unique results
        assert len(output) == 4

        # All chunks present
        chunk_ids = [item["chunk_id"] for item in output]
        assert set(chunk_ids) == {"A", "B", "C", "D"}

        # Each chunk has one source
        for item in output:
            assert len(item["sources"]) == 1
            assert item["sources"][0] in ["fts", "vector"]

    def test_two_lists_with_overlap(self):
        """Test fusion with overlapping chunks - overlap should score higher."""
        fusion = RRFFusion()

        # Example from the plan:
        # FTS results: [A, B, C]
        # Vector results: [B, D, A]
        results = {
            "fts": [
                {"chunk_id": "A", "content": "chunk A"},
                {"chunk_id": "B", "content": "chunk B"},
                {"chunk_id": "C", "content": "chunk C"},
            ],
            "vector": [
                {"chunk_id": "B", "content": "chunk B"},
                {"chunk_id": "D", "content": "chunk D"},
                {"chunk_id": "A", "content": "chunk A"},
            ]
        }

        output = fusion.fuse(results)

        # Should have 4 unique results (A, B, C, D)
        assert len(output) == 4

        # Get scores for each chunk
        scores_by_id = {item["chunk_id"]: item["rrf_score"] for item in output}

        # B appears at position 2 in fts and position 1 in vector
        # A appears at position 1 in fts and position 3 in vector
        # Both should score higher than C and D (single-system chunks)

        # B should have highest score (best ranks in both systems)
        assert output[0]["chunk_id"] == "B"

        # A should be second (in both systems but not top rank in vector)
        assert output[1]["chunk_id"] == "A"

        # Both overlap chunks should score higher than single-system chunks
        assert scores_by_id["A"] > scores_by_id["C"]
        assert scores_by_id["A"] > scores_by_id["D"]
        assert scores_by_id["B"] > scores_by_id["C"]
        assert scores_by_id["B"] > scores_by_id["D"]

        # Check sources for overlap chunks
        a_item = next(item for item in output if item["chunk_id"] == "A")
        b_item = next(item for item in output if item["chunk_id"] == "B")
        assert set(a_item["sources"]) == {"fts", "vector"}
        assert set(b_item["sources"]) == {"fts", "vector"}

    def test_deduplication(self):
        """Test that duplicate chunk_ids are deduplicated."""
        fusion = RRFFusion()

        # Same chunk appears in multiple positions in same system
        # (This is an edge case that shouldn't happen, but we handle it)
        results = {
            "fts": [
                {"chunk_id": "A", "content": "chunk A"},
                {"chunk_id": "B", "content": "chunk B"},
            ],
            "vector": [
                {"chunk_id": "A", "content": "chunk A", "extra": "data"},
                {"chunk_id": "C", "content": "chunk C"},
            ]
        }

        output = fusion.fuse(results)

        # Should have 3 unique chunks (A, B, C)
        assert len(output) == 3
        chunk_ids = [item["chunk_id"] for item in output]
        assert len(chunk_ids) == len(set(chunk_ids))  # All unique

    def test_rank_calculation_formula(self):
        """Verify RRF formula: score = 1 / (k + rank) with k=60."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {"chunk_id": "A", "content": "chunk A"},
                {"chunk_id": "B", "content": "chunk B"},
            ]
        }

        output = fusion.fuse(results)

        # For A at rank 1: score = 1 / (60 + 1) = 1/61 ≈ 0.01639
        # For B at rank 2: score = 1 / (60 + 2) = 1/62 ≈ 0.01613

        a_item = next(item for item in output if item["chunk_id"] == "A")
        b_item = next(item for item in output if item["chunk_id"] == "B")

        expected_a_score = 1.0 / (60 + 1)
        expected_b_score = 1.0 / (60 + 2)

        assert abs(a_item["rrf_score"] - expected_a_score) < 1e-6
        assert abs(b_item["rrf_score"] - expected_b_score) < 1e-6

    def test_score_sorting_descending(self):
        """Test that results are sorted by RRF score in descending order."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {"chunk_id": "A"},
                {"chunk_id": "B"},
                {"chunk_id": "C"},
                {"chunk_id": "D"},
                {"chunk_id": "E"},
            ]
        }

        output = fusion.fuse(results)

        # Extract scores
        scores = [item["rrf_score"] for item in output]

        # Should be in descending order
        assert scores == sorted(scores, reverse=True)

        # First item should have highest score
        assert scores[0] > scores[-1]

    def test_custom_k_parameter(self):
        """Test that custom k parameter works."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {"chunk_id": "A"},
            ]
        }

        # Use k=10 instead of default 60
        output = fusion.fuse(results, k=10)

        # Score should be 1 / (10 + 1) = 1/11 ≈ 0.0909
        expected_score = 1.0 / (10 + 1)
        assert abs(output[0]["rrf_score"] - expected_score) < 1e-6

    def test_metadata_preservation(self):
        """Test that chunk metadata is preserved in output."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {
                    "chunk_id": "A",
                    "content": "test content",
                    "file_path": "/path/to/file.py",
                    "start_line": 10,
                    "breadcrumb": "module.function"
                }
            ]
        }

        output = fusion.fuse(results)

        # Check that all original metadata is preserved
        assert output[0]["chunk_id"] == "A"
        assert output[0]["content"] == "test content"
        assert output[0]["file_path"] == "/path/to/file.py"
        assert output[0]["start_line"] == 10
        assert output[0]["breadcrumb"] == "module.function"

        # Plus added fields
        assert "rrf_score" in output[0]
        assert "sources" in output[0]

    def test_missing_chunk_id(self):
        """Test handling of chunks without chunk_id (should be skipped)."""
        fusion = RRFFusion()

        results = {
            "fts": [
                {"chunk_id": "A"},
                {"no_id": "missing"},  # Missing chunk_id
                {"chunk_id": "B"},
            ]
        }

        output = fusion.fuse(results)

        # Should only have A and B (missing chunk_id is skipped)
        assert len(output) == 2
        chunk_ids = [item["chunk_id"] for item in output]
        assert set(chunk_ids) == {"A", "B"}

    def test_example_from_plan(self):
        """Test exact example from plan documentation."""
        fusion = RRFFusion()

        # Example from plan:
        # FTS results: [A, B, C]
        # Vector results: [B, D, A]
        # Expected scores (k=60):
        # - A: 1/(60+1) + 1/(60+3) = 0.0164 + 0.0159 = 0.0323
        # - B: 1/(60+2) + 1/(60+1) = 0.0161 + 0.0164 = 0.0325
        # - C: 1/(60+3) = 0.0159
        # - D: 1/(60+2) = 0.0161
        # Final order: B, A, D, C

        results = {
            "fts": [
                {"chunk_id": "A"},
                {"chunk_id": "B"},
                {"chunk_id": "C"},
            ],
            "vector": [
                {"chunk_id": "B"},
                {"chunk_id": "D"},
                {"chunk_id": "A"},
            ]
        }

        output = fusion.fuse(results)

        # Verify order
        chunk_ids = [item["chunk_id"] for item in output]
        assert chunk_ids == ["B", "A", "D", "C"]

        # Verify exact scores
        scores_by_id = {item["chunk_id"]: item["rrf_score"] for item in output}

        expected_scores = {
            "A": 1.0/(60+1) + 1.0/(60+3),  # 0.0323
            "B": 1.0/(60+2) + 1.0/(60+1),  # 0.0325
            "C": 1.0/(60+3),                # 0.0159
            "D": 1.0/(60+2),                # 0.0161
        }

        for chunk_id, expected_score in expected_scores.items():
            assert abs(scores_by_id[chunk_id] - expected_score) < 1e-6
