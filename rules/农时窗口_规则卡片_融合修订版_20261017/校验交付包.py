"""Check local evidence and document consistency, not Agent performance or agronomy."""
from pathlib import Path
from datetime import datetime
from collections import Counter
import copy
import hashlib
import json
import re
import sys
import unicodedata
from zipfile import ZipFile
from xml.etree import ElementTree as ET

BASE = Path(__file__).resolve().parent
DATA = BASE / '结构化资料'
WORD = BASE / '农时窗口_规则卡片表_融合修订版_20261017.docx'
SCOPE = '本地来源、引用及文档一致性；不是实际Agent、农业专业或新增排版验证'


def load(name):
    return json.loads((DATA / name).read_text(encoding='utf-8'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def norm(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).translate(
        str.maketrans({'⻓': '长', '⻔': '门', '⻅': '见', '⻛': '风'}))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_evidence(evidence, sources, pages, label):
    sid = evidence['source_id']
    require(sid in sources, label + ' 未知来源 ' + sid)
    require(evidence['source_sha256'] == sources[sid]['sha256'], label + ' 引用指纹不一致')
    locations = evidence['pdf_pages']
    require(isinstance(locations, list) and len(locations) > 0, label + ' 缺少页码')
    require(all(type(p) is int and 1 <= p <= len(pages[sid]) for p in locations), label + ' 页码越界')
    require(bool(norm(evidence['quote'])), label + ' 引文为空')
    require(any(norm(evidence['quote']) in norm(pages[sid][p-1]) for p in locations), label + ' 指定页引文不匹配')


def validate_new_review(review, sources, pages, ids):
    expected_sources = {'Z02', 'Z03', 'Z11', 'Z12'}
    require(set(review['new_sources']) == expected_sources and len(review['new_sources']) == 4, '新增来源清单不完整')
    require(review['not_for_runtime'] is True and review['base_cards_unchanged'] is True,
            '补充审查不得冒充已启用规则或覆盖核心版本')
    require(review['new_card_count'] == 0 and review['agent_cases_executed'] == 0 and review['agronomic_reviews'] == 0,
            '补充资料的新增卡片或验证状态与本版不符')
    reviews = review['reviews']
    require(len(reviews) == 4 and {r['source_id'] for r in reviews} == expected_sources, '新增来源审查重复或遗漏')
    quotes = 0
    for entry in reviews:
        sid = entry['source_id']
        require(entry['runtime_enabled'] is False, sid + ' 补充参考被自动启用')
        require(bool(entry['related_cards']) and set(entry['related_cards']) <= ids, sid + ' 关联卡片不存在')
        for name in ['decision', 'what_is_new', 'existing_coverage', 'applicability', 'prototype_note']:
            require(isinstance(entry[name], str) and bool(entry[name].strip()), sid + ' 审查缺项：' + name)
        require(bool(entry['limits']) and all(isinstance(t, str) and t.strip() for t in entry['limits']), sid + ' 缺少采用边界')
        require(bool(entry['evidence']), sid + ' 缺少审查证据')
        for evidence in entry['evidence']:
            require(evidence['source_id'] == sid, sid + ' 审查证据来源错配')
            validate_evidence(evidence, sources, pages, sid + ' 新增审查')
            quotes += 1
    require(quotes == 9, '新增审查引用数量变化')
    cases = review['boundary_cases']
    require(len(cases) == 8 and {c['id'] for c in cases} == {f'A{i:02}' for i in range(1, 9)}, '新增案例编号错误')
    for case in cases:
        require(case['source_id'] in expected_sources, case['id'] + ' 案例来源不存在')
        require(bool(case['related_cards']) and set(case['related_cards']) <= ids, case['id'] + ' 案例卡片引用错误')
        require(bool(case['input']) and bool(case['expected']), case['id'] + ' 案例缺项')
    return quotes


def main():
    from pypdf import PdfReader
    checks = []
    sources_list = load('来源索引.json')
    sources = {s['id']: s for s in sources_list}
    expected_sources = {'Z01', 'Z02', 'Z03', 'Z04', 'Z05', 'Z06', 'Z07', 'Z09',
                        'Z11', 'Z12', 'Z13', 'Z14', 'Z15', 'Z16', 'Z17', 'Z18'}
    require(len(sources_list) == len(sources) == 16 and set(sources) == expected_sources, '来源编号重复、遗漏或数量变化')
    pages = {}
    for sid, source in sources.items():
        # original_snapshot records provenance only; runtime depends on file inside this package.
        path = (BASE / source['file']).resolve()
        require(path.is_relative_to(BASE), sid + ' 来源路径超出本包')
        require(sha(path) == source['sha256'], sid + ' 来源文件指纹改变')
        pages[sid] = [p.extract_text() or '' for p in PdfReader(path).pages]
        require(any(t.strip() for t in pages[sid]), sid + ' PDF没有可读取文本')
    checks.append('16份包内PDF存在、可读且与已记录SHA-256一致')

    cards = load('规则卡片.json')['cards']
    ids = {c['id'] for c in cards}
    require(len(cards) == 31 and ids == {f'C{i:02}' for i in range(1, 32)}, '卡片编号或数量错误')
    contract = load('接入约定.json')
    fields = set(contract['field_names'])
    require(len(fields) == len(contract['field_names']), '接入字段重复')
    categories = dict(Counter(c['category'] for c in cards))
    require(categories == {'首版候选': 7, '监测研究参考': 6, '参数与背景参考': 5, '扩展处置参考': 13}, '卡片分类数量变化，需同步复核')
    require({c['id'] for c in cards if c['category'] == '首版候选'} ==
            {'C11', 'C18', 'C19', 'C20', 'C23', 'C24', 'C31'}, '首版候选范围改变')
    word_fields = ('title', 'applicability', 'condition', 'source_supported_knowledge',
                   'prototype_behavior', 'missing_behavior', 'limits')
    with ZipFile(WORD) as archive:
        xml = ET.fromstring(archive.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    word_text = norm('\n'.join(''.join(p.itertext()) for p in xml.findall('.//w:p', ns)))
    quote_count = 0
    for card in cards:
        label = card['id']
        require(card['runtime_enabled'] is False, label + ' 自动执行开关不符合本版默认约定')
        require('original_fields' not in card, label + ' 混入已被修订的旧条件字段')
        require(bool(card['review_status']), label + ' 缺少复核状态')
        require(bool(card['input_fields']) and set(card['input_fields']) <= fields, label + ' 输入字段未声明')
        for name in word_fields:
            require(isinstance(card[name], str) and bool(card[name].strip()), label + ' 缺少 ' + name)
            require(norm(card[name]) in word_text, label + ' Word与JSON不一致：' + name)
        require(bool(card['evidence']), label + ' 缺少证据')
        for evidence in card['evidence']:
            validate_evidence(evidence, sources, pages, label)
            quote_count += 1
    supplement = load('补充时机与边界依据.json')['evidence']
    require(quote_count == 35 and len(supplement) == 3, '引用数量变化')
    for i, evidence in enumerate(supplement, 1):
        validate_evidence(evidence, sources, pages, f'补充依据{i}')
    checks.extend(['31张卡编号、分类、输入字段及默认关闭状态正确',
                   '35段卡片引文及3段补充依据均与指定PDF页匹配',
                   '31张卡的标题、范围、条件、知识、行为、缺项和边界均在Word中匹配'])

    mapping = load('新旧规则映射.json')['mapping']
    require(set(mapping) == {f'R{i:02}' for i in range(1, 13)}, '新旧映射缺失或多余R编号')
    cases = load('边界案例.json')['cases']
    require(len(cases) == 12 and {c['id'] for c in cases} == {f'V{i:02}' for i in range(1, 13)}, '边界案例编号错误')
    for case in cases:
        require(bool(case['input']) and bool(case['expected']), case['id'] + ' 案例缺项')
    references = re.findall(r'\bC\d{2}\b', json.dumps([mapping, cases], ensure_ascii=False))
    require(set(references) <= ids, '映射或案例引用不存在的C编号')
    checks.append('12组边界案例及R01至R12映射的编号和C卡引用有效（未运行Agent）')

    review = load('新增资料审查.json')
    new_quotes = validate_new_review(review, sources, pages, ids)
    review_text = norm((BASE / '新增资料审查与补充说明.md').read_text(encoding='utf-8'))
    for entry in review['reviews']:
        items = [entry[k] for k in ['decision', 'what_is_new', 'existing_coverage', 'applicability', 'prototype_note']]
        items += entry['limits'] + [e['quote'] for e in entry['evidence']]
        require(all(norm(item) in review_text for item in items), entry['source_id'] + ' 审查说明与JSON不一致')
    checks.append('4份新增资料审查、9段指定页引文、关联卡片及说明文字一致')
    checks.append('8组新增边界预期案例编号与引用有效；补充资料未自动启用（未运行Agent）')

    # Preserve the earlier visual review as a baseline, never silently refresh its fingerprints.
    baseline = load('内容校验记录.json')
    require(sha(WORD) == baseline['docx_sha256'], 'Word已改变，原29页目视检查记录不再适用，需重新复核')
    require(sha(DATA / '规则卡片.json') == baseline['json_sha256'], '规则JSON已改变，需复核并更新版本基线')
    checks.append('Word及规则JSON与原内容/排版复核基线一致；本次未重新目视检查')

    mutations = []
    bad = copy.deepcopy(cards[0]['evidence'][0]); bad['quote'] = '故意制造的错误引用_不应通过'; mutations.append(('错引原文', bad))
    bad = copy.deepcopy(cards[0]['evidence'][0]); bad['pdf_pages'] = [0]; mutations.append(('非法页码', bad))
    bad = copy.deepcopy(cards[0]['evidence'][0]); bad['source_sha256'] = '0' * 64; mutations.append(('引用指纹错误', bad))
    bad = copy.deepcopy(cards[0]['evidence'][0]); bad['source_id'] = 'Z99'; mutations.append(('未知来源', bad))
    for name, bad in mutations:
        try:
            validate_evidence(bad, sources, pages, name)
        except ValueError:
            checks.append('反例已拦截：' + name)
        else:
            raise ValueError('校验器漏报：' + name)
    review_mutations = []
    bad = copy.deepcopy(review); bad['reviews'][0]['runtime_enabled'] = True
    review_mutations.append(('补充资料被自动启用', bad))
    bad = copy.deepcopy(review); bad['reviews'][0]['related_cards'] = ['C99']
    review_mutations.append(('补充资料引用不存在的卡片', bad))
    for name, bad in review_mutations:
        try:
            validate_new_review(bad, sources, pages, ids)
        except ValueError:
            checks.append('反例已拦截：' + name)
        else:
            raise ValueError('校验器漏报：' + name)
    return {'status': 'pass', 'scope': SCOPE, 'cards': len(cards), 'sources': len(sources),
            'quoted_fragments': quote_count, 'supplementary_fragments': len(supplement),
            'new_review_fragments': new_quotes, 'new_expected_case_groups_authored': len(review['boundary_cases']),
            'categories': categories, 'agent_cases_executed': 0, 'agronomic_reviews': 0,
            'checks': checks}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = main()
    except Exception as exc:
        result = {'status': 'fail', 'scope': SCOPE, 'error': f'{type(exc).__name__}: {exc}'}
    result['checked_at'] = datetime.now().astimezone().isoformat()
    (BASE / '校验结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['status'] == 'pass' else 1)
