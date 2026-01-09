-- Feedback table for accuracy measurement
CREATE TABLE feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  retrieval_id UUID NOT NULL REFERENCES retrievals(id) ON DELETE CASCADE, -- Feedback is on specific retrieval
  feedback_type VARCHAR(20) NOT NULL CHECK (feedback_type IN ('positive', 'negative', 'neutral')),
  feedback_text TEXT, -- Optional user comment explaining why (nullable)
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_feedback_retrieval_id ON feedback(retrieval_id);
CREATE INDEX idx_feedback_type ON feedback(feedback_type); -- For aggregating positive vs negative
CREATE INDEX idx_feedback_created_at ON feedback(created_at DESC); -- For recent feedback
