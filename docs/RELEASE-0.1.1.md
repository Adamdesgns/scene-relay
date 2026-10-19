# Scene Relay 0.1.1

**Brief. Build. Revise. Render.** By [@adamdesgns](https://x.com/adamdesgns). Release documentation updated **2026-10-19**.

Scene Relay connects a local Codex workflow to the user's own unmodified Claude Code for editable video authoring, followed by a controlled local HyperFrames render.

This version adds exact requested-model verification and safer Windows path handling. Missing, conflicting, or incomplete model evidence fails authoring. Windows data roots are capped at 120 UTF-16 code units; compact atomic-write filenames and per-path checks avoid reliance on system long-path settings.

## Tested asset

- Filename: **`scene-relay-plugin-0.1.1.zip`**
- Size: **94,023 bytes**
- SHA-256: **`0102e9fe0bc84ca36b782c23b6b0c2e6b116334cf3c7bda035a942b9d6d78c36`**

This ZIP is the unchanged tested artifact. Repository docs add later smoke-test evidence and therefore differ from the ZIP's earlier documentation. Repacking this repository will create a different archive. Use [SHA256SUMS](../SHA256SUMS) and the [sanitized verification record](../release-verification-0.1.1.json) to verify the release asset.

Choose the named **`scene-relay-plugin-0.1.1.zip`** release asset for the verified hash. GitHub's automatically generated **Source code** archives are repository snapshots and have different hashes.

## Verification summary

- Source and exact-archive offline suites: **146 passed, two skipped each**.
- Extracted MCP initialization and health: passed.
- Exact-package live authoring: Claude Code **2.1.301**, exact **`claude-opus-5-5`** in initialization, four assistant messages, and final usage; `model_verified: true`.
- Existing **`claude.ai` / `firstParty` Max** authentication, without API overrides or fallback.
- Completed **3-second 1080p30 H.264 video with stereo AAC**, using HyperFrames **0.8.169**; two nonblocking warnings.

The separate 40-second v0.1.0 demonstration is historical evidence and did not use this version's full model-verification gate.

## Before use

The tested setup requires an existing eligible Claude subscription, official Claude Code, Python 3.11+, Node 22+, HyperFrames 0.8.169, an existing compatible Chromium browser, FFmpeg, and ffprobe. Use `require_existing_claude_login: true` and an explicit short writable data root. The current MCP-created engine expects 1920x1080 output at the renderer's 30 fps. Supply a reviewed seed with local dependencies and valid HTML audio or `assets/sound-design.wav`; no seed or media is bundled. No API setup is needed for the tested route. Follow the [manual MCP/harness instructions](../README.md).

The plugin is free; inference is not guaranteed free. Plan limits and any enabled extra usage apply. Model verification occurs after execution and cannot reverse charges. No provider, entitlement, or legal guarantee is made.

Persistent native installation, installed-host placeholder expansion, a fresh desktop workflow, other operating systems, and other authentication routes remain untested. No human listening review is claimed. This independent release is not an official OpenAI directory listing or an Anthropic/OpenAI endorsement.

[MIT license](../LICENSE). Third-party software and media retain their own terms. Private runtime data and smoke-test assets are not published with this source release.
