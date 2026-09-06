"""Contract CLI; hardware execution is introduced in later issues."""

import argparse
from pathlib import Path
import sys

from .validation import case_errors, load_document, timeline_errors, metric_errors, finding_errors


def main(argv=None):
    parser = argparse.ArgumentParser(prog='aivoicebench')
    subparsers = parser.add_subparsers(dest='command', required=True)
    run = subparsers.add_parser('run', help='Prepare one Case or suite; no hardware adapter yet')
    run.add_argument('path', type=Path)
    run.add_argument('--output', type=Path, default=Path('artifacts/runs'))
    run.add_argument('--asset-root', type=Path)
    run.add_argument('--device-profile', type=Path)
    run.add_argument('--dry-run', action='store_true', help='Only prepare assets/contracts, never capture hardware')
    validate = subparsers.add_parser('validate', help='Validate TestCase JSON/YAML without hardware or network')
    validate.add_argument('paths', nargs='+', type=Path)
    validate.add_argument('--kind', choices=['test-case', 'timeline', 'metric', 'finding'], default='test-case')
    validate.add_argument('--timeline', type=Path, help='Required context for metric references')
    validate.add_argument('--metrics', nargs='*', type=Path, default=[], help='MetricResult files referenced by a finding')
    validate.add_argument('--regression-case', type=Path, help='Linked TestCase for a frozen regression candidate')
    args = parser.parse_args(argv)
    if args.command == 'run':
        from .runner import run_input
        import yaml
        try:
            results = run_input(args.path, args.output, args.asset_root, args.dry_run,
                                load_document(args.device_profile) if args.device_profile else None)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f'RUN ERROR: {error}', file=sys.stderr)
            return 1
        for directory, manifest in results:
            print(f'{manifest["status"].upper()} {directory} (dry_run; no hardware measurement)')
            for blocker in manifest['blockers']:
                print(f'  {blocker}')
        return 2 if any(manifest['status'] == 'blocked' for _, manifest in results) else 0
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
