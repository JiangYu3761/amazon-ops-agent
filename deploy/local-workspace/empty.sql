-- Reference data required by subsequent migrations, without demo users or business records.
BEGIN;
INSERT INTO departments(name) VALUES ('运营'),('广告'),('设计'),('供应链'),('产品'),('认证'),('财务');
INSERT INTO workflow_templates(id,code,name,product_type,version)
VALUES ('30000000-0000-0000-0000-000000000001','camera-launch-v1','摄像头新品上线','smart_camera',1);
INSERT INTO schema_migrations(version) VALUES ('001_schema.sql'),('002_seed.sql');
COMMIT;
