"""Human revision contract — preserves original machine results alongside
human corrections without overwriting.

Every revision is an immutable annotation layered on top of the original
machine output. The original is never modified. Revisions can correct:
- ASR text
- Speaker role attribution
- Event boundaries (start/end ms)
- Turn association
- Finding confirmation/rejection

This enables the Machine → Human Review → Confirmed Evidence → Regression
lifecycle required by the user's directive section #10.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .runner import write_json


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class RevisionStore:
    """Append-only revision store. Originals are never modified."""

    def __init__(self, output_dir):
        self.dir = Path(output_dir).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.revisions_path = self.dir / 'revisions.json'
        if self.revisions_path.exists():
            self.revisions = json.loads(self.revisions_path.read_text(encoding='utf-8'))
        else:
            self.revisions = {'schema_version': '1.0.0', 'revisions': []}

    def _save(self):
        write_json(self.revisions_path, self.revisions)

    def add_revision(self, target_type, target_id, field, original_value,
                     revised_value, reviewer, reason, confidence=1.0):
        """Add a human revision to a machine output.

        Args:
            target_type: 'segment' | 'event' | 'turn' | 'finding' | 'transcript'
            target_id: ID of the target object
            field: field name being corrected (e.g., 'speaker_role', 'start_ms', 'text')
            original_value: the original machine value (preserved)
            revised_value: the corrected value
            reviewer: reviewer name/ID
            reason: why the correction was made
            confidence: reviewer confidence (0-1)
        """
        revision = {
            'revision_id': 'REV-' + uuid.uuid4().hex[:12],
            'target_type': target_type,
            'target_id': target_id,
            'field': field,
            'original_value': original_value,
            'revised_value': revised_value,
            'reviewer': reviewer,
            'reason': reason,
            'confidence': confidence,
            'created_at': _utc_now(),
        }
        self.revisions['revisions'].append(revision)
        self._save()
        return revision

    def get_revisions_for(self, target_type, target_id):
        """Get all revisions for a specific target object."""
        return [r for r in self.revisions['revisions']
                if r['target_type'] == target_type and r['target_id'] == target_id]

    def apply_revisions(self, document, target_type, id_field):
        """Apply revisions to a copy of a document, preserving originals.

        Returns a new dict with '_revisions' field listing applied changes.
        The original document is NOT modified.
        """
        import copy
        result = copy.deepcopy(document)
        applied = []

        if isinstance(document, list):
            items = document
        elif isinstance(document, dict) and 'segments' in document:
            items = document['segments']
        elif isinstance(document, dict) and 'events' in document:
            items = document['events']
        elif isinstance(document, dict) and 'turns' in document:
            items = document['turns']
        elif isinstance(document, dict) and 'findings' in document:
            items = document['findings']
        else:
            items = [document]

        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get(id_field)
            if item_id is None:
                continue
            revs = self.get_revisions_for(target_type, item_id)
            if revs:
                item_rev = copy.deepcopy(item)
                item_rev['_revisions'] = []
                for rev in revs:
                    field = rev['field']
                    if field in item_rev:
                        item_rev['_original_' + field] = item_rev[field]
                    item_rev[field] = rev['revised_value']
                    item_rev['_revisions'].append({
                        'revision_id': rev['revision_id'],
                        'field': field,
                        'original_value': rev['original_value'],
                        'revised_value': rev['revised_value'],
                        'reviewer': rev['reviewer'],
                        'reason': rev['reason'],
                        'confidence': rev['confidence'],
                    })
                    applied.append(rev['revision_id'])
                if isinstance(document, list):
                    idx = items.index(item)
                    result[idx] = item_rev
                elif isinstance(document, dict) and 'segments' in document:
                    idx = document['segments'].index(item)
                    result['segments'][idx] = item_rev
                elif isinstance(document, dict) and 'events' in document:
                    idx = document['events'].index(item)
                    result['events'][idx] = item_rev
                elif isinstance(document, dict) and 'turns' in document:
                    idx = document['turns'].index(item)
                    result['turns'][idx] = item_rev
                elif isinstance(document, dict) and 'findings' in document:
                    idx = document['findings'].index(item)
                    result['findings'][idx] = item_rev
                else:
                    result = item_rev

        # Lists preserve their public shape; each changed item already contains
        # its revision references. Document envelopes can carry an aggregate.
        if isinstance(result, dict):
            result['_applied_revisions'] = applied
        return result

    def confirm_finding(self, finding_id, reviewer, confirmed=True,
                        notes='', severity=None):
        """Confirm or reject a finding candidate."""
        return self.add_revision(
            'finding', finding_id,
            'status',
            'needs_verification',
            'confirmed' if confirmed else 'wont_fix',
            reviewer,
            f'Finding {"confirmed" if confirmed else "rejected"} by {reviewer}: {notes}',
            confidence=0.9,
        )
