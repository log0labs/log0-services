-- When an AI summary was last asked for; lets the API tell "generating" from "failed".
ALTER TABLE incident ADD COLUMN IF NOT EXISTS ai_summary_requested_at TIMESTAMPTZ;
