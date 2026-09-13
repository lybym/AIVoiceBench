# Model management

> 2026-09-11 基线：对齐 main `8d01ef2` / `v0.3.2`（本文件最初的审计基线 `c612d36` / alpha.2 属历史记录）。当前主链与验证限制见 [Recording Backbone](23-recording-backbone.md)；真实验收仍待完成。

The browser model manager separates provider profiles from capability routes. Five purposes are configured independently: **`asr`** (File ASR for Recording Analysis), **`streaming_asr`** (session-based recognition for Active Voice Test), `tts`, `diarization` and result `judge`. Profiles declare protocol, endpoint, model ID, parameters and credential reference; default routes select a profile per purpose. Changes apply at the next Run. Unsupported speech adapters are explicitly not_integrated; saving configuration does not invoke a provider or certify connectivity.

File ASR and Streaming ASR are deliberately separate purposes rather than one vague `default_asr`: the first recognizes a finished External Recording for Recording Analysis; the second runs a live session for Active Control and text/semantic observation (PRD-F016, [Streaming ASR 边界](24-streaming-asr.md)). Neither provider's timestamps are formal acoustic boundaries. Active Measurement speech boundaries come from the Live Measurement Audio processor, while Streaming ASR text may be referenced as semantic Evidence. A configuration written before `streaming_asr` existed loads with that purpose unconfigured and reports `routes_defaulted`; existing purposes are never re-pointed silently.

M1 wires the configured Volcengine ASR route for File ASR. ASR-native diarization reuses the selected ASR response when its route selects an enabled volcengine_asr profile; bind both routes to the same profile. The `streaming_asr` route selects the Volcengine big-model streaming protocol and drives the free-mode capture path; it does not require signed-URL audio publication. Judge configuration can be captured, but ImportRun does not execute it. ASR/clusters do not imply role attribution. Vosk remains intact.

## Reference and adaptation

Reviewed the local DeepSeek Harness source: packages/llm/llm-pi-ai/src/config.ts (provider profiles and credentials), packages/host/apiproxy/src/api/settings.ts (redacted descriptors and expected revisions), and dynamic-config.spec.ts (request-level resolution). AIVoiceBench implements its own Python storage and validation around those ideas; it does not vendor or clone the Harness architecture. No guessed model IDs, voices or speech endpoint presets are shipped.

## Persistence and credentials

Settings use SQLite transactions in AIVOICEBENCH_OUTPUT/.model-settings/credentials.sqlite3, persisted by the existing output volume. The local database contains credentials in plaintext and must remain private; Linux directory/file modes are 0700/0600. The Web API only returns configured flags. A credential_env reference takes precedence over a locally saved key; keys can be updated, preserved by omission or explicitly cleared. Secrets never enter Run snapshots. Invalid configuration responses never echo request bodies. Model configuration APIs are for the existing trusted single-user deployment, not a multi-tenant admin system. Compose binds localhost; use an authenticated proxy before making the application available remotely.

## Revisions and Run evidence

Writes require expected_revision; stale editors receive 409 instead of replacing newer settings. Profile/schema validation, route validation, persistence and revision increment share a transaction. Removing a profile removes its local secret; active routes must be cleared. Each Web Run resolves settings once, captures a secret-free model-config.json and registers its SHA256 in the import manifest. Snapshots live inside the selected analysis revision. Existing revisions retain their snapshot after subsequent edits. Stored settings override legacy LLM environment routing after the first save; before that, environment routing remains available.

## Verification

Tests cover credential redaction, persistence, env precedence, stale writes, invalid routes/parameters/URLs, cross-origin mutation rejection, unsupported adapters, no-network save operations and immutable Run snapshots. Docker release smoke includes model settings CRUD/redaction and codec imports. These checks do not constitute external API connectivity or real-recording accuracy acceptance.
