# Build report: 0.1.1

Scene Relay by [@adamdesgns](https://x.com/adamdesgns). Updated **2026-10-19**.

Version 0.1.1 adds strict model-evidence verification and Windows path protections. The tested release asset is `scene-relay-plugin-0.1.1.zip`, **94,023 bytes**, with SHA-256:

```text
0102e9fe0bc84ca36b782c23b6b0c2e6b116334cf3c7bda035a942b9d6d78c36
```

The asset remains unchanged. Repository documentation was updated after the later live smoke test; the archived documentation predates that test. Runtime source remains the tested implementation. Consult the [sanitized verification record](../release-verification-0.1.1.json) and [checksums](../SHA256SUMS) for the published asset.

## Changes

Model verification requires a valid initialization model and nonempty final `modelUsage`. Every observed initialization model, supplied assistant model, and final usage key must exactly match the requested identifier. No alias, case, or provider normalization is performed. Missing, invalid, incomplete, or mismatched evidence fails authoring. Only verified success records `model_verified: true`; evidence tracking remains bounded independently of the progress-event ring. Verification happens after execution and cannot undo incurred usage.

On Windows, the normalized data root is limited to **120 UTF-16 code units**, checked files to **259**, and directories to **247**. An overlong root fails before configuration or job writes. Compact exclusive temporary filenames preserve atomic state replacement without requiring system long-path changes. Deep installation paths and short data roots are separate choices; nested assets still need to fit their per-path limits.

## Verification

The source suite and the exact archive's isolated extraction suite each ran **148 tests: 146 passed and two Windows symlink-capability skips**. The archive was exercised from a deep installation path with a short data root. Extracted MCP initialization and health checks also passed.

The later live smoke test used the exact extracted package and official **Claude Code 2.1.301**. The requested **`claude-opus-5-5`** matched initialization, **four assistant messages**, and final model usage. `model_verified` was true. The user's existing **`claude.ai` / `firstParty` Max** route matched preflight and authoring, with no detected API overrides and no fallback.

The resulting HyperFrames **0.8.169** render was **3 seconds, 1920x1080 at 30 fps, H.264 with stereo AAC**. Technical render/media validation and sampled-frame review passed with **two nonblocking warnings**: `studio_missing_editable_id` and `nested_structure_needs_subcomposition`. No human listening review or general visual-quality guarantee is claimed.

## Scope

The tested setup uses an existing eligible Claude subscription; no API setup is needed for that route. The plugin source is free, while access, plan limits, and any extra usage remain the user's responsibility. No remaining-allowance or final-bill guarantee is made.

The current MCP-created engine expects 1920x1080 output, and rendering uses 30 fps. The seed must supply reviewed local dependencies and valid audio through HTML or `assets/sound-design.wav`; the validated output requires audio. No seed or media is bundled in the source ZIP.

Persistent native installation, installed-host placeholder expansion, a fresh desktop workflow, other operating systems, and other authentication routes remain untested. The historical v0.1.0 40-second demonstration is separate evidence and used the earlier initialization-only model observation.

The updated repository reports omit personal paths and job IDs. Runtime projects, account credentials, private prompts, raw logs, and smoke assets are not included in this source release. Third-party dependencies and production media are not bundled. The existing MIT license and **@adamdesgns** credit are retained. See [test status](TEST-STATUS.md), [authentication boundaries](AUTHENTICATION.md), and [release notes](RELEASE-0.1.1.md).
