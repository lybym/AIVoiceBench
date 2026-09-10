# Model management

The browser model manager separates provider profiles from capability routes (TTS, ASR, diarization and result Judge). Profiles declare protocol, endpoint, model ID, parameters and credential reference; default routes select a profile per purpose. Changes apply at the next Run. Unsupported speech adapters are explicitly not_integrated; saving configuration does not invoke a provider or certify connectivity. Only the existing OpenAI-compatible result Judge is wired by this milestone. Vosk and existing ASR infrastructure remain intact; cloud speech transport is a separate integration milestone.

## Reference and adaptation

Reviewed the local DeepSeek Harness source: packages/llm/llm-pi-ai/src/config.ts (provider profiles and credentials), packages/host/apiproxy/src/api/settings.ts (redacted descriptors and expected revisions), and dynamic-config.spec.ts (request-level resolution). AIVoiceBench implements its own Python storage and validation around those ideas; it does not vendor or clone the Harness architecture. No guessed model IDs, voices or speech endpoint presets are shipped.

## Persistence and credentials

Settings use SQLite transactions in AIVOICEBENCH_OUTPUT/.model-settings/credentials.sqlite3, persisted by the existing output volume. The local database contains credentials in plaintext and must remain private; Linux directory/file modes are 0700/0600. The Web API only returns configured flags. A credential_env reference takes precedence over a locally saved key; keys can be updated, preserved by omission or explicitly cleared. Secrets never enter Run snapshots. Invalid configuration responses never echo request bodies. Model configuration APIs are for the existing trusted single-user deployment, not a multi-tenant admin system. Compose binds localhost; use an authenticated proxy before making the application available remotely.

## Revisions and Run evidence

Writes require expected_revision; stale editors receive 409 instead of replacing newer settings. Profile/schema validation, route validation, persistence and revision increment share a transaction. Removing a profile removes its local secret; active routes must be cleared. Each Web Run resolves settings once, captures a secret-free model-config.json and registers its SHA256 in the import manifest. Existing Runs retain their snapshot after subsequent edits. Stored settings override legacy LLM environment routing after the first save; before that, environment routing remains available.

## Verification

Tests cover credential redaction, persistence, env precedence, stale writes, invalid routes/parameters/URLs, cross-origin mutation rejection, unsupported adapters, no-network save operations and immutable Run snapshots. Docker release smoke includes model settings CRUD/redaction and codec imports. These checks do not constitute external API connectivity or real-recording accuracy acceptance.
