"""File readiness is separate from task completion; links are never dereferenced."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def delivery_catalog():
    path=Path(os.getenv('CATALOG_FILE',str(Path(__file__).resolve().parents[2]/'frontend'/'catalog.json')))
    return json.loads(path.read_text())


def item_meta(task,catalog):
    template=next((t for t in catalog['tasks'] if t['key']==task.get('sop_key')),None)
    if template and template.get('delivery'):
        return template['delivery']
    return {'category':{'listing':'listing','ads':'advertising','inventory':'delivery'}.get(task.get('module_code'),'store'),
            'subcategory':task.get('stage_name','自定义工作'),'kind':'file','expected_files':[]}


def preparation_state(task,documents,meta):
    if task['status']=='not_applicable':return 'not_applicable'
    if task['status']=='blocked':return 'blocked'
    if any(d['preparation_status']=='needs_update' for d in documents):return 'needs_update'
    if meta['kind']=='milestone' and task['status']=='completed':return 'stage_done'
    if meta['kind']=='file' and not documents:return 'missing'
    if documents and all(d['preparation_status']=='prepared' for d in documents):
        return 'ready' if task['status']=='completed' else 'file_ready'
    return 'pending_review' if documents else 'stage_pending'


def build_delivery_rows(tasks,documents,evidence,catalog,filters):
    rows=[]
    for task in tasks:
        meta=item_meta(task,catalog)
        all_versions=[d for d in documents if d['task_id']==task['id']]
        references=[dict(d) for d in all_versions if d.get('is_current',True)]
        known_urls={d.get('url') for d in all_versions}
        for link in evidence:
            if link['task_id']==task['id'] and link.get('uri') not in known_urls:
                known_urls.add(link.get('uri'))
                references.append({'title':link.get('note') or '已有任务证据链接','url':link['uri'],'version':'未登记','preparation_status':'unconfirmed'})
        state=preparation_state(task,references,meta)
        if filters.get('delivery_category','all') not in {'all',meta['category']}:continue
        if filters.get('delivery_kind','all') not in {'all',meta['kind']}:continue
        ready=filters.get('delivery_readiness','all')
        if ready=='ready' and state not in {'ready','stage_done'}:continue
        if ready=='missing' and state not in {'missing','needs_update'}:continue
        if ready=='pending' and state in {'ready','stage_done','not_applicable'}:continue
        if ready not in {'all','ready','missing','pending',state}:continue
        search=filters.get('search','').lower()
        haystack=' '.join([task['title'],meta['subcategory'],*meta['expected_files'],*[d['title'] for d in references],task.get('owner') or '']).lower()
        if search and search not in haystack:continue
        # Keep signed URLs or embedded query credentials out of external model prompts.
        refs=[{k:d.get(k) for k in ('title','version','preparation_status')}|{'link_host':urlsplit(d.get('url') or '').hostname} for d in references]
        rows.append({'task_id':str(task['id']),'title':task['title'],'category':meta['category'],'subcategory':meta['subcategory'],
                     'expected_files':meta['expected_files'],'kind':meta['kind'],'preparation_state':state,
                     'task_status':task['status'],'owner':task.get('owner'),'phase':task.get('phase'),'documents':refs})
    return rows
