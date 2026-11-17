"""Validated dynamic forms and the initial operating-process content."""
import math
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FormField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,47}$')
    label: str = Field(min_length=1, max_length=100)
    type: Literal['text','textarea','number','date','select','multiselect','person','location','attachment']
    required: bool = False
    help: str = Field(default='', max_length=500)
    options: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def valid_options(self):
        if not self.label.strip():
            raise ValueError('填写项目名称不能为空')
        if self.type in {'select','multiselect'} and (not self.options or any(not x.strip() or len(x)>100 for x in self.options) or len(set(self.options))!=len(self.options)):
            raise ValueError('选择项目需要不重复的有效选项')
        return self


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    fields: list[FormField] = Field(min_length=1, max_length=30)
    owner_id: UUID
    collaborator_ids: list[UUID] = Field(default_factory=list, max_length=30)
    reviewer_ids: list[UUID] = Field(min_length=1, max_length=20)
    review_mode: Literal['all','any','sequential'] = 'all'
    planned_start: date | None = None
    due_date: date | None = None
    location: str = Field(default='', max_length=300)
    dependencies: list[UUID] = Field(default_factory=list, max_length=100)
    recall_pending: bool = False

    @model_validator(mode='after')
    def check(self):
        if len({f.id for f in self.fields}) != len(self.fields):
            raise ValueError('填写项目标识不能重复')
        for ids in (self.collaborator_ids,self.reviewer_ids,self.dependencies):
            if len(set(ids)) != len(ids):
                raise ValueError('人员或前置任务不能重复')
        if self.planned_start and self.due_date and self.planned_start > self.due_date:
            raise ValueError('开始日期不能晚于截止日期')
        return self


class WorkAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    action: Literal['save','submit','reopen']
    answers: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default='', max_length=1000)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1)
    submission_id: UUID
    decision: Literal['approved','rejected']
    comment: str = Field(default='', max_length=2000)


def safe_link(value):
    if not isinstance(value, str) or len(value)>4000 or any(c.isspace() for c in value):
        raise HTTPException(422, '附件链接格式不正确')
    try:
        parts = urlsplit(value)
        if parts.scheme not in {'http','https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, '附件链接必须为不含账户口令的 HTTP(S) 地址')
    return value


def validate_answers(fields, answers, submitting, member_ids, attachment_ids):
    known = {f['id'] for f in fields}
    if set(answers)-known:
        raise HTTPException(422, '填写项目已变化，请刷新后重新填写')
    for field in fields:
        value = answers.get(field['id'])
        missing = value is None or value == '' or value == [] or (isinstance(value,str) and not value.strip())
        if missing:
            if submitting and field.get('required'):
                raise HTTPException(422, f"请填写：{field['label']}")
            continue
        kind = field['type']
        valid = True
        if kind in {'text','textarea','location'}:
            valid = isinstance(value,str) and len(value)<=10000
        elif kind == 'number':
            valid = not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)
        elif kind == 'date':
            try:
                valid = isinstance(value,str) and date.fromisoformat(value).isoformat()==value
            except ValueError:
                valid = False
        elif kind == 'select':
            valid = isinstance(value,str) and value in field['options']
        elif kind == 'multiselect':
            valid = isinstance(value,list) and len(value)<=30 and all(isinstance(x,str) and x in field['options'] for x in value) and len(set(value))==len(value)
        elif kind == 'person':
            valid = isinstance(value,str) and value in member_ids
        elif kind == 'attachment':
            valid = isinstance(value,list) and len(value)<=20
            if valid:
                for item in value:
                    if not isinstance(item,dict) or set(item)-{'kind','id','url','title','version'} or not isinstance(item.get('title'),str) or not item['title'].strip() or len(item['title'])>240:
                        valid=False
                        break
                    if not isinstance(item.get('version','v1'),str) or len(item.get('version','v1'))>80:
                        valid=False
                        break
                    if item.get('kind')=='file':
                        valid = item.get('id') in attachment_ids
                    elif item.get('kind')=='link':
                        safe_link(item.get('url'))
                    else:
                        valid=False
                    if not valid:
                        break
        if not valid:
            raise HTTPException(422, f"填写格式不正确：{field['label']}")


LEGACY_KEYS = {
    '确认正式产品名称、型号和能力边界':'capability', '确认FCC、危险品及标签状态':'certification',
    '确认生产、头程及入仓周期':'supply-lead', '完成SKU成本卡和预算边界':'unit-cost',
    '完成标题、五点和关键词库':'copy', '完成主图、附图、A+和视频':'images',
    '首批货件入仓并达到可售状态':'receiving', '完成首发广告结构、预算与否词库':'ad-launch',
    '完成页面、库存、价格、促销和广告验收':'launch-gate', '完成首轮搜索词、广告位与竞品复盘':'weekly-review',
}
LEGACY_DEPENDENCIES = {
    'certification':['capability'], 'supply-lead':['capability'], 'unit-cost':['supply-lead'],
    'copy':['capability','certification'], 'images':['copy'], 'receiving':['certification','supply-lead'],
    'ad-launch':['images','unit-cost'], 'launch-gate':['receiving','ad-launch'], 'weekly-review':['launch-gate'],
}


def default_fields(task):
    labels = {
        'shop':('账户与授权范围','填写站点、主体、权限及需要跟进的 Case。'),
        'compliance':('核实结论与适用范围','记录产品能力、认证适用范围、有效期和未解决事项。'),
        'listing':('内容方案与验收说明','说明目标用户、语言、页面版本、素材清单及检查结果。'),
        'inventory':('交期与履约安排','记录批次、数量、生产/发货/入仓日期及异常处理。'),
        'finance':('成本口径与测算结论','写明币种、单件成本、预算、测算依据和缺失费用。'),
        'ads':('投放方案与核对结果','说明广告目标、Campaign、预算、竞价、关键词和否定策略。'),
        'calendar':('上线验收与活动安排','逐项核对页面、价格、库存、促销和广告是否达到上线要求。'),
        'service':('问题与处理方案','记录订单范围、客户反馈、处理动作与复核结果。'),
        'review':('复盘结论与后续行动','记录观察周期、事实依据、偏差、负责人及下一次复核日期。'),
    }
    label, help_text = labels.get(task['module_code'],labels['review'])
    return [
        {'id':'summary','label':label,'type':'textarea','required':True,'help':help_text,'options':[]},
        {'id':'result','label':'核对结果','type':'select','required':True,'help':'尚有缺口时，请在说明中列出处理计划。','options':['已核实，可验收','存在缺口，需审核确认']},
        {'id':'deliverables','label':'交付附件','type':'attachment','required':bool(task['evidence_required']),'help':'上传文件或登记飞书、网盘等资料链接，并标注版本。','options':[]},
    ]
