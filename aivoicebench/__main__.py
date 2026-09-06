"""Contract CLI; hardware execution is introduced in later issues."""

import argparse
from pathlib import Path
import sys

from .validation import case_errors, load_document, timeline_errors, metric_errors, finding_errors


def main(argv=None):
    parser = argparse.ArgumentParser(prog='aivoicebench')
    subparsers = parser.add_subparsers(dest='command', required=True)
    validate = subparsers.add_parser('validate', help='Validate TestCase JSON/YAML without hardware or network')
    validate.add_argument('paths', nargs='+', type=Path)
    validate.add_argument('--kind', choices=['test-case', 'timeline', 'metric', 'finding'], default='test-case')
    validate.add_argument('--timeline', type=Path, help='Required context for metric references')
    validate.add_argument('--metrics', nargs='*', type=Path, default=[], help='MetricResult files referenced by a finding')
    validate.add_argument('--regression-case', type=Path, help='Linked TestCase for a frozen regression candidate')
    args = parser.parse_args(argv)
    if args.kind in ('metric', 'finding') and args.timeline is None:
        parser.error('--kind metric/finding requires --timeline')
    failures = 0
    for path in args.paths:
        try:
            if args.kind == 'finding':
                errors = finding_errors(load_document(path), load_document(args.timeline),
                                        [load_document(item) for item in args.metrics],
                                        load_document(args.regression_case) if args.regression_case else None)
            elif args.kind == 'metric':
                errors = metric_errors(load_document(path), load_document(args.timeline))
            else:
                checker = case_errors if args.kind == 'test-case' else timeline_errors
                errors = checker(load_document(path))
        except (OSError, ValueError) as error:
            errors = [str(error)]
        except Exception as error:
            # YAML parse errors use a separate hierarchy; preserve the file/error in CLI output.
            import yaml
            if not isinstance(error, yaml.YAMLError):
                raise
            errors = [str(error)]
        if errors:
            failures += 1
            print(f'INVALID {path}', file=sys.stderr)
            print('\n'.join(errors), file=sys.stderr)
        else:
            print(f'VALID {path} (contract only; assets/hardware not verified)')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
