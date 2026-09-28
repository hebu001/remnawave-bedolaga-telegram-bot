"""Account for required tests and explicitly reviewed optional tests not run."""

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLICY = Path(__file__).with_name('optional-not-run.json')
FIELDS = ('classname', 'name', 'type', 'message')


def summarize(junit: Path, pytest_exit_code: int, *, allow_optional=False, root=ROOT, policy_path=POLICY):
    summary = {'pytest_exit_code': pytest_exit_code, 'baseline_complete': False, 'mandatory_complete': False}
    try:
        cases = ET.parse(junit).getroot().findall('.//testcase')  # noqa: S314 - locally produced pytest XML
    except (OSError, ET.ParseError) as error:
        summary['invalid_junit'] = str(error)
        return summary
    allowed = {}
    if allow_optional:
        policy = json.loads(policy_path.read_text())
        for record in policy['allowed_skips']:
            source = record['nodeid'].split('::')[0]
            expected = policy['source_sha256'].get(source)
            if expected and (root / source).is_file():
                if hashlib.sha256((root / source).read_bytes()).hexdigest() == expected:
                    allowed[tuple(record[field] for field in FIELDS)] = record
    skipped = []
    for case in cases:
        item = case.find('skipped')
        if item is not None:
            skipped.append(
                dict(
                    classname=case.get('classname', ''),
                    name=case.get('name', ''),
                    type=item.get('type', ''),
                    message=item.get('message', ''),
                )
            )
    optional = [
        allowed[tuple(record[field] for field in FIELDS)]
        for record in skipped
        if tuple(record[field] for field in FIELDS) in allowed
    ]
    unexpected = [record for record in skipped if tuple(record[field] for field in FIELDS) not in allowed]
    summary.update(
        tests=len(cases),
        failures=sum(case.find('failure') is not None for case in cases),
        errors=sum(case.find('error') is not None for case in cases),
        skipped=len(skipped),
        optional_not_run=optional,
        unexpected_skips=unexpected,
    )
    summary['passed'] = summary['tests'] - summary['failures'] - summary['errors'] - summary['skipped']
    successful = pytest_exit_code == 0 and summary['passed'] > 0 and not summary['failures'] and not summary['errors']
    summary['baseline_complete'] = successful and not skipped
    summary['mandatory_complete'] = successful and not unexpected
    summary['status'] = (
        'passed_all'
        if summary['baseline_complete']
        else 'passed_with_optional_not_run'
        if summary['mandatory_complete']
        else 'failed'
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junit', type=Path, required=True)
    parser.add_argument('--pytest-exit-code', type=int, required=True)
    parser.add_argument('--allow-optional', action='store_true')
    args = parser.parse_args()
    result = summarize(args.junit, args.pytest_exit_code, allow_optional=args.allow_optional)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['mandatory_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
