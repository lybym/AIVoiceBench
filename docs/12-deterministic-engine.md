# Deterministic canonical-event evaluation

```powershell
python -m aivoicebench analyze examples/test-case.example.yaml --timeline examples/timeline.example.json
```

`analyze` validates the Case/Timeline identity and versions, then writes Case/Timeline snapshots, canonical metrics and input/output hashes into a new ignored analysis directory. Synthetic input remains synthetic. Real/imported Timeline evaluation requires `--artifact-root` and verified file hashes; missing files produce insufficient_evidence, not measurements based solely on claimed metadata.

Implemented event selection: final tester utterance end to first associated response onset; planned-pause false endpoint; active old-response end after interruption; overlap of speech interval unions; eligible timeout outcome; device-log ASR CER. Exact formulas and limits are in `03-metric-definition.md`. Unknown or unimplemented semantic metrics stay insufficient_evidence. Barge-in stop latency alone never proves that the new intent was answered.

Conservative prerequisites: complete observation coverage, synchronized tracks and acoustic/reviewed boundaries. Derived timing is accepted only when it references audio/manual evidence. ASR estimates, scheduler intent and device-log stage timestamps cannot be relabeled black-box acoustic boundaries. Partial timelines currently block all metric calculation, even if some windows could eventually be salvaged; per-window eligibility is future work.

False-endpoint absence requires a device-output evidence snippet covering the whole planned pause plus observed tester resumption. An early response makes normal E2E first-audio latency inapplicable. Barge-in requires an identified old response strictly active at interruption. Timeout absence requires the final tester turn's response to complete before the configured deadline; a positive timeout needs a controller event and healthy output coverage through the deadline.

Case deterministic assertions become traceable Case-version threshold policies. A timing uncertainty interval crossing the threshold yields insufficient_evidence. Semantic/assertion criteria are not replaced by numeric proxies. The engine neither creates an internal root cause nor invents a Finding.

`aggregate_latency` uses R7 on compatible single-sample results, excludes missing values, retains counts and run-qualified Evidence references, and rejects duplicate metric IDs/mixed scope or execution kinds. The report container will resolve cross-run references and baseline compatibility; aggregation is not an automatic release gate.

Verified with synthetic canonical fixtures: VAD pause remains unbroken, E2E is 1380 ms, early endpoint fixture fails its Case assertion, and barge-in stops the old response after 200 ms. These values reproduce fixture arithmetic only. Raw-audio speech detection, automatic response/turn association and live controller integration remain pending; do not treat this event-consumer milestone as a complete HIL event extraction system.
