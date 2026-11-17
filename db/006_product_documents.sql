CREATE TABLE product_documents (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 title text NOT NULL CHECK(length(trim(title))>0),
 url text NOT NULL CHECK(url ~* '^https?://'),
 version text NOT NULL DEFAULT 'v1',
 preparation_status text NOT NULL DEFAULT 'preparing' CHECK(preparation_status IN ('preparing','prepared','needs_update')),
 supersedes_id uuid REFERENCES product_documents(id),
 evidence_id uuid REFERENCES task_evidence(id),
 is_current boolean NOT NULL DEFAULT true,
 note text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX product_documents_product_task_idx ON product_documents(product_id,task_id,is_current);
CREATE UNIQUE INDEX product_documents_supersedes_idx ON product_documents(supersedes_id) WHERE supersedes_id IS NOT NULL;
INSERT INTO schema_migrations(version) VALUES ('006_product_documents.sql') ON CONFLICT DO NOTHING;
