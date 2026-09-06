"""Contract CLI; hardware execution is introduced in later issues."""

import argparse
from pathlib import Path
import sys

from .validation import case_errors, load_document


def main(argv=None):
    parser = argparse.ArgumentParser(prog='aivoicebench')
    subparsers = parser.add_subparsers(dest='command', required=True)
    validate = subparsers.add_parser('validate', help='Validate TestCase JSON/YAML without hardware or network')
    validate.add_argument('paths', nargs='+', type=Path)
    args = parser.parse_args(argv)
    failures = 0
    for path in args.paths:
        try:
            errors = case_errors(load_document(path))
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
