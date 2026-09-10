"""Validate the release workflow before publishing anything.

Checks the classes of defect that would break or misrepresent a release:
duplicate mapping keys anywhere in the YAML, a repeated/overriding `prerelease`
input, a missing draft input, and a Release body that is not read from the
versioned notes file.
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys, the defect this workflow previously had."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        result.setdefault(key, None)
        if key in result and result[key] is not None:
            raise ValueError(f'duplicate key: {key!r} at line {key_node.start_mark.line + 1}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


path = ROOT / '.github/workflows/release.yml'
text = path.read_text(encoding='utf-8')

try:
    doc = yaml.load(text, Loader=UniqueKeyLoader)
except ValueError as error:
    failures.append(f'release.yml is not unambiguous YAML: {error}')
    doc = None

if doc is not None:
    inputs = doc[True]['workflow_dispatch']['inputs'] if True in doc else None
    check(inputs is not None, 'release.yml must be dispatched manually with explicit inputs')
    for required in ('tag', 'ref', 'draft'):
        check(required in inputs, f'missing workflow input: {required}')
    steps = doc['jobs']['build-and-release']['steps']
    release_steps = [s for s in steps if 'action-gh-release' in str(s.get('uses', ''))]
    check(len(release_steps) == 1, 'exactly one Release step is expected')
    if release_steps:
        with_block = release_steps[0]['with']
        check('prerelease' in with_block, 'Release step must set prerelease explicitly')
        check('body_path' in with_block, 'Release body must come from the versioned notes file')
        check('draft' in with_block, 'Release step must honour the draft input')
        check(with_block['prerelease'] != 'false', 'prerelease must not be hard-coded false')
        check('make_latest' in with_block,
              'a pre-release must not take the Latest designation from a stable release')

    # The duplicate-key defect was a second `prerelease:` key later in the same mapping.
    # Count only real mapping keys, not prose in comments or expressions.
    occurrences = len([line for line in text.splitlines()
                       if re.match(r'\s*prerelease\s*:', line)])
    check(occurrences == 1, f'prerelease must appear exactly once as a key, found {occurrences}')

if failures:
    print('release.yml INVALID')
    for failure in failures:
        print(' -', failure)
    sys.exit(1)
print('release.yml OK: unambiguous YAML, single prerelease key, versioned body, draft support')
