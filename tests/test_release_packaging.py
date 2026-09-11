"""Release workflow and preview packaging contract tests.

Release-workflow and packaging defects are release blockers (wrong version, no
usable attachment, a pre-release taking the stable "Latest" slot). These checks
are cheap and keep the defect that shipped a duplicate `prerelease` key from
coming back.
"""

import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = re.search(r'VERSION = "([^"]+)"',
                    (ROOT / 'aivoicebench/version.py').read_text(encoding='utf-8')).group(1)

SIGNED_URL_VARS = ('AIVOICEBENCH_AUDIO_PUT_URL', 'AIVOICEBENCH_AUDIO_GET_URL',
                   'AIVOICEBENCH_AUDIO_HOST')


def load_checker():
    spec = importlib.util.spec_from_file_location(
        'check_release_workflow', ROOT / 'scripts/check_release_workflow.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseWorkflowTests(unittest.TestCase):
    def test_workflow_has_no_duplicate_keys(self):
        """A duplicate YAML key silently overrides an earlier one in the same mapping."""
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')

        class Unique(yaml.SafeLoader):
            pass

        def mapping(loader, node, deep=False):
            seen = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in seen:
                    raise ValueError(f'duplicate key {key!r} at line {key_node.start_mark.line + 1}')
                seen[key] = loader.construct_object(value_node, deep=deep)
            return seen

        Unique.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        document = yaml.load(text, Loader=Unique)  # raises on the defect
        self.assertIsInstance(document, dict)

    def test_workflow_checker_passes(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_release_workflow.py')],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_prerelease_is_declared_once_and_not_forced_false(self):
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        keys = [line for line in text.splitlines() if re.match(r'\s*prerelease\s*:', line)]
        self.assertEqual(len(keys), 1, keys)
        self.assertNotIn('prerelease: false', keys[0])
        self.assertIn('make_latest', text,
                      'a pre-release must not take over the stable Latest designation')

    def test_release_body_comes_from_the_versioned_notes(self):
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('body_path', text)
        self.assertTrue((ROOT / 'docs/releases' / f'{VERSION}.md').is_file(),
                        'release notes must exist for the current application version')

    def test_workflow_verifies_tag_version_and_notes_before_publishing(self):
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        for token in ("tag != 'v' + VERSION", 'Missing release notes', 'docker load'):
            self.assertIn(token, text)
        # The tarball must be proven loadable, not merely produced.
        self.assertIn('prove it reloads and runs', text)


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.notes = (ROOT / 'docs/releases' / f'{VERSION}.md').read_text(encoding='utf-8')

    def test_notes_state_the_boundary_honestly(self):
        """Pre-releases must say so; formal releases must still document limitations."""
        if '-' in VERSION:
            self.assertIn('预览版', self.notes)
        self.assertIn('已知限制', self.notes)
        self.assertIn('未尝试', self.notes)

    def test_notes_do_not_claim_unimplemented_features(self):
        """The previous body claimed a full semantic/Finding/analysis stack."""
        for claim in ('LLM 语义评估 (Mock + 火山引擎/OpenAI Provider)',
                      'Finding 生成',
                      '人工修正契约'):
            self.assertNotIn(claim, self.notes)
        self.assertTrue('未实现' in self.notes or '尚未实现' in self.notes)

    def test_notes_document_the_real_cloud_asr_prerequisites(self):
        for name in SIGNED_URL_VARS:
            self.assertIn(name, self.notes)
        self.assertIn('同一个对象', self.notes)

    def test_notes_record_unverified_items_honestly(self):
        self.assertIn('未尝试', self.notes)
        self.assertIn('已知限制', self.notes)


class PreviewPackagingTests(unittest.TestCase):
    def test_preview_compose_is_isolated_and_local_only(self):
        text = (ROOT / 'docs/releases/docker-compose.preview.yml').read_text(encoding='utf-8')
        document = yaml.safe_load(text)
        service = document['services']['aivoicebench-preview']
        self.assertEqual(service['container_name'], 'aivoicebench-preview')
        self.assertIn(f'aivoicebench:v{VERSION}', service['image'])
        self.assertTrue(all(port.startswith('127.0.0.1:') for port in service['ports']),
                        'the preview must bind to loopback only')
        volumes = ' '.join(service['volumes'])
        self.assertIn('aivoicebench-preview-data', volumes)
        for name in SIGNED_URL_VARS:
            self.assertIn(name, text)

    def test_preview_launcher_never_deletes_existing_state(self):
        text = (ROOT / 'docs/releases/start-aivoicebench-preview.ps1').read_text(encoding='utf-8')
        for destructive in ('docker rm -f', 'docker volume rm', 'docker rmi', 'docker system prune'):
            self.assertNotIn(destructive, text,
                             f'launcher must not destroy existing state ({destructive})')
        self.assertIn('aivoicebench-preview', text)
        self.assertIn('127.0.0.1', text)
        self.assertIn('docker load', text)
        self.assertIn('Get-NetTCPConnection', text)
        self.assertIn(f'aivoicebench:v{VERSION}', text)

    def test_launcher_documents_cloud_asr_prerequisites(self):
        text = (ROOT / 'docs/releases/start-aivoicebench-preview.ps1').read_text(encoding='utf-8')
        for name in SIGNED_URL_VARS:
            self.assertIn(name, text)

    def test_launcher_carries_a_utf8_bom(self):
        """Windows PowerShell 5.1 decodes a BOM-less file as ANSI and fails to parse it.

        The first preview launcher shipped as UTF-8 without a BOM; because it contains
        Chinese text, `powershell.exe` mis-decoded it and reported "Missing closing '}'".
        The default Windows shell is 5.1, so a BOM (or ASCII-only content) is required.
        """
        raw = (ROOT / 'docs/releases/start-aivoicebench-preview.ps1').read_bytes()
        self.assertEqual(raw[:3], b'\xef\xbb\xbf',
                         'release scripts with non-ASCII text must be UTF-8 with BOM')
        self.assertIn(b'\r\n', raw, 'Windows script line endings expected')

    def test_launcher_handles_native_command_stderr(self):
        """`docker` writes warnings to stderr; 'Stop' makes PS 5.1 fail the whole script.

        The first preview launcher set $ErrorActionPreference='Stop' and then ran
        `docker info *> $null`; Windows PowerShell 5.1 surfaced the docker warning as a
        terminating NativeCommandError, so the script died before doing anything. Native
        calls must be checked through $LASTEXITCODE instead.
        """
        text = (ROOT / 'docs/releases/start-aivoicebench-preview.ps1').read_text(encoding='utf-8-sig')
        self.assertNotIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn('$LASTEXITCODE', text)
        # A helper with ValueFromRemainingArguments cannot accept docker's `-a` style flags.
        self.assertNotIn('ValueFromRemainingArguments', text)

    def test_launcher_parses_under_windows_powershell(self):
        """Parse with the real Windows PowerShell when it is available."""
        if sys.platform != 'win32' or shutil.which('powershell') is None:
            self.skipTest('Windows PowerShell not available')
        script = str(ROOT / 'docs/releases/start-aivoicebench-preview.ps1')
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             f"$e=$null;$t=$null;"
             f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',"
             f"[ref]$t,[ref]$e);"
             "if ($e.Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 1 } else { 'ok' }"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_asset_is_pinned_to_the_current_version(self):
        """Executable release assets must be pinned to this build."""
        for relative in ('docs/releases/docker-compose.preview.yml',
                         'docs/releases/start-aivoicebench-preview.ps1'):
            text = (ROOT / relative).read_text(encoding='utf-8-sig')
            self.assertIn(VERSION, text, relative)
            # Look for version-like references (v0.3.0 or 0.3.0) but not IP addresses.
            found = set(re.findall(r'(?<!\d\.)(?<!\d)(\d+\.\d+\.\d+)(?![\d.])', text))
            stale = found - {VERSION, '127.0.0.1'.rsplit('.', 1)[0]}
            self.assertEqual(stale, set(),
                             f'{relative} references a superseded build: {sorted(stale)}')
        notes = (ROOT / 'docs/releases' / f'{VERSION}.md').read_text(encoding='utf-8')
        self.assertIn(VERSION, notes)

    def test_version_is_a_release_version(self):
        """Accepts both formal (0.3.0) and alpha (0.2.0-alpha.2) versions."""
        self.assertTrue(re.match(r'\d+\.\d+\.\d+', VERSION), VERSION)


if __name__ == '__main__':
    unittest.main()
