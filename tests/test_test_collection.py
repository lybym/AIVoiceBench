"""Meta-test: every declared test must actually be collected.

This guards a failure mode that cost this repository four review rounds: a test
written at module level (or otherwise outside a `TestCase`) is *declared* but
never executed, so it silently provides no coverage while looking like evidence.

`unittest discover` — which CI runs — only collects `TestCase` methods, so a
module-level `def test_*` is invisible. The check below is deliberately mechanical
rather than heuristic: no `tests/test_*.py` may declare a module-level test
function, and the number of test methods declared inside each test class must
equal the number `unittest` collects for that module.
"""

import ast
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent


def declared_tests(path):
    """(module_level_names, {class_name: method_count}) declared in one module."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    module_level = [node.name for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith('test_')]
    classes = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [item.name for item in node.body
                       if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and item.name.startswith('test_')]
            if methods:
                classes[node.name] = len(methods)
    return module_level, classes


class TestCollectionGuard(unittest.TestCase):
    def modules(self):
        return sorted(path for path in TESTS_ROOT.glob('test_*.py')
                      if path.name != Path(__file__).name)

    def test_no_module_level_test_functions_exist(self):
        offenders = []
        for path in self.modules():
            module_level, _classes = declared_tests(path)
            for name in module_level:
                offenders.append(f'{path.name}::{name}')
        self.assertEqual(offenders, [],
                         'these tests are declared outside a TestCase and are never '
                         'collected by `unittest discover`')

    def test_declared_test_methods_are_all_collected(self):
        mismatches = []
        for path in self.modules():
            _module_level, classes = declared_tests(path)
            declared = sum(classes.values())
            if not declared:
                continue
            module = f'tests.{path.stem}'
            try:
                collected = unittest.TestLoader().loadTestsFromName(module).countTestCases()
            except Exception as error:  # an import error is its own failure
                mismatches.append(f'{module}: could not be loaded ({type(error).__name__})')
                continue
            if collected < declared:
                mismatches.append(f'{module}: declared {declared} test methods but only '
                                  f'{collected} were collected')
        self.assertEqual(mismatches, [],
                         'declared tests are not being collected by the test loader')


if __name__ == '__main__':
    unittest.main()