INSERT INTO departments(name) VALUES ('运营'),('广告'),('设计'),('供应链'),('产品'),('认证'),('财务');

INSERT INTO app_users(id,email,display_name,department_id,role_code)
SELECT '10000000-0000-0000-0000-000000000001','owner@example.com','项目负责人',id,'project_owner'
FROM departments WHERE name='运营';

INSERT INTO products(id,model,name,brand,marketplace,asin,owner_id,target_launch_date,current_stage)
VALUES ('20000000-0000-0000-0000-000000000001','G11','智能安防摄像头（正式名称待确认）','HOMA','US','B0GXZ848Q1','10000000-0000-0000-0000-000000000001','2027-02-17','上线准备');

INSERT INTO workflow_templates(id,code,name,product_type,version)
VALUES ('30000000-0000-0000-0000-000000000001','camera-launch-v1','摄像头新品上线','smart_camera',1);

INSERT INTO product_workflows(id,product_id,workflow_template_id,template_version)
VALUES ('40000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','30000000-0000-0000-0000-000000000001',1);

INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,status,weight,due_date,evidence_required,approval_required) VALUES
('40000000-0000-0000-0000-000000000001','产品与合规','确认正式产品名称、型号和能力边界',1,'in_progress',3,'2026-11-30',true,true),
('40000000-0000-0000-0000-000000000001','产品与合规','确认FCC、危险品及标签状态',2,'not_started',5,'2026-12-05',true,true),
('40000000-0000-0000-0000-000000000001','供应链','确认生产、头程及入仓周期',3,'not_started',5,'2026-11-28',true,false),
('40000000-0000-0000-0000-000000000001','成本与定价','完成SKU成本卡和预算边界',4,'not_started',4,'2026-12-08',true,true),
('40000000-0000-0000-0000-000000000001','Listing','完成标题、五点和关键词库',5,'not_started',3,'2027-01-05',true,true),
('40000000-0000-0000-0000-000000000001','素材','完成主图、附图、A+和视频',6,'not_started',4,'2027-01-12',true,true),
('40000000-0000-0000-0000-000000000001','库存','首批货件入仓并达到可售状态',7,'not_started',5,'2027-02-03',true,false),
('40000000-0000-0000-0000-000000000001','广告','完成首发广告结构、预算与否词库',8,'not_started',3,'2027-02-10',true,true),
('40000000-0000-0000-0000-000000000001','上线验收','完成页面、库存、价格、促销和广告验收',9,'not_started',5,'2027-02-16',true,true),
('40000000-0000-0000-0000-000000000001','上线后60天','完成首轮搜索词、广告位与竞品复盘',10,'not_started',3,'2027-02-24',true,false);

INSERT INTO tracking_queries(id,product_id,keyword,marketplace,postal_code,max_pages,runs_per_day) VALUES
('50000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','indoor security camera','US','10001',3,4),
('50000000-0000-0000-0000-000000000002','20000000-0000-0000-0000-000000000001','wifi security camera','US','10001',3,4),
('50000000-0000-0000-0000-000000000003','20000000-0000-0000-0000-000000000001','home security camera','US','10001',3,4);

INSERT INTO rank_observations(tracking_query_id,product_id,asin,observation_type,observed_at,rank_value,page_number,visible_position) VALUES
('50000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','B0GXZ848Q1','organic','2026-11-08 02:00+00',42,1,50),
('50000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','B0GXZ848Q1','organic','2026-11-10 02:00+00',35,1,42),
('50000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','B0GXZ848Q1','organic','2026-11-12 02:00+00',39,1,47),
('50000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001','B0GXZ848Q1','organic','2026-11-14 02:00+00',31,1,38);

INSERT INTO agent_findings(product_id,risk,finding_type,title,explanation,evidence,proposed_action) VALUES
('20000000-0000-0000-0000-000000000001','critical','launch_dependency','产品能力和合规边界尚未确认','正式产品能力、订阅边界、FCC、危险品及标签状态未形成可审批证据，后续Listing和发货均存在返工风险。','{"affected_stages":["Listing","素材","库存"]}','先完成产品能力表和合规证据包，再放行Listing与首批发货。'),
('20000000-0000-0000-0000-000000000001','warning','ranking_baseline','关键词排名样本不足','当前只有少量示范观测，尚不能判断趋势或归因。','{"required_days":14,"current_days":4}','固定邮编、设备和频率，连续采集14天后再建立排名基线。');

INSERT INTO schema_migrations(version) VALUES ('001_schema.sql'),('002_seed.sql') ON CONFLICT DO NOTHING;
