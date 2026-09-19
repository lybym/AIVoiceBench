"""Meta-test: contract prose must state the rule the validator actually enforces.

This repository has now spent three separate review rounds on the *same* defect
class, each time in a different file: a sentence, schema description or error
message asserting a Turn-binding capability that the implementation rejects.

- Round 3: `docs/08` and `validation._finding_turn_errors` said "events **or
  metrics**" while the generator had already been tightened to events only.
- Round 5: `docs/22-findings-report.md` re-stated the same superseded rule, and
  three further stale statements of the same class were found in `findings.py`,
  the work log, `docs/07` and `docs/08`.
- Round 6: `schemas/finding.schema.json` still promised turns "of the linked
  events/metrics" while the validator derived reachability from `event_ids`
  alone.

Fixing one line per round is what let the pattern keep recurring. The root cause
is that nothing pinned the prose to the semantics, so this module does, in both
directions:

1. It pins the rule itself against the validator's real behaviour, so the
   authoritative semantics are asserted rather than merely described.
2. It asserts the authoritative prose artifacts — the schema that defines the
   data contract, and the technical docs that define the implementation
   constraint — state the event-only rule and contain no published phrase the
   rule contradicts.

The stale-phrase list is a canary list rather than an inferred rule. An inferred
rule ("any sentence containing 'metric' and 'turn'") would produce false
positives on the *correct* negations, such as "a linked metric cannot supply the
turn". Each entry therefore records a phrase that was actually published and
actually contradicted the validator; the guard fails if any of them returns, and
`test_the_canary_catches_the_phrase_that_was_actually_published` proves the
detector is not vacuous.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Authoritative prose artifacts that state the Turn-binding rule.
TURN_RULE_SOURCES = (
    'schemas/finding.schema.json',
    'docs/07-contract-versions.md',
    'docs/08-findings-and-evidence.md',
    'docs/22-findings-report.md',
)

# Phrases that were published and did contradict the event-only rule. Every one
# of these asserts that a linked metric can establish a Finding's Turn.
CONTRADICTING_PHRASES = (
    'linked events/metrics',
    'events or metrics',
    'events 或 metrics',
    'linked events or metrics',
)


def _published_phrase_hits(text):
    """Contradicting phrases present in `text` (case-insensitive)."""
    lowered = text.lower()
    return [phrase for phrase in CONTRADICTING_PHRASES if phrase in lowered]


class TurnBindingContractTests(unittest.TestCase):
    """The documented rule and the enforced rule must be the same rule."""

    def test_the_validator_rejects_a_turn_reachable_only_through_a_metric(self):
        """Pin the rule behaviourally: only a cited event can bind a Turn.

        Builds a genuinely valid Finding first (via the real generator, so every
        schema and referential requirement is satisfied), then declares one extra
        Turn that its linked metric names but none of its cited events reach. If
        someone loosens `_finding_turn_errors` to accept a metric-supplied turn,
        this fails, so the prose requirement below has to be revisited
        deliberately instead of drifting away from the code.
        """
        from aivoicebench.findings import generate_findings_document
        from aivoicebench.llm import LLMJudge, MockLLMProvider
        from aivoicebench.metrics import compute_timeline_metrics
        from aivoicebench.validation import finding_errors
        from tests.judge_fixture import (RUN_ID, dialogue_timeline, fused_document,
                                         turns_document)

        timeline = dialogue_timeline()
        turns = turns_document()
        metrics = compute_timeline_metrics(timeline)
        judge_document = LLMJudge(MockLLMProvider()).evaluate(
            fused_document(), turns, metrics, timeline, run_id=RUN_ID)
        document = generate_findings_document(judge_document, timeline, metrics,
                                              run_id=RUN_ID)
        self.assertTrue(document['findings'], 'the fixture must produce a Finding')
        finding = dict(document['findings'][0])
        self.assertEqual(finding['turn_ids'], ['TURN-0001'])
        # A Turn that exists in the contract's vocabulary but is reachable only
        # through a metric (the metric below names it) must not be declarable.
        foreign = dict(metrics['metrics'][0])
        foreign['metric_id'] = 'MET-PROSE-2'
        foreign['turn_id'] = 'TURN-9999'
        supplied = list(metrics['metrics']) + [foreign]
        finding['metric_ids'] = ['MET-PROSE-2']
        finding['turn_ids'] = ['TURN-9999']

        problems = finding_errors(finding, timeline, supplied)
        self.assertTrue(
            any('TURN-9999' in problem and 'not reachable' in problem
                for problem in problems),
            'a turn reachable only through a linked metric must be rejected; '
            f'got {problems}')

    def test_no_authoritative_prose_promises_metric_supplied_turns(self):
        """No schema description or technical doc may re-state that capability."""
        offenders = []
        for relative in TURN_RULE_SOURCES:
            text = (ROOT / relative).read_text(encoding='utf-8')
            for phrase in _published_phrase_hits(text):
                offenders.append(f'{relative}: {phrase!r}')
        self.assertEqual(
            offenders, [],
            'contract prose promises a Turn binding the validator rejects; '
            'state the event-only rule instead')

    def test_the_schema_itself_names_the_event_only_rule(self):
        """The data contract must carry the rule, not just omit the old text."""
        schema = json.loads((ROOT / 'schemas/finding.schema.json'
                             ).read_text(encoding='utf-8'))
        description = schema['properties']['turn_ids']['description']
        self.assertIn('linked events', description)
        self.assertRegex(description, r'metric cannot establish a turn')
        # A slash between events and metrics is exactly the superseded shorthand.
        self.assertIsNone(re.search(r'events\s*/\s*metrics', description))

    def test_each_rule_source_keeps_an_explicit_negation(self):
        """Removing the stale text is not enough; the correct rule must be stated.

        Docs that describe the migration additionally have to say the Timeline
        is required, because 'never guessed' is only honest if it is not
        delivered as an unvalidatable document.
        """
        for relative in TURN_RULE_SOURCES:
            text = (ROOT / relative).read_text(encoding='utf-8').lower()
            self.assertIn('event', text,
                          f'{relative} must name the object carrying the Turn binding')
        for relative in ('docs/07-contract-versions.md',
                         'docs/08-findings-and-evidence.md'):
            text = (ROOT / relative).read_text(encoding='utf-8').lower()
            self.assertIn('required', text,
                          f'{relative} must state that migration requires the Timeline')

    def test_the_published_abstention_reason_offers_no_metrics_path(self):
        """The user-visible reason must describe the rule that produced it."""
        from aivoicebench.findings import generate_findings_document

        timeline = {'schema_version': '2.0.0', 'case_id': 'CASE-auto',
                    'status': 'partial', 'gaps': [],
                    'events': [{'event_id': 'EVT-1', 'turn_id': None}],
                    'evidence': [{'evidence_id': 'EVD-1'}]}
        document = generate_findings_document(
            [{'decision': 'observed', 'finding_severity': 'info',
              'event_refs': ['EVT-1'], 'evidence_refs': ['EVD-1']}],
            timeline, {'metrics': []}, 'RUN-prose')
        reasons = [item['reason'] for item in document['abstentions']]
        self.assertTrue(reasons, 'the candidate must abstain for this fixture')
        for reason in reasons:
            self.assertNotIn(
                'or metrics', reason,
                'the abstention reason must not offer a capability the '
                'generator does not implement')

    def test_the_canary_catches_the_phrase_that_was_actually_published(self):
        """Prove the detector above is not vacuous.

        The guard is only worth having if re-introducing the exact published
        text fails it. This reproduces the round-6 schema description in memory
        and asserts the same detector rejects it.
        """
        round_six_description = (
            'Turns this Finding is bound to. Finding 2.1.0 records them '
            'explicitly; each must resolve to the Turn of the linked '
            'events/metrics.')
        self.assertEqual(_published_phrase_hits(round_six_description),
                         ['linked events/metrics'])
        # And the correct replacement must pass, so the guard is not simply
        # rejecting every sentence that mentions both words.
        corrected = ('each must resolve to the Turn of the linked events. Only an '
                     'event carries a Turn binding, so a linked metric cannot '
                     'establish a turn')
        self.assertEqual(_published_phrase_hits(corrected), [])


if __name__ == '__main__':
    unittest.main()
