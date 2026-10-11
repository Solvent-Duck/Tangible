-- Prompt-cache usage per model call. Bedrock reports input_tokens as the non-cached remainder, so
-- without these the row's tokens look too small for its cost. Existing rows made no cache traffic.
ALTER TABLE model_call
    ADD COLUMN cache_read_tokens  int NOT NULL DEFAULT 0,
    ADD COLUMN cache_write_tokens int NOT NULL DEFAULT 0;
