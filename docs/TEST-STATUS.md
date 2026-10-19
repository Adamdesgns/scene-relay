# Test status: 0.1.1

Scene Relay by [@adamdesgns](https://x.com/adamdesgns). Updated **2026-10-19**.

The unchanged release ZIP passed offline verification and a later live smoke test. The [sanitized release record](../release-verification-0.1.1.json) identifies the tested archive and contains the detailed results. These repository docs supplement the ZIP's earlier documentation; they do not indicate a rebuilt archive.

| Check | Observed result | Limit |
|---|---|---|
| Source offline suite | **148 tests: 146 passed, two skipped.** | Windows symlink-capability skips; synthetic tests do not prove live account access. |
| Exact archive offline suite | **148 tests: 146 passed, two skipped**, after extraction into an isolated deep installation path with a short data root. | This is archive execution, not native plugin installation. The two runs overlap and are not additive coverage. |
| Extracted MCP | Initialization and health checks passed. | Health checks dependency presence; it is not a model or billing check. |
| Exact-package live authoring | Official Claude Code **2.1.301** completed with `model_verified: true`. The requested `claude-opus-5-5` matched initialization, **four assistant messages**, and final model usage. | Other CLI stream formats, models, accounts, and providers are unverified. |
| Live authentication | Existing `claude.ai` / `firstParty` **Max** route matched before authoring; no API overrides or fallback. | Remaining allowance, extra-usage settings, and actual charges were not established. |
| Live rendering | **3 seconds, 1920x1080, 30 fps, H.264 and stereo AAC**, using HyperFrames **0.8.169**. Technical render/media validation passed. | **Two nonblocking warnings** remained. A smoke test is not a general quality guarantee; no human listening review is claimed. |

## Remaining compatibility limits

Persistent native installation, actual `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` expansion in an installed host, a fresh desktop workflow, other operating systems, and other authentication routes remain untested. The current MCP-created engine expects 1920x1080 output; rendering uses 30 fps and requires audio. Supply reviewed local seed dependencies and audio, which are not bundled. An explicit short data root is required by the documented Windows setup; see the [README](../README.md).

Strict model verification is performed after execution. Rejecting missing or mismatched evidence does not undo already incurred usage. The tested personal workflow requires an existing eligible Claude subscription and `require_existing_claude_login: true`; the free plugin does not include model access or remove plan limits and extra-usage charges.

## Historical evidence

The **v0.1.0** development workflow produced a separate 40-second 1080p film with stereo audio. It recorded `claude-opus-5-5` at CLI initialization but did not enforce v0.1.1's full model-evidence check. That historical render had ten nonblocking warnings. It is neither the v0.1.1 smoke test nor a fresh installation test, and its production assets and private runtime records are not part of this public source release.

See the [build report](BUILD-REPORT.md) and [release notes](RELEASE-0.1.1.md). The repository's updated reports omit personal machine paths, job identifiers, private prompts, transcripts, and smoke assets.
