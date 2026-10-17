"""Run both delivery validators relative to this file, regardless of working directory."""
from pathlib import Path
from datetime import datetime
import json
import os
import subprocess
import sys

BASE = Path(__file__).resolve().parent
PACKAGES = ('农时窗口_规则与验证交付包_20261017', '农时窗口_规则卡片_融合修订版_20261017')


def main():
    results = []
    for name in PACKAGES:
        run = None
        try:
            run = subprocess.run([sys.executable, str(BASE / name / '校验交付包.py')],
                                 cwd=BASE, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            # Parse this run's output, not an earlier report left behind after a crash.
            result = json.loads(run.stdout)
            if run.returncode != 0 or result.get('status') != 'pass':
                result['status'] = 'fail'
            result['returncode'] = run.returncode
        except Exception as exc:
            result = {'status': 'fail', 'error': f'{type(exc).__name__}: {exc}'}
            if run is not None and run.stderr:
                result['stderr'] = run.stderr[-3000:]
        results.append({'package': name, **result})
        print(name + '：' + ('通过' if result['status'] == 'pass' else '失败'))
    summary = {'status': 'pass' if all(r['status'] == 'pass' for r in results) else 'fail',
               'checked_at': datetime.now().astimezone().isoformat(),
               'scope': '当前两套资料交付包及其依赖来源；不含tmp历史生成脚本、实际Agent或农业适用性测试',
               'packages': results}
    (BASE / '资料校验结果.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('详细结果：' + str(BASE / '资料校验结果.json'))
    return 0 if summary['status'] == 'pass' else 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
