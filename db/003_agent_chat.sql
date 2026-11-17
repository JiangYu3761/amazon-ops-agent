CREATE TABLE agent_conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  user_id uuid REFERENCES app_users(id),
  title text,
  current_page text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
  role text NOT NULL CHECK(role IN ('user','assistant','system')),
  content text NOT NULL,
  page_context text NOT NULL,
  requested_scope text NOT NULL,
  context_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  model_name text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX agent_messages_conversation_time_idx
  ON agent_messages(conversation_id, created_at);

INSERT INTO schema_migrations(version) VALUES ('003_agent_chat.sql') ON CONFLICT DO NOTHING;

