-- Multi-stage approval cases coexist with historical single-node submissions.
CREATE TABLE approval_configs (
 task_id uuid PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
 version integer NOT NULL DEFAULT 1,
 definition jsonb NOT NULL,
 updated_by uuid NOT NULL REFERENCES app_users(id),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE approval_config_history (
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 version integer NOT NULL,
 definition jsonb NOT NULL,
 actor_id uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(task_id,version)
);
CREATE TABLE approval_cases (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 task_id uuid NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
 number text NOT NULL UNIQUE,
 version integer NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_progress','completed')),
 config_version integer NOT NULL,
 definition jsonb NOT NULL,
 application_answers jsonb NOT NULL DEFAULT '{}'::jsonb,
 stages jsonb NOT NULL,
 current_stage_key text,
 initiator_id uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 submitted_at timestamptz,
 completed_at timestamptz
);
CREATE TABLE approval_events (
 id bigserial PRIMARY KEY,
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 case_id uuid REFERENCES approval_cases(id) ON DELETE CASCADE,
 stage_key text,
 action text NOT NULL,
 actor_id uuid NOT NULL REFERENCES app_users(id),
 payload jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approval_events_task_idx ON approval_events(task_id,id DESC);
CREATE TABLE approval_files (
 id uuid PRIMARY KEY,
 task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 case_id uuid NOT NULL REFERENCES approval_cases(id) ON DELETE CASCADE,
 stage_key text NOT NULL,
 filename text NOT NULL,
 size integer NOT NULL CHECK(size>0 AND size<=10485760),
 sha256 text NOT NULL,
 uploaded_by uuid NOT NULL REFERENCES app_users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
-- Canonical keys are renamed only when no newer task already occupies that key.
-- Task IDs and all linked evidence/forms/approval records stay intact.
INSERT INTO audit_logs(entity_type,entity_id,action,before_data,after_data)
SELECT 'task',t.id::text,'compliance_key_mapped',jsonb_build_object('title',t.title,'sop_key',t.sop_key),
 jsonb_build_object('sop_key',m.new_key) FROM tasks t JOIN (VALUES
 ('certification','fcc-certification'),('dangerous','dangerous-goods'),('label','label-confirmation')) m(old_key,new_key) ON t.sop_key=m.old_key
WHERE NOT EXISTS(SELECT 1 FROM tasks newer WHERE newer.product_workflow_id=t.product_workflow_id AND newer.sop_key=m.new_key);
UPDATE tasks t SET sop_key=m.new_key FROM (VALUES
 ('certification','fcc-certification'),('dangerous','dangerous-goods'),('label','label-confirmation')) m(old_key,new_key)
WHERE t.sop_key=m.old_key AND NOT EXISTS(SELECT 1 FROM tasks newer WHERE newer.product_workflow_id=t.product_workflow_id AND newer.sop_key=m.new_key);
-- Retain the original task ID and all evidence, single-node forms and approvals.
INSERT INTO audit_logs(entity_type,entity_id,action,before_data,after_data)
SELECT 'task',id::text,'compliance_split',jsonb_build_object('title',title,'sop_key',sop_key),
 jsonb_build_object('title','FCC 认证','sop_key','fcc-certification') FROM tasks
WHERE title='确认FCC、危险品及标签状态';
UPDATE tasks t SET title='FCC 认证',sop_key=CASE WHEN NOT EXISTS(SELECT 1 FROM tasks newer WHERE newer.product_workflow_id=t.product_workflow_id AND newer.sop_key='fcc-certification' AND newer.id<>t.id) THEN 'fcc-certification' ELSE t.sop_key END
WHERE t.title='确认FCC、危险品及标签状态';
INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,weight,due_date,evidence_required,approval_required,module_code,phase,department,sop_key,acceptance_criteria)
SELECT t.product_workflow_id,'产品与合规',x.title,t.sequence+x.sequence_delta,1,t.due_date,true,true,'compliance',t.phase,'合规',x.key,x.acceptance
FROM tasks t CROSS JOIN (VALUES
 ('dangerous-goods','危险品判定',1,'保存危险品材料、判定结果、审核意见与归档记录。'),
 ('label-confirmation','标签确认',2,'保存标签初稿、合规审核、修订、定稿及使用验收记录。')
) x(key,title,sequence_delta,acceptance)
WHERE t.sop_key='fcc-certification' AND t.id=(
 SELECT candidate.id FROM tasks candidate JOIN product_workflows cw ON cw.id=candidate.product_workflow_id
 JOIN product_workflows tw ON tw.product_id=cw.product_id WHERE tw.id=t.product_workflow_id AND candidate.sop_key='fcc-certification'
 ORDER BY cw.started_at DESC,candidate.updated_at DESC,candidate.id LIMIT 1) AND NOT EXISTS(
 SELECT 1 FROM tasks existing JOIN product_workflows ew ON ew.id=existing.product_workflow_id JOIN product_workflows tw ON tw.id=t.product_workflow_id WHERE ew.product_id=tw.product_id AND existing.sop_key=x.key)
ON CONFLICT DO NOTHING;
INSERT INTO task_dependencies(task_id,depends_on_task_id)
SELECT label.id,fcc.id FROM tasks label JOIN tasks fcc ON fcc.product_workflow_id=label.product_workflow_id
WHERE label.sop_key='label-confirmation' AND fcc.sop_key='fcc-certification'
ON CONFLICT DO NOTHING;
INSERT INTO task_dependencies(task_id,depends_on_task_id)
SELECT label.id,dangerous.id FROM tasks label JOIN product_workflows lw ON lw.id=label.product_workflow_id
JOIN product_workflows dw ON dw.product_id=lw.product_id JOIN tasks dangerous ON dangerous.product_workflow_id=dw.id
WHERE label.sop_key='label-confirmation' AND dangerous.sop_key='dangerous-goods'
ON CONFLICT DO NOTHING;
INSERT INTO task_dependencies(task_id,depends_on_task_id)
SELECT dangerous.id,capability.id FROM tasks dangerous JOIN product_workflows dw ON dw.id=dangerous.product_workflow_id
JOIN product_workflows cw ON cw.product_id=dw.product_id JOIN tasks capability ON capability.product_workflow_id=cw.id
WHERE dangerous.sop_key='dangerous-goods' AND (capability.sop_key='capability' OR capability.title='确认正式产品名称、型号和能力边界')
ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version) VALUES('008_approval_cases.sql') ON CONFLICT DO NOTHING;
