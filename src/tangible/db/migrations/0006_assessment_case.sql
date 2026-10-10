-- Link each assessment to the case it was signed from. Nullable: assessments are append-only and
-- rows made before this migration (or outside a case) have no case. One assessment per case.
ALTER TABLE assessment ADD COLUMN case_id uuid REFERENCES case_file (id);
CREATE UNIQUE INDEX assessment_one_per_case ON assessment (case_id) WHERE case_id IS NOT NULL;
