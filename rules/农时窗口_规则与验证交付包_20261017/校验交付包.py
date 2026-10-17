"""Validate evidence and reference scenarios only. This does not test an Agent."""
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import json, re, hashlib, copy, sys

BASE=Path(__file__).resolve().parent
DATA=BASE/'结构化交付'
LIB=BASE.parent/'农时窗口_官方资料库_20261016'
def load(name): return json.loads((DATA/name).read_text(encoding='utf-8'))
def norm(t): return re.sub(r'\s+','',t)
def minute(t):
    h,m=map(int,t.split(':')); return h*60+m
def require(cond,message):
    if not cond: raise ValueError(message)

def validate_versions(scenario):
    tasks=scenario['tasks']; versions=scenario['versions']; fields={f['id']:f for f in scenario['fields']}
    for v in versions:
        rows=v['assignments']; ids=[a['task_id'] for a in rows]
        require(len(ids)==len(set(ids)),v['version']+' duplicate task')
        dayrows=defaultdict(list)
        for a in rows:
            t=tasks[a['task_id']]; f=fields[t['field']]
            require(f['province']=='湖北' and f['stage']=='seedling','scope mismatch')
            require(t['crew_size']==1 and t['splittable'] is False,'unsupported crew model')
            require(a['duration_minutes']==t['duration_minutes'],'duration differs')
            require(minute(a['end'])-minute(a['start'])==t['duration_minutes'],'time span mismatch')
            require(t['earliest_date']<=a['date']<=t['latest_date'],'outside user dates')
            w=a['conditions_at_completion'] if a['status']=='completed' else v['workability'][t['field']][a['date']]
            require(w['site'] is True and w['weather'] is True,'scheduled unknown or blocked workability')
            require(any(minute(lo)<=minute(a['start']) and minute(a['end'])<=minute(hi) for lo,hi in v['availability'][a['date']]),'outside available interval')
            if t['type']=='maintenance':
                require(f['maintenance_need_confirmed'] is True,'maintenance necessity unconfirmed')
            for dep in t['depends_on']:
                prior=next((p for p in rows if p['task_id']==dep),None)
                require(prior is not None,'missing prerequisite')
                require((prior['date'],prior['end'])<=(a['date'],a['start']),'prerequisite order')
            if a['status']=='completed': require(a['completed_at']==a['date']+'T'+a['end']+':00+08:00','completion timestamp')
            dayrows[a['date']].append(a)
        for date in scenario['dates']:
            require(sum(minute(hi)-minute(lo) for lo,hi in v['availability'][date])==v['capacity_minutes'][date],'capacity and intervals disagree')
            require(sum(a['duration_minutes'] for a in dayrows[date])<=v['capacity_minutes'][date],'capacity overflow')
            ordered=sorted(dayrows[date],key=lambda a:a['start'])
            for left,right in zip(ordered,ordered[1:]): require(left['end']<=right['start'],'worker overlap')
        require(not(set(ids)&{x['task_id'] for x in v['pending']}),'pending task scheduled')
    for prev,cur in zip(versions,versions[1:]):
        require(cur['parent']==prev['version'],'broken version chain')
        for a in prev['assignments']:
            if a['status']=='completed':
                b=next(x for x in cur['assignments'] if x['task_id']==a['task_id'])
                require(a==b,'completed record changed')
    a1=next(x for x in versions[0]['assignments'] if x['task_id']=='A-M')
    a2=next(x for x in versions[1]['assignments'] if x['task_id']=='A-M')
    a3=next(x for x in versions[2]['assignments'] if x['task_id']=='A-M')
    require(a1['date']=='2026-10-20' and a2['date']=='2026-10-21','expected personnel replan')
    require(a2==a3,'unaffected A task changed')
    b=versions[2]['pending'][0]
    require(b['task_id']=='B-M' and b['necessity']=='required','B feedback not reflected')
    require(b['window_by_date']['2026-10-20']=='unavailable','blocked day status')
    require(all(b['window_by_date'][d]=='pending' for d in scenario['dates'][2:]),'future unknown status')
    return True

def main():
    from lxml import html
    from pypdf import PdfReader
    checks=[]
    def checked(label,fn): fn(); checks.append({'item':label,'result':'pass'})
    sources=load('来源索引.json'); src={s['id']:s for s in sources}; text={}
    require(len(sources)==len(src)==11,'source IDs/count')
    for sid,s in src.items():
        f=LIB/s['local_original']; raw=f.read_bytes()
        require(hashlib.sha256(raw).hexdigest()==s['sha256'],sid+' source checksum')
        if f.suffix=='.pdf': text[sid]=[p.extract_text() or '' for p in PdfReader(f).pages]
        else:
            r=html.fromstring(raw.decode('utf-8'))
            text[sid]=r.text_content()
        # read_path is a reading copy, never the evidence extraction dependency.
        reading_path, _, fragment=s['read_path'].partition('#')
        reading=LIB/reading_path
        pages=PdfReader(reading).pages
        require(len(pages)>0,sid+' empty reading PDF')
        require(any((p.extract_text() or '').strip() for p in pages),sid+' unreadable reading PDF')
        if fragment:
            require(re.fullmatch(r'page=[1-9][0-9]*',fragment) is not None,sid+' invalid page fragment')
            require(int(fragment[5:])<=len(pages),sid+' reading page out of range')
        if f.suffix=='.pdf':
            require(hashlib.sha256(reading.read_bytes()).hexdigest()==s['sha256'],sid+' reading copy differs from original')
    checks.append({'item':'11份来源文件哈希与可读取性','result':'pass'})
    checks.append({'item':'11份网页PDF阅读路径、文本可读取性及原PDF副本一致性','result':'pass'})
    cards=load('规则卡片.json')['cards']; ids={c['id'] for c in cards}
    require(len(ids)==len(cards)==12,'card IDs')
    contract=load('输入输出约定.json')
    declared_fields={r[0] for r in contract['fields']}
    require(len(declared_fields)==len(contract['fields']),'duplicate field name')
    for c in cards:
        require(set(c['required_observations'])<=declared_fields,c['id']+' undefined input field')
        require(c['task_type'] in contract['rule_card_mapping'],'unknown card task type')
        for e in c['evidence']:
            sid=e['source_id']; t=text[sid][e['pdf_page_1based']-1] if e['pdf_page_1based'] else text[sid]
            require(norm(e['quote']) in norm(t),c['id']+' quote mismatch')
            require(e['source_sha256']==src[sid]['sha256'],'evidence hash')
            require(e['url']==src[sid]['url'],'evidence URL')
    checks.append({'item':'12张卡片原文片段与位置匹配（忽略空白）','result':'pass'})
    checks.append({'item':'规则卡字段与输入约定逐项对应','result':'pass'})
    design=load('工程约定.json')['rules']; dids={d['id'] for d in design}
    require(len(dids)==12,'design count')
    casefile=load('12组开发案例.json'); cases=casefile['cases']
    require(len(cases)==len({c['id'] for c in cases})==12,'case count')
    for c in cases: require(set(c['rule_ids'])<=ids|dids,'invalid case reference')
    scenario=load('三块地与七天计划.json')
    for t in scenario['tasks'].values(): require(set(t['rule_ids'])<=ids,'invalid task reference')
    checks.append({'item':'规则编号及开发案例交叉引用','result':'pass'})
    checked('V1至V3容量、时段、需求、窗口、期限、前置与完成记录一致性',lambda:validate_versions(scenario))
    # Mutations prove the validator rejects material errors; these are not Agent tests.
    mutations=[]
    bad=copy.deepcopy(scenario);bad['versions'][1]['assignments'][-1]['date']='2026-10-20';mutations.append(('容量冲突',bad))
    bad=copy.deepcopy(scenario);bad['versions'][2]['assignments'][0]['status']='scheduled';mutations.append(('完成记录被改写',bad))
    bad=copy.deepcopy(scenario);bad['versions'][0]['workability']['A']['2026-10-20']['site']=None;mutations.append(('未知现场条件被安排',bad))
    bad=copy.deepcopy(scenario);bad['tasks']['A-M']['depends_on']=['B-I'];mutations.append(('前置任务尚未完成',bad))
    bad=copy.deepcopy(scenario);bad['fields'][0]['maintenance_need_confirmed']=None;mutations.append(('维护需求未知',bad))
    bad=copy.deepcopy(scenario);bad['versions'][0]['assignments'].append(copy.deepcopy(bad['versions'][0]['assignments'][0]));mutations.append(('重复排程',bad))
    for name,bad in mutations:
        try:validate_versions(bad)
        except ValueError:checks.append({'item':'反例检查 '+name,'result':'correctly_rejected'})
        else:raise ValueError('validator missed '+name)
    for d in scenario['weather_snapshot']:require(d['origin']=='synthetic' and not d['operability_inferred_from_numeric_weather'],'weather mislabelled')
    require(casefile['not_holdout'] is True,'test split misleading')
    checks.append({'item':'模拟天气与开发案例属性标记','result':'pass'})
    result={'status':'pass','scope':'资料与人工参考算例内部一致性；不是实际Agent或农业有效性测试','rule_cards':12,'plan_versions':3,'development_case_groups_authored':12,'agent_cases_executed':0,'independent_human_reviews':0,'agronomic_expert_reviews':0,'checks':checks,'checked_at':datetime.now().astimezone().isoformat()}
    (BASE/'校验结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except Exception as exc:
        result={'status':'fail','scope':'资料与人工参考算例内部一致性；不是实际Agent或农业有效性测试',
                'error':f'{type(exc).__name__}: {exc}',
                'checked_at':datetime.now().astimezone().isoformat()}
        (BASE/'校验结果.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        sys.exit(1)
