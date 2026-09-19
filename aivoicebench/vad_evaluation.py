"""Real-recording acoustic-boundary evaluation scaffold (Issue #23, AC3).

Issue #23's third acceptance criterion asks for a *manually annotated real
recording* sample set reporting speech start/end error, miss/false-alarm
behaviour and the evaluated denominator/coverage. This module is the reusable
scaffolding for that evaluation. It is deliberately separate from the boundary
producers: it consumes one annotation file plus one or more produced
``AcousticSegments`` documents and reports numbers with their denominator.

**It does not manufacture the acceptance evidence.** When no annotated sample set
is supplied, :func:`evaluate` returns ``status='no_annotated_sample_set'`` with
``has_annotated_sample_set=False`` and every metric ``None`` — the evaluation is
reported as not performed rather than as a zero-error result. Only a human
annotation of a real recording can satisfy AC3, and nothing here may be presented
as real-recording evidence if the sample set is synthetic.

Annotation format: see ``schemas/vad-annotation.schema.json``. The annotation is
bound to the recording by sha256, so a prediction produced from different audio
cannot be scored against it.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .validation import schema_errors

#: Evaluation policy version. Bump when matching or metric definitions change.
EVALUATION_POLICY_VERSION = '1.0.0'

#: Reasons an evaluation reports instead of numbers.
NO_ANNOTATED_SAMPLE_SET = 'no_annotated_sample_set'
EMPTY_ANNOTATION_SET = 'annotated_sample_set_has_no_intervals'


class AnnotationError(ValueError):
    """The annotation document is unusable (shape, bounds or recording identity)."""


@dataclass
class Interval:
    start_ms: float
    end_ms: float
    label: str = 'speech'

    @property
    def duration_ms(self):
        return self.end_ms - self.start_ms


@dataclass
class Annotations:
    """One annotated recording.

    ``annotated_regions`` is the set of time ranges a human actually listened to
    and labelled. Regions outside it are *not* silence evidence: a false alarm
    there is unmeasurable, and the report says so instead of counting it as
    correct.
    """

    source: dict
    intervals: list
    annotator: str
    annotation_id: str
    annotated_regions: list = field(default_factory=list)
    notes: str | None = None
    #: How precisely the annotator could place a boundary. Kept separate from any
    #: provider's uncertainty so a provider's window resolution is never read as
    #: human annotation precision.
    boundary_uncertainty_ms: float | None = None

    @property
    def duration_ms(self):
        return self.source['duration_ms']


def load_annotation(document):
    """Validate an in-memory annotation document and build :class:`Annotations`."""
    errors = schema_errors(document, 'vad-annotation')
    if errors:
        raise AnnotationError('Invalid VAD annotation document: ' + '; '.join(errors))
    source = dict(document['source'])
    intervals = []
    previous_end = 0.0
    for index, item in enumerate(document['intervals']):
        start = float(item['start_ms'])
        end = float(item['end_ms'])
        if end <= start:
            raise AnnotationError(f'/intervals/{index}: end_ms must exceed start_ms')
        if start < previous_end - 0.001:
            raise AnnotationError(f'/intervals/{index}: intervals must be ordered and non-overlapping')
        if end > source['duration_ms'] + 0.001:
            raise AnnotationError(f'/intervals/{index}: end_ms exceeds the recording duration')
        previous_end = end
        intervals.append(Interval(start_ms=start, end_ms=end, label=item.get('label', 'speech')))
    regions = []
    for index, item in enumerate(document.get('annotated_regions') or []):
        start = float(item['start_ms'])
        end = float(item['end_ms'])
        if end <= start:
            raise AnnotationError(f'/annotated_regions/{index}: end_ms must exceed start_ms')
        if end > source['duration_ms'] + 0.001:
            raise AnnotationError(f'/annotated_regions/{index}: end_ms exceeds the recording duration')
        regions.append(Interval(start_ms=start, end_ms=end, label='annotated'))
    return Annotations(
        source=source,
        intervals=intervals,
        annotator=document['annotator'],
        annotation_id=document['annotation_id'],
        annotated_regions=regions,
        notes=document.get('notes'),
        boundary_uncertainty_ms=document.get('boundary_uncertainty_ms'))


def load_annotation_file(path):
    import json
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise AnnotationError(f'Cannot read VAD annotation {path}: {error}') from None
    return load_annotation(document)


def _overlap(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _match(truth, predicted):
    """Deterministically match predicted segments to annotated speech intervals.

    One-to-one greedy matching. A candidate must overlap the annotated interval;
    the best candidate maximises overlap, then IoU, then the smallest absolute
    onset difference, then the lowest segment index. The order is total, so two
    runs over the same inputs always produce the same matching.
    """
    used = set()
    matches = []
    for truth_index, interval in enumerate(truth):
        best = None
        best_key = None
        for index, segment in enumerate(predicted):
            if index in used:
                continue
            overlap = _overlap(interval.start_ms, interval.end_ms,
                               segment['start_ms'], segment['end_ms'])
            if overlap <= 0:
                continue
            union = (max(interval.end_ms, segment['end_ms'])
                     - min(interval.start_ms, segment['start_ms']))
            iou = overlap / union if union > 0 else 0.0
            key = (-overlap, -iou, abs(segment['start_ms'] - interval.start_ms), index)
            if best_key is None or key < best_key:
                best_key = key
                best = index
        if best is not None:
            used.add(best)
            matches.append({
                'annotation_index': truth_index,
                'segment_index': best,
                'segment_id': predicted[best]['segment_id'],
                'overlap_ms': round(_overlap(interval.start_ms, interval.end_ms,
                                             predicted[best]['start_ms'], predicted[best]['end_ms']), 3),
                'start_error_ms': round(predicted[best]['start_ms'] - interval.start_ms, 3),
                'end_error_ms': round(predicted[best]['end_ms'] - interval.end_ms, 3),
            })
    return matches, used


def _in_annotated_region(start, end, regions):
    """Whether an interval lies inside the human-annotated regions.

    With no declared regions the whole recording is treated as annotated, which is
    the assumption the annotation itself makes.
    """
    if not regions:
        return True
    for region in regions:
        if start >= region.start_ms - 0.001 and end <= region.end_ms + 0.001:
            return True
    return False


def _summary(values):
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    mean = sum(ordered) / count
    if count == 1:
        median = ordered[0]
        p95 = ordered[0]
    else:
        median = (ordered[(count - 1) // 2] + ordered[count // 2]) / 2.0
        position = (count - 1) * 0.95
        lower = int(position)
        upper = min(lower + 1, count - 1)
        p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {
        'count': count,
        'mean_ms': round(mean, 3),
        'median_ms': round(median, 3),
        'p95_ms': round(p95, 3),
        'min_ms': round(ordered[0], 3),
        'max_ms': round(ordered[-1], 3),
    }


def _signed_summary(values):
    if not values:
        return None
    summary = _summary([abs(value) for value in values])
    summary['mean_signed_ms'] = round(sum(values) / len(values), 3)
    return summary


def _empty(status, reason, has_sample_set):
    return {
        'evaluation_policy_version': EVALUATION_POLICY_VERSION,
        'status': status,
        'reason': reason,
        'has_annotated_sample_set': has_sample_set,
        'annotation': None,
        'denominator': None,
        'coverage': None,
        'speech_start_error_ms': None,
        'speech_end_error_ms': None,
        'speech_start_absolute_error_ms': None,
        'speech_end_absolute_error_ms': None,
        'miss': None,
        'false_alarm': None,
        'per_document': [],
        'errors': [],
    }


def evaluate(annotation=None, documents=None):
    """Score produced acoustic documents against a manual annotation.

    ``annotation`` is an :class:`Annotations` (or ``None`` to report that no
    annotated sample set exists). ``documents`` is a list of produced
    ``AcousticSegments`` dictionaries.

    The result always names its status, its reason and its denominator, so a
    reader can tell "measured, and here is the denominator" from "not measured".
    """
    if annotation is None:
        return _empty(NO_ANNOTATED_SAMPLE_SET,
                      'No manually annotated real-recording sample set was supplied. '
                      'Speech start/end error, miss/false-alarm behaviour and coverage '
                      'cannot be reported, and no accuracy claim is made.',
                      False)
    documents = list(documents or [])
    identity_failures = []
    for index, document in enumerate(documents):
        actual = document.get('source', {}).get('sha256')
        if actual != annotation.source['sha256']:
            identity_failures.append({
                'document_index': index,
                'document_id': document.get('document_id'),
                'document_source_sha256': actual,
                'annotation_source_sha256': annotation.source['sha256'],
            })
    if identity_failures:
        raise AnnotationError(
            'Acoustic documents do not come from the annotated recording: '
            + '; '.join(str(item) for item in identity_failures))
    if not annotation.intervals:
        result = _empty(EMPTY_ANNOTATION_SET,
                        'The annotated sample set contains no speech intervals, so no '
                        'detection error can be computed.', True)
        result['annotation'] = {'annotation_id': annotation.annotation_id,
                                'annotator': annotation.annotator,
                                'source_sha256': annotation.source['sha256']}
        result['denominator'] = {
            'annotated_intervals': 0,
            'annotated_speech_ms': 0.0,
            'annotated_region_ms': round(_annotated_ms(annotation), 3),
            'recording_duration_ms': round(annotation.duration_ms, 3),
            'documents_evaluated': len(documents),
            'predicted_segments_evaluated': sum(len(document.get('segments') or [])
                                                for document in documents),
            'intervals_with_a_match': 0,
        }
        return result
    if not documents:
        result = _empty(NO_ANNOTATED_SAMPLE_SET,
                        'An annotation was supplied but no produced acoustic document was '
                        'scored, so no detection error is reported.', True)
        result['annotation'] = {'annotation_id': annotation.annotation_id,
                                'annotator': annotation.annotator,
                                'source_sha256': annotation.source['sha256']}
        return result

    annotated_ms = _annotated_ms(annotation)
    speech_ms = sum(interval.duration_ms for interval in annotation.intervals)
    intervals_total = len(annotation.intervals)
    start_errors = []
    end_errors = []
    matched_total = 0
    matched_speech_ms = 0.0
    matched_prediction_ms = 0.0
    false_alarm_total = 0
    false_alarm_ms = 0.0
    unmeasurable_total = 0
    duration_total = 0.0
    predicted_total = 0
    per_document = []
    for document in documents:
        predicted = list(document.get('segments') or [])
        matches, used = _match(annotation.intervals, predicted)
        start_errors.extend(item['start_error_ms'] for item in matches)
        end_errors.extend(item['end_error_ms'] for item in matches)
        matched_speech_ms += sum(annotation.intervals[item['annotation_index']].duration_ms
                                 for item in matches)
        matched_prediction_ms += sum(
            predicted[item['segment_index']]['end_ms'] - predicted[item['segment_index']]['start_ms']
            for item in matches)
        document_false_alarms = []
        document_unmeasurable = []
        for index, segment in enumerate(predicted):
            if index in used:
                continue
            if _in_annotated_region(segment['start_ms'], segment['end_ms'],
                                    annotation.annotated_regions):
                document_false_alarms.append(segment['segment_id'])
                false_alarm_ms += segment['end_ms'] - segment['start_ms']
            else:
                document_unmeasurable.append(segment['segment_id'])
        matched_total += len(matches)
        false_alarm_total += len(document_false_alarms)
        unmeasurable_total += len(document_unmeasurable)
        duration_total += annotation.duration_ms
        predicted_total += len(predicted)
        per_document.append({
            'document_id': document.get('document_id'),
            'method': document.get('processor', {}).get('method'),
            'predicted_segments': len(predicted),
            'matched_intervals': len(matches),
            'missed_intervals': intervals_total - len(matches),
            'false_alarm_segments': len(document_false_alarms),
            'unmeasurable_segments': len(document_unmeasurable),
            'matches': matches,
        })

    expected_matches = intervals_total * len(documents)
    missed_total = expected_matches - matched_total
    denominator = {
        'annotated_intervals': intervals_total,
        'annotated_speech_ms': round(speech_ms, 3),
        'annotated_region_ms': round(annotated_ms, 3),
        'recording_duration_ms': round(annotation.duration_ms, 3),
        'documents_evaluated': len(documents),
        'predicted_segments_evaluated': predicted_total,
        'intervals_with_a_match': matched_total,
        'expected_matches': expected_matches,
    }
    coverage = {
        'matched_interval_ratio': round(matched_total / expected_matches, 6) if expected_matches else None,
        'matched_speech_ratio': round(matched_speech_ms / (speech_ms * len(documents)), 6)
        if speech_ms else None,
        'annotated_region_ratio': round(annotated_ms / annotation.duration_ms, 6)
        if annotation.duration_ms else None,
        'segments_outside_annotated_regions': unmeasurable_total,
    }
    return {
        'evaluation_policy_version': EVALUATION_POLICY_VERSION,
        'status': 'evaluated',
        'reason': None,
        'has_annotated_sample_set': True,
        'annotation': {
            'annotation_id': annotation.annotation_id,
            'annotator': annotation.annotator,
            'source_sha256': annotation.source['sha256'],
            'boundary_uncertainty_ms': annotation.boundary_uncertainty_ms,
            'annotated_region_declared': bool(annotation.annotated_regions),
        },
        'denominator': denominator,
        'coverage': coverage,
        'speech_start_error_ms': _signed_summary(start_errors),
        'speech_end_error_ms': _signed_summary(end_errors),
        'speech_start_absolute_error_ms': _summary([abs(value) for value in start_errors]),
        'speech_end_absolute_error_ms': _summary([abs(value) for value in end_errors]),
        'miss': {
            'missed_intervals': missed_total,
            'missed_speech_ms': round(speech_ms * len(documents) - matched_speech_ms, 3),
            'miss_ratio': round(missed_total / expected_matches, 6) if expected_matches else None,
        },
        'false_alarm': {
            'false_alarm_segments': false_alarm_total,
            'false_alarm_ms': round(false_alarm_ms, 3),
            'matched_prediction_ms': round(matched_prediction_ms, 3),
            'false_alarm_per_minute': round(
                false_alarm_total / (duration_total / 60000.0), 6) if duration_total else None,
        },
        'per_document': per_document,
        'errors': [],
        'note': ('Synthetic or machine-generated annotations must never be reported as '
                 'real-recording acceptance evidence. All times are milliseconds on the '
                 'annotated artifact time base; prediction coordinates are '
                 'audio_relative_ms of the same artifact.'),
    }


def _annotated_ms(annotation):
    if annotation.annotated_regions:
        return sum(region.duration_ms for region in annotation.annotated_regions)
    return annotation.duration_ms


def evaluate_files(annotation_path, document_paths):
    """Load an annotation file and produced documents, then :func:`evaluate`."""
    import json
    annotation = load_annotation_file(annotation_path)
    documents = []
    for path in document_paths:
        try:
            documents.append(json.loads(Path(path).read_text(encoding='utf-8-sig')))
        except (OSError, ValueError) as error:
            raise AnnotationError(f'Cannot read acoustic document {path}: {error}') from None
    for document in documents:
        if document.get('schema_version') != '1.0.0':
            raise AnnotationError(
                'Acoustic document is not AcousticSegments 1.0.0: '
                + str(document.get('schema_version')))
    return evaluate(annotation, documents)
