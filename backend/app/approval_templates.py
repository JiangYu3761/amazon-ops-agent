"""Business-specific approval templates. Definitions contain no live people or dates.

All catalog tasks resolve to a named template. Running approval sheets retain their
own immutable definition snapshot; changing this registry only affects new sheets.
"""
from copy import deepcopy


def field(key, label, kind='text', required=False, help='', options=None):
    return dict(id=key, label=label, type=kind, required=required, help=help, options=options or [])


def stage(key, name, kind, fields, return_to=None):
    result=dict(key=key, name=name, type=kind, fields=fields)
    if return_to:
        result['return_to']=return_to
    return result


ALIASES={'certification':'fcc-certification','dangerous':'dangerous-goods','label':'label-confirmation'}
LEGACY_TITLES={
    '确认正式产品名称、型号和能力边界':'capability',
    '确认FCC、危险品及标签状态':'fcc-certification',
    '确认生产、头程及入仓周期':'supply-lead',
    '完成SKU成本卡和预算边界':'unit-cost',
    '完成标题、五点和关键词库':'copy',
    '完成主图、附图、A+和视频':'images',
    '首批货件入仓并达到可售状态':'receiving',
    '完成首发广告结构、预算与否词库':'ad-launch',
    '完成页面、库存、价格、促销和广告验收':'launch-gate',
    '完成首轮搜索词、广告位与竞品复盘':'weekly-review',
}

# Each row is a deliberate application form for one existing SOP matter.
# Syntax: field ID : label : kind (default text). No generic three-field fallback
# is used for the catalog, and unknown/custom tasks retain their own scope form.
DETAILS={
 'account':('主体及收款资料核验','entity:店铺主体法定名称|marketplace:申请站点|registration_no:主体登记编号|beneficiary:收款账户归属|discrepancy:主体与收款资料差异:textarea'),
 'roles':('后台权限申请与交接','account_scope:后台账户与站点|job_roles:拟开通或撤回的岗位权限:textarea|permission_reason:权限用途与最小授权依据:textarea|handover_person:交接对象|revoke_date:离职或权限撤回日期:date'),
 'brand':('品牌备案及授权核验','brand_name:品牌与商标名称|trademark_no:商标注册或申请编号|trademark_owner:权利人与授权主体|authorization_scope:授权市场与期限:textarea|brand_status:备案当前状态'),
 'account-health':('账户健康检查与异常闭环','check_period:检查周期|marketplace:站点|metric_scope:绩效指标与通知范围:textarea|case_deadline:最早申诉或处理期限:date|health_findings:异常及风险项:textarea'),
 'case':('Case 材料提交与结果复核','case_id:Case编号|issue_type:问题类别|appeal_request:诉求与事实依据:textarea|platform_deadline:平台回复或申诉截止:date|case_materials:需要提交的材料清单:textarea'),
 'store-design':('品牌旗舰店制作与验收','store_name:旗舰店名称与站点|page_structure:页面结构与跳转关系:textarea|asset_scope:所需图片与内容清单:textarea|brand_rules:品牌表达与合规要求:textarea'),
 'store-audit':('旗舰店巡检与修复验收','store_url:旗舰店链接|audit_date:巡检日期:date|broken_links:失效链接及影响页面:textarea|brand_deviations:品牌表达偏差:textarea'),
 'handover':('运营资料与权限交接','handover_scope:交接产品与站点|outgoing_person:移交人员|incoming_person:接手人员|open_risks:未闭环风险与截止日期:textarea|permissions_inventory:权限和资料移交清单:textarea'),
 'capability':('产品型号与能力主档确认','model_revision:产品型号与硬件版本|capabilities:核心功能与参数:textarea|supported_regions:服务区域与限制|unverified_claims:尚未验证的对外宣称:textarea'),
 'subscription':('免费与订阅功能确认','service_region:服务市场与地区|free_features:免费功能及限制:textarea|paid_features:订阅功能与收费条件:textarea|renewal_terms:续费取消与到期处理:textarea'),
 'transparency':('透明计划资格与标签授权','brand_name:品牌名称|enrollment_scope:适用型号和站点|eligibility_basis:资格与授权依据:textarea|code_source:标签或编码来源|authorization_end:授权截止日期:date'),
 'privacy':('隐私与数据功能声明审核','data_categories:收集处理的数据类别:textarea|processing_regions:处理与存储地区|consent_flow:授权与删除路径:textarea|public_statement:对外声明版本和适用功能:textarea'),
 'compliance-pack':('合规证据归档与发货放行','shipment_scope:放行批次与型号|certification_refs:认证报告索引:textarea|dangerous_result:危险品判定结论|label_revision:获准标签版本|unresolved_risks:未解决合规问题:textarea'),
 'position':('产品定位研究与确认','target_audience:目标人群与购买动机:textarea|core_scenarios:核心使用场景:textarea|selling_points:卖点及优先顺序:textarea|claim_evidence:差异化主张依据:textarea'),
 'keywords':('关键词研究与词库审核','marketplace:目标站点与语言|keyword_sources:词源与采集日期:textarea|core_terms:核心词与场景词:textarea|exclusions:不适用或禁用词:textarea|mapping_plan:页面埋点位置:textarea'),
 'competitors':('竞品页面档案与比较','competitor_asins:竞品ASIN清单:textarea|comparison_date:比较日期:date|comparison_dimensions:价格功能素材等比较维度:textarea|evidence_scope:页面证据与观察边界:textarea'),
 'category':('类目属性与变体结构审核','category_path:拟用类目与商品类型|required_attributes:必填属性与取值:textarea|sku_structure:SKU及父子体关系:textarea|variation_basis:变体主题及适用依据:textarea'),
 'listing-create':('Listing 创建与可见性验收','marketplace:目标站点|sku:SKU与拟绑定ASIN|creation_method:创建方式与销售权限|attribute_revision:获准属性表版本|visibility_checks:前台可见性检查项:textarea'),
 'copy':('Listing 文案制作与审核','language:文案语言与站点|keyword_revision:词库版本|title_proposal:标题方案:textarea|bullet_scope:五点卖点与功能边界:textarea|claim_limits:禁用宣称与订阅限制:textarea'),
 'images':('主附图需求制作与验收','asset_count:主图与附图数量:number|shooting_scope:拍摄场景与展示重点:textarea|visual_claims:图片表达与功能证据:textarea|file_specs:尺寸格式与交付命名:textarea|brief_revision:需求稿版本'),
 'aplus':('A+ 与品牌故事制作发布','module_plan:A+模块与品牌故事结构:textarea|copy_revision:已审批文案版本|image_revision:已审批素材版本|mobile_checks:移动端验收要求:textarea'),
 'video':('视频脚本制作与功能核对','duration_seconds:目标时长秒:number|script_scope:脚本分镜与功能演示:textarea|subtitle_language:字幕语言|music_license:音乐素材授权依据|delivery_specs:画幅分辨率与交付格式:textarea'),
 'listing-qa':('Listing 上线联合验收','listing_url:待验收Listing链接|content_revision:文案与素材版本|attribute_checks:属性变体及权限核对项:textarea|mobile_scope:桌面与移动端检查范围:textarea'),
 'listing-test':('页面变更申请与效果验证','baseline_revision:当前页面版本|change_hypothesis:变更假设与预期效果:textarea|change_scope:拟修改的位置与内容:textarea|observation_window:验证周期与样本要求|rollback_rule:保留或回滚标准:textarea'),
 'listing-health':('页面异常排查与恢复','listing_url:异常页面链接|detected_at:发现日期:date|symptom:抑制购物车或异常变更情况:textarea|affected_scope:影响站点与SKU|recovery_target:恢复目标与平台期限:textarea'),
 'ad-goal':('广告目标与预算边界审批','business_stage:产品经营阶段|ad_objective:广告目标与优先级:textarea|budget_amount:阶段预算:number|profit_boundary:利润与库存约束:textarea|stop_conditions:停止条件:textarea'),
 'ad-map':('广告目的与定向框架审核','objective_revision:已审批目标版本|ad_types:SP SB Display等适用类型|target_modules:关键词商品受众模块:textarea|stage_plan:阶段目标及调整条件:textarea'),
 'ad-structure':('广告结构与命名实施','naming_pattern:Campaign与Ad Group命名规范|campaign_map:结构与预算层级:textarea|marketplace:投放站点|ownership:维护责任与账户范围:textarea'),
 'ad-keywords':('广告关键词及定向审核','keyword_revision:关键词库与竞品档案版本|match_types:匹配方式及用途:textarea|product_targets:商品定向对象与依据:textarea|initial_negatives:初始否词与排除范围:textarea'),
 'ad-creative':('广告素材与落地页验收','ad_format:广告类型与素材规格|asset_revision:待审核素材版本|landing_url:落地页链接|rights_scope:品牌权限与素材授权|policy_checks:广告政策与表达核对项:textarea'),
 'ad-launch':('广告上线设置审批','campaign_names:拟上线Campaign清单:textarea|daily_budget:每日预算:number|bid_plan:竞价与广告位调整:textarea|launch_date:拟上线日期:date|launch_stop_rule:上线检查及停止条件:textarea'),
 'ad-search':('搜索词与商品匹配复盘','report_period:报表观察期间|attribution_window:归因成熟窗口|search_scope:Campaign与搜索词范围:textarea|matched_asin_scope:MatchedASIN范围|maturity_limits:样本与数据成熟限制:textarea'),
 'ad-placement':('广告位与转化结构诊断','report_period:诊断期间|placement_scope:广告位与Campaign范围:textarea|baseline_period:比较基线期间|conversion_findings:点击与转化结构变化:textarea'),
 'ad-budget':('广告预算竞价调整审批','current_budget:当前预算:number|proposed_budget:拟调整预算:number|bid_change:拟调整竞价范围:textarea|inventory_constraint:库存与利润约束:textarea|validation_rule:效果验证与回滚条件:textarea'),
 'ad-negatives':('否词与流量迁移审批','negative_terms:拟否定关键词或ASIN:textarea|match_scope:否定匹配方式与应用层级|migration_destination:流量迁移目的结构|collateral_scope:可能误伤的流量范围:textarea|evidence_window:依据报表与成熟归因期间'),
 'ad-verify':('广告变更效果复核','change_reference:变更申请或记录编号|baseline_period:变更前基线期间|verification_period:变更后验证期间|success_conditions:效果判定条件:textarea|rollback_conditions:回滚触发条件:textarea'),
 'supply-lead':('交期询证与周期确认','supplier:供应商与生产地点|production_days:生产周期天:number|transit_days:头程周期天:number|receiving_days:入仓周期天:number|variance:交期波动与风险:textarea'),
 'forecast':('销量情景与备货方案审批','forecast_period:预测覆盖期间|conservative_units:保守销量:number|base_units:基准销量:number|optimistic_units:积极销量:number|forecast_basis:预测依据与库存边界:textarea'),
 'purchase':('采购数量与付款节点审批','supplier:采购供应商|purchase_units:采购数量:number|currency:采购币种|purchase_amount:采购金额:number|payment_milestones:付款节点与条件:textarea'),
 'production':('生产排期质检与完工验收','purchase_reference:采购订单编号|factory:工厂与生产地点|scheduled_start:原计划开工日期:date|scheduled_finish:原计划完工日期:date|quality_requirements:质检标准与抽检范围:textarea'),
 'shipment':('货件创建装箱与发运验收','shipment_batch:出货批次与货件编号|warehouse:目的仓与收货地址|carton_specs:箱规件数和重量:textarea|label_revision:获准箱标产品标签版本|packing_list:装箱资料要求:textarea'),
 'first-mile':('头程承运与物流节点跟踪','carrier:承运商与联系人|tracking_number:运单或提单编号|shipping_route:运输方式与线路|freight_cost:预计头程费用:number|planned_arrival:原计划到仓日期:date'),
 'receiving':('仓库签收与可售库存验收','shipment_reference:货件和运单编号|expected_units:应收数量:number|warehouse:目的仓|planned_receipt:原计划签收日期:date|reconciliation_scope:接收差异核对要求:textarea'),
 'stock-audit':('多状态库存口径核对','snapshot_date:库存快照日期:date|fulfillable:可售数量:number|reserved:预留数量:number|inbound:在途数量:number|purchase_units:采购未发数量:number|discrepancies:口径差异与原因:textarea'),
 'replenish':('补货计划与断货预警审批','planning_date:补货测算日期:date|safety_stock:安全库存数量:number|reorder_point:补货点数量:number|proposed_units:建议补货数量:number|stockout_estimate:预计断货日期:date|supply_risk:供货和现金限制:textarea'),
 'aging':('库龄滞销与移除方案审批','snapshot_date:库龄快照日期:date|age_buckets:库龄分布与数量:textarea|capacity_limit:容量或仓储约束|proposed_action:降价移除等处理方案:textarea|financial_impact:费用与回收金额估算:textarea'),
 'reconcile-shipment':('货件差异与赔偿申诉','shipment_reference:货件与运单编号|loss_type:丢失破损或接收差异|disputed_units:争议数量:number|claim_deadline:索赔截止日期:date|claim_basis:索赔依据与材料清单:textarea'),
 'pricing':('常规售价与价格底线审批','currency:售价币种|regular_price:拟常规售价:number|floor_price:价格底线:number|promotion_tiers:促销价格梯度:textarea|margin_basis:毛利与费用依据:textarea'),
 'events':('活动报名与日历排期审批','event_name:活动名称与站点|event_window:活动起止期间|registration_deadline:报名截止日期:date|eligibility:活动资格与限制:textarea|event_owner:报名与执行责任人'),
 'event-margin':('活动折扣费用与贡献利润审核','event_reference:活动名称或申请编号|discount_rate:拟用折扣与优惠叠加规则|platform_fees:平台活动费用:number|contribution_estimate:预计贡献利润:number|missing_costs:缺失费用和测算限制:textarea'),
 'event-stock':('活动库存配送与广告准备','event_window:活动期间|required_stock:活动需求库存:number|delivery_plan:配送能力与到仓安排:textarea|advertising_budget:活动广告预算:number|supply_gap:库存或配送缺口:textarea'),
 'coupon':('优惠券与促销展示验收','coupon_scope:适用SKU与站点|promotion_price:促销价或券额:number|promotion_window:生效及结束时间|stacking_rule:与其他优惠叠加规则|display_checks:前台展示和结算核对项:textarea'),
 'launch-gate':('产品上线联合放行','launch_date:目标上线日期:date|listing_reference:已验收页面版本|inventory_reference:可售库存确认依据|pricing_reference:价格与促销审批依据|advertising_reference:广告上线验收依据'),
 'event-review':('活动效果与库存复盘','event_reference:活动名称与期间|baseline_period:对比基线期间|actual_units:实际销量:number|actual_contribution:实际贡献利润:number|inventory_impact:活动后库存与补货影响:textarea'),
 'unit-cost':('SKU成本卡核算与复核','currency:核算币种|product_cost:产品单位成本:number|first_mile_cost:单位头程成本:number|fba_fee:FBA费用:number|referral_fee:佣金:number|return_allowance:退货及其他成本口径:textarea'),
 'budget':('经营分项预算审批','budget_period:预算期间|currency:预算币种|ad_budget:广告预算:number|asset_budget:素材预算:number|promotion_budget:活动预算:number|purchase_budget:采购预算:number|budget_conditions:资金及使用约束:textarea'),
 'cash-plan':('采购付款回款与现金缺口计划','planning_period:现金计划期间|opening_cash:期初可用现金:number|payment_schedule:采购付款节点:textarea|settlement_schedule:预计回款节点:textarea|cash_gap:现金缺口及应对:textarea'),
 'settlement':('平台销售退款与结算对账','settlement_period:结算期间|currency:结算币种|gross_sales:销售总额:number|refund_amount:退款金额:number|discount_amount:折扣金额:number|settled_amount:实际结算金额:number|reconciliation_gap:对账差异:textarea'),
 'profit':('单品贡献利润与费用复核','report_period:核算期间|currency:核算币种|revenue_basis:收入及退款口径:textarea|unit_cost_revision:完整成本卡版本|period_expenses:期间费用范围与分摊:textarea|missing_items:尚未接全的税费及其他费用:textarea'),
 'expense':('仓储广告与异常扣费核查','billing_period:账单期间|expense_type:仓储广告或其他费用类型|charged_amount:实际扣费:number|expected_amount:预计应扣金额:number|dispute_basis:异常差额及复核依据:textarea'),
 'cash-review':('现金预测与预算偏差复核','report_period:复核期间|approved_budget:已批预算:number|actual_spend:实际使用金额:number|updated_cash:最新可用现金:number|variance_reason:使用偏差与原因:textarea|corrective_actions:后续现金安排:textarea'),
 'service-sop':('客服响应退换货规则审批','service_scope:产品服务区域与渠道|response_sla:响应时限|return_policy:退换货条件与路径:textarea|escalation_path:问题升级岗位与时限:textarea'),
 'order':('订单配送异常处理','order_reference:订单或异常批次|exception_type:取消未配送或配送异常|affected_units:受影响订单或数量:number|customer_deadline:承诺回复日期:date|resolution_proposal:处理方案与费用边界:textarea'),
 'returns':('退款退货原因与损失复核','analysis_period:统计期间|return_count:退款退货数量:number|refund_loss:退款及处理损失:number|reason_categories:退货原因分类:textarea|followup_actions:售后改进行动:textarea'),
 'voc':('买家之声与体验问题复核','collection_period:反馈收集期间|feedback_sources:买家之声及反馈来源|negative_topics:负面体验主题与频次:textarea|affected_models:受影响型号|evidence_boundary:样本偏差与信息边界:textarea'),
 'service-content':('售后FAQ制作与发布审核','content_scope:安装连接订阅等问题范围|supported_versions:适用产品与固件版本|troubleshooting_steps:排查与解决步骤:textarea|escalation_conditions:需要升级处理的条件:textarea|language:发布语言与渠道'),
 'product-feedback':('重复产品问题与修复验证','problem_reference:问题编号与关联退货反馈|reproduction_steps:复现步骤及影响范围:textarea|severity:问题等级与用户影响|proposed_fix:修复措施与版本:textarea|verification_conditions:验证样本与通过条件:textarea'),
 'data-definition':('经营数据口径确认','marketplace:站点范围|currency:统一币种|report_timezone:报表时区|data_grain:记录粒度与业务键|attribution_window:归因窗口及成熟规则:textarea'),
 'data-source':('经营数据源配置与导入验收','source_types:销售广告库存成本等来源|report_period:导入期间|field_mapping:报表字段与业务键映射:textarea|identity_checks:币种SKU及ASIN校验要求:textarea|reconciliation_rule:导入前后核对规则:textarea'),
 'rank-plan':('关键词排名监控配置审批','keywords:监控关键词清单:textarea|marketplace:站点与语言|postal_code:固定邮编|device:设备类型|runs_per_day:每日采集频率:number|evidence_rule:采集证据及异常处理要求:textarea'),
 'rank-evidence':('排名观测与采集证据复核','observation_period:观测期间|ranking_types:自然位广告位BSR各自口径|query_parameters:邮编设备关键词参数:textarea|missing_runs:缺失与失败观测:textarea|evidence_location:采集证据存放位置'),
 'daily-review':('运营日检与异常分派','inspection_date:日检日期:date|inspection_scope:页面库存广告账户检查范围:textarea|detected_issues:已发现异常:textarea|handling_deadline:最早处理截止日期:date|escalation_rule:异常升级条件:textarea'),
 'weekly-review':('周度目标差距与行动复盘','report_week:复盘周次|target_baseline:本周目标与原计划:textarea|actual_results:实际结果与依据:textarea|variance:目标差距与原因:textarea|next_actions:行动负责人及期限:textarea'),
 'decision-log':('经营变更与回滚决策审批','change_reference:变更申请或问题编号|change_date:拟变更日期:date|evidence_basis:变更证据与假设:textarea|impact_scope:影响SKU页面广告或库存范围:textarea|rollback_rule:结果复核与回滚规则:textarea'),
 'day30':('上线30天经营复盘审批','launch_reference:上线日期与产品范围|period_results:前30天经营结果:textarea|target_gap:目标差距与原因:textarea|resource_request:资源调整建议:textarea|next_checkpoint:下一次复核日期:date'),
 'day60':('上线60天去留放量与补货决策','review_period:经营复盘期间|profit_evidence:利润与现金依据:textarea|advertising_evidence:广告验证结论:textarea|inventory_evidence:库存与补货约束:textarea|scale_decision:去留放量或收缩建议:textarea'),
}


def parse_fields(spec):
    fields=[]
    for entry in spec.split('|'):
        parts=entry.split(':')
        fields.append(field(parts[0],parts[1],parts[2] if len(parts)>2 else 'text',required=True))
    return fields


def attachments(key='supporting_files',label='申请依据与资料',required=False):
    return field(key,label,'attachment',required,help='上传文件或登记资料链接，注明版本；实际材料仅属于当前审批单。')


def approval_fields(label='核对意见与通过依据'):
    return [field('review_basis',label,'textarea',True),attachments('review_evidence','审核依据附件')]


def fcc_template():
    application=[field('certification_scope','认证范围及适用型号','textarea',True),
        field('requested_standard','拟申请标准或适用路径','text',True,help='由合规人员或机构确认适用路径，不自动认定所有产品均需同类证书。'),
        field('agency_name','拟委托认证机构'),field('laboratory','实验室或办理地点'),
        field('external_contact','机构联系人及联系方式'),
        field('planned_draft_date','原计划草稿反馈日期','date'),field('planned_original_date','原计划正本日期','date'),
        attachments('application_files','申请表、产品技术资料与样品清单',True)]
    stages=[
        stage('application','资料准备与提交','application',application),
        stage('manager_review','内部负责人审批','approval',approval_fields('送检范围、材料完整性与申请审核意见'),'application'),
        stage('testing','认证机构接单与测试','handling',[
            field('agency_name','实际承接机构','text',True),field('laboratory','测试实验室与地点','location',True),
            field('external_contact','外部联系人及联系方式','text',True),field('accepted_on','实际确认接单日期','date',True),
            field('test_started_on','实际测试开始日期','date'),field('committed_feedback_on','机构承诺反馈日期','date',True),
            field('test_progress','测试进展与待补件清单','textarea',True),field('rectification_deadline','补件或整改截止日期','date'),
            attachments('acceptance_receipt','机构接单回执与测试进展附件',True)]),
        stage('draft_feedback','草稿反馈','handling',[
            field('draft_received_on','实际收到草稿日期','date',True),field('sender','草稿发送人','text',True),
            field('recipient','内部接收人','text',True),field('draft_version','草稿版本','text',True),
            field('agency_notes','机构说明或本轮修订内容','textarea'),attachments('draft_files','草稿报告与说明',True)]),
        stage('draft_review','草稿审核与修订','approval',[
            field('model_identity_check','型号、主体及报告内容核对意见','textarea',True),
            field('revision_requests','需要补充或修订的内容','textarea'),field('revision_due_on','要求修订反馈日期','date'),
            attachments('marked_draft','批注稿及审核依据')],'draft_feedback'),
        stage('original','正本签发与接收','handling',[
            field('issued_on','实际签发日期','date',True),field('received_on','实际收到正本日期','date',True),
            field('report_number','正式报告或证书编号','text',True),field('applicable_models','正本适用型号','text',True),
            field('issuing_agency','签发机构','text',True),field('received_by','内部接收人','text',True),
            attachments('original_files','正本或正式报告',True)]),
        stage('archive_review','最终验收与归档','approval',[
            field('archive_location','最终文件归档位置','location',True),field('acceptance_result','最终资料验收结论','textarea',True),
            field('remaining_restrictions','适用限制或后续注意事项','textarea'),attachments('archive_files','归档索引或验收依据')],'original'),
    ]
    return dict(key='fcc-certification',name='FCC 认证审批流程',version=1,fields=application,stages=stages)


def dangerous_template():
    app=[field('product_composition','产品成分、电池及配件构成','textarea',True),field('shipping_modes','适用运输方式与目的市场','text',True),
         field('assessment_agency','拟判定机构或平台'),attachments('safety_materials','安全资料、SDS及产品说明',True)]
    return dict(key='dangerous-goods',name='危险品判定审批流程',version=1,fields=app,stages=[
        stage('application','判定材料提交','application',app),
        stage('agency_acceptance','机构或平台接单评估','handling',[field('external_contact','判定联系人或平台Case','text',True),field('accepted_on','实际接单日期','date',True),field('assessment_progress','评估进展与补充要求','textarea',True),attachments('assessment_receipt','接单回执与补件要求')]),
        stage('result_feedback','判定结果反馈','handling',[field('classification','危险品分类及适用限制','textarea',True),field('received_on','实际收到判定日期','date',True),attachments('classification_files','正式判定结果与运输要求',True)]),
        stage('internal_review','危险品结论内部复核','approval',approval_fields('成分、运输方式及平台限制复核意见'),'result_feedback'),
        stage('archive_review','结果归档与运输放行','approval',[field('transport_conditions','获准运输条件与执行限制','textarea',True),field('archive_location','判定资料归档位置','location',True)],'result_feedback')])


def label_template():
    app=[field('label_types','UPC、FNSKU、SN及型号标签范围','textarea',True),field('model_revision','适用型号与产品版本','text',True),field('certification_reference','认证及危险品判定依据'),field('print_specs','尺寸、材质与印刷要求','textarea',True),attachments('label_source','标签信息来源与要求')]
    return dict(key='label-confirmation',name='标签确认审批流程',version=1,fields=app,stages=[
        stage('application','标签信息汇总','application',app),
        stage('initial_design','标签设计初稿','handling',[field('design_revision','初稿版本','text',True),field('designer_contact','设计或供应商联系人'),attachments('label_draft','标签设计稿',True)]),
        stage('compliance_review','标签合规与编码核对','approval',approval_fields('编码、型号、主体、认证标记及警示语核对意见'),'initial_design'),
        stage('revision','修订与定稿','handling',[field('final_revision','获准定稿版本','text',True),field('revision_notes','审核意见逐项闭环','textarea',True),attachments('final_artwork','最终印刷或使用稿',True)]),
        stage('print_acceptance','印刷或使用验收','approval',[field('batch_scope','印刷批次或使用范围','text',True),field('scan_result','扫码、实物和版本核对结果','textarea',True),attachments('physical_evidence','标签样张或实物验收证据',True)],'revision')])


DEFINITION_KEYS={'position','keywords','competitors','category','ad-goal','ad-map','ad-keywords','supply-lead','forecast','rank-plan','data-definition'}
CONTENT_KEYS={'store-design','copy','images','aplus','video','service-content','service-sop'}
CHANGE_KEYS={'ad-budget','ad-negatives','ad-verify','listing-test','decision-log','aging','replenish','pricing','budget','purchase','cash-plan','day60'}
EXECUTION_KEYS={'listing-create','ad-structure','ad-launch','shipment','first-mile','coupon','events','production'}
INSPECTION_KEYS={'store-audit','listing-qa','listing-health','account-health','stock-audit','receiving','compliance-pack','launch-gate'}
CASE_KEYS={'case','order','reconcile-shipment','product-feedback'}


def family_stages(key, app, title):
    if key in CONTENT_KEYS:
        return [stage('application','需求与素材申请','application',app),
            stage('brief_review','需求与表达边界审核','approval',approval_fields('需求、功能边界与素材授权审核意见'),'application'),
            stage('production','制作与初稿提交','handling',[field('draft_revision','制作初稿版本','text',True),field('production_notes','制作内容与需求对应说明','textarea',True),attachments('draft_assets','初稿素材文件',True)]),
            stage('content_review','内容审核与修改反馈','approval',approval_fields('文字、画面、功能表达与交付规格核对意见'),'production'),
            stage('final_delivery','修订与定稿交付','handling',[field('final_revision','最终版本','text',True),field('feedback_closure','反馈修改逐项闭环','textarea',True),attachments('final_assets','定稿文件与使用说明',True)]),
            stage('acceptance','发布或交付验收','approval',approval_fields('定稿版本、使用位置与交付要求验收意见'),'final_delivery')]
    if key in DEFINITION_KEYS:
        return [stage('application','研究范围与资料登记','application',app),stage('scope_review','范围与依据审核','approval',approval_fields('研究范围、样本和适用边界审核意见'),'application'),
            stage('analysis','调查与方案编制','handling',[field('analysis_method','调查方法与数据来源','textarea',True),field('proposal','具体结论与候选方案','textarea',True),attachments('working_papers','调查底稿与方案',True)]),
            stage('result_review','结论与方案复核','approval',approval_fields('结论与证据一致性、方案约束复核意见'),'analysis'),
            stage('archive_review','定版与归档确认','approval',[field('approved_revision','最终定版编号','text',True),field('archive_location','定版资料归档位置','location',True)],'analysis')]
    if key in CHANGE_KEYS:
        return [stage('application','方案与变更申请','application',app),stage('authorization','预算与执行边界审批','approval',approval_fields('金额、影响范围、风险及执行条件审批意见'),'application'),
            stage('execution','获准方案办理','handling',[field('execution_reference','获准申请、订单或变更编号','text',True),field('applied_changes','实际执行动作及差异','textarea',True),field('executed_on','实际执行日期','date',True),attachments('execution_evidence','执行证据与单据',True)]),
            stage('verification','结果回传与偏差说明','handling',[field('verification_window','结果观察或核对期间','text',True),field('actual_result','实际结果与预算或目标差异','textarea',True),field('corrective_action','纠偏或回滚建议','textarea'),attachments('result_evidence','结果核对依据')]),
            stage('acceptance','效果复核与结案审批','approval',approval_fields('结果、偏差处理及保留或回滚决定'),'verification')]
    if key in EXECUTION_KEYS:
        return [stage('application','执行安排申请','application',app),stage('plan_review','条件与排期审核','approval',approval_fields('执行条件、排期和资源审核意见'),'application'),
            stage('handoff','接手与办理登记','handling',[field('receiving_party','接手岗位或外部联系人','text',True),field('accepted_on','实际接手日期','date',True),field('committed_due','承诺交付日期','date',True),field('execution_progress','办理进展与异常','textarea',True),attachments('handoff_receipt','接手回执与办理单据')]),
            stage('delivery','办理结果与实际交付','handling',[field('delivered_on','实际交付或上线日期','date',True),field('delivery_reference','交付版本、订单或运单编号','text',True),field('deviations','交付差异与处理结果','textarea',True),attachments('delivery_files','实际交付与状态证据',True)]),
            stage('acceptance','交付与执行结果验收','approval',approval_fields('交付数量、版本、状态和异常闭环验收意见'),'delivery')]
    if key in INSPECTION_KEYS:
        return [stage('application','验收范围与依据登记','application',app),stage('inspection','逐项检查与差异登记','handling',[field('checked_on','实际检查日期','date',True),field('check_result','检查项与实际核对结果','textarea',True),field('discrepancies','缺口与差异清单','textarea'),attachments('check_evidence','检查截图、记录或数量依据',True)]),
            stage('rectification','差异整改与补充证据','handling',[field('rectification_result','差异闭环及无需整改的依据','textarea',True),field('verified_revision','复核版本或批次','text',True),attachments('rectification_evidence','整改与复核证据')]),
            stage('acceptance','最终核验与放行','approval',approval_fields('检查完成情况、未闭环限制与放行意见'),'rectification')]
    if key in CASE_KEYS:
        return [stage('application','问题与诉求提交','application',app),stage('triage','责任与处理方案审核','approval',approval_fields('事实、责任和处理方案审核意见'),'application'),
            stage('handling','接单调查与办理','handling',[field('handler_contact','实际接手岗位或外部联系人','text',True),field('received_on','对方实际接单日期','date'),field('investigation','调查过程与补件整改要求','textarea',True),attachments('case_correspondence','沟通回执及办理资料')]),
            stage('feedback','处理反馈与结果登记','handling',[field('feedback_received_on','实际收到反馈日期','date',True),field('resolution','处理结论与未解决事项','textarea',True),attachments('resolution_files','结果回执与证据',True)]),
            stage('closure_review','结果复核与结案','approval',approval_fields('处理结果、损失及客户或平台诉求闭环意见'),'feedback')]
    # Administrative verification, financial reconciliation, service analysis and
    # periodic review each have dedicated application fields and role-specific work.
    if key in {'account','roles','brand','handover','capability','subscription','transparency','privacy'}:
        return [stage('application','资料与核验申请','application',app),stage('material_review','材料与授权范围审核','approval',approval_fields('资料有效性、适用范围及授权依据审核意见'),'application'),
            stage('confirmation','核验与办理结果登记','handling',[field('verified_subject','实际核验主体、账户或版本','text',True),field('confirmation_basis','核验渠道、确认人及结果','textarea',True),field('remaining_conditions','待补充条件与处理安排','textarea'),attachments('confirmation_files','官方回执、确认表与授权依据',True)]),
            stage('acceptance','确认结果审核与归档','approval',approval_fields('核验差异、资料归档及适用限制确认意见'),'confirmation')]
    return [stage('application','口径与复核范围登记','application',app),stage('basis_review','数据与事实依据审核','approval',approval_fields('记录粒度、期间、来源和样本有效性审核意见'),'application'),
        stage('analysis','核算分析与证据整理','handling',[field('calculation_basis','核算方法、来源与限制','textarea',True),field('reconciliation_result','实际结果与差异分析','textarea',True),attachments('analysis_files',title+'工作底稿',True)]),
        stage('feedback','差异闭环与行动反馈','handling',[field('findings','需要跟进的问题与责任归属','textarea',True),field('followup_actions','下一步行动、负责人及截止安排','textarea',True),attachments('feedback_files','补充核对和行动依据')]),
        stage('acceptance','结果复核与归档验收','approval',approval_fields('结果、限制与后续行动验收意见'),'analysis')]


def task_key(task):
    key=task.get('sop_key') or LEGACY_TITLES.get(str(task.get('title','')).replace(' ','')) or task.get('key')
    return ALIASES.get(key,key)


def template_for(task):
    key=task_key(task)
    special={'fcc-certification':fcc_template,'dangerous-goods':dangerous_template,'label-confirmation':label_template}
    if key in special:
        return deepcopy(special[key]())
    if key in DETAILS:
        name,spec=DETAILS[key]
        app=parse_fields(spec)+[attachments('application_files',name+'申请依据',bool(task.get('evidence_required',True)))]
        return dict(key=key,name=name+'审批流程',version=1,fields=deepcopy(app),stages=family_stages(key,app,name))
    title=task.get('title') or '自定义事项'
    module=task.get('module_code') or task.get('module') or 'review'
    defaults={
        'shop':('account','account_scope:店铺账号与站点范围|authorization_scope:操作或授权范围:textarea|official_basis:平台材料与核验依据:textarea'),
        'compliance':('capability','product_scope:适用型号与市场|applicable_rules:需要核对的认证或合规要求:textarea|verification_contact:机构或合规联系人'),
        'listing':('images','content_scope:页面或素材交付范围:textarea|content_revision:需求及素材版本|usage_limits:功能表达与授权限制:textarea'),
        'ads':('ad-launch','campaign_scope:投放或调整对象范围:textarea|advertising_budget:预算金额:number|risk_boundary:利润库存与停止条件:textarea'),
        'inventory':('shipment','batch_reference:采购生产或货件批次|quantity:本次涉及数量:number|delivery_route:供应商仓库及交付路线:textarea'),
        'calendar':('events','event_name:活动或里程碑名称|event_window:活动起止与报名期限|event_conditions:活动资格及执行条件:textarea'),
        'finance':('budget','currency:核算或支付币种|amount:本次涉及金额:number|financial_basis:预算成本或账单依据:textarea'),
        'service':('case','issue_reference:订单问题或反馈编号|customer_impact:客户影响与处理诉求:textarea|resolution_scope:拟处理范围与费用边界:textarea'),
        'review':('weekly-review','report_period:复核统计期间|data_scope:数据来源与口径范围:textarea|decision_question:需要确认的结论或决策:textarea'),
    }
    family,spec=defaults.get(module,defaults['review'])
    app=[field('work_scope',title+'的申请范围','textarea',True)]+parse_fields(spec)+[field('acceptance_requirement','事项验收标准','textarea',True,help=task.get('acceptance_criteria') or '由管理员明确可检查的验收要求。'),attachments('application_files',title+'申请依据',bool(task.get('evidence_required')))]
    return dict(key='custom-'+str(key or module),name=title+'审批流程',version=1,fields=deepcopy(app),stages=family_stages(family,app,title))


def static_registry(catalog):
    """Build the read-only browser registry from the same definitions as the API."""
    data={'version':1,'templates':{},'fallback':{}}
    for task in catalog['tasks']:
        data['templates'][task['key']]=template_for({**task,'sop_key':task['key']})
    for old,new in ALIASES.items():
        data['templates'][old]=deepcopy(data['templates'][new])
    for module in catalog['modules']:
        data['fallback'][module['id']]=template_for({'module_code':module['id'],'title':module['name']+'自定义事项','evidence_required':True})
    return data


if __name__=='__main__':
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Generate the read-only approval template registry.')
    parser.add_argument('--catalog',default='frontend/catalog.json')
    parser.add_argument('--output',default='frontend/approval-templates.json')
    args=parser.parse_args()
    data=static_registry(json.loads(Path(args.catalog).read_text()))
    Path(args.output).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(f"Approval registry: {len(data['templates'])} definitions including aliases")
