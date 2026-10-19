# Scene Relay 0.1.1

**Brief. Build. Revise. Render.** A free local video plugin by [@adamdesgns](https://x.com/adamdesgns).

Scene Relay lets Codex direct your own unmodified Claude Code installation to author editable launch-video scenes. A separate local HyperFrames renderer checks the project and produces an MP4. Jobs have IDs, progress, deadlines, and cancellation limited to their own processes.

The exact v0.1.1 release ZIP passed both offline suites (**146 passed, two skipped each**) and a live Windows smoke test: **3 seconds, 1920x1080, 30 fps, H.264 video and stereo AAC audio**. Claude Code **2.1.301** reported `claude-opus-5-5` consistently in initialization, four assistant messages, and final model usage; `model_verified` was true. The renderer retained two nonblocking warnings. See [release notes](docs/RELEASE-0.1.1.md), [test status](docs/TEST-STATUS.md), and the [sanitized verification record](release-verification-0.1.1.json).

The release asset `scene-relay-plugin-0.1.1.zip` is unchanged from testing. These repository docs add the later smoke-test evidence; the ZIP's bundled docs predate that test. Persistent native installation, installed-host placeholder expansion, other operating systems, and other authentication routes remain untested. This is an independent project, with no Anthropic or OpenAI endorsement.

## Requirements

The tested setup requires:

- Windows and Codex with access to local execution or a manually configured stdio MCP server.
- **Python 3.11+** and **Node 22+**.
- The official unmodified **Claude Code 2.1.301**, already signed in through Anthropic's own flow, and an **existing eligible Claude subscription with access to the requested model**. The smoke test used Max. No API setup is needed for this tested route.
- **HyperFrames 0.8.169**, an existing compatible Chromium browser, **FFmpeg**, and **ffprobe**. The renderer downloads nothing.
- A reviewed seed project containing its local dependencies and assets, including valid audio. Use HTML audio or the adapter's fallback `assets/sound-design.wav`; validated output requires an audio stream. No seed or media is bundled in the source ZIP.
- At least **1 GiB available memory** and **1 GiB free disk** for authoring. Rendering requires **2 GiB free disk or three times the source size, whichever is greater**, plus the memory check. These are admission checks, not reserved resources.

**The plugin is free; inference is not guaranteed free.** Plan limits and any enabled extra usage apply. Authentication status does not establish remaining allowance or the final charge. The plugin does not implement login, extract credentials, buy credits, or automatically switch to an API/provider/model. See [authentication boundaries](docs/AUTHENTICATION.md).

## Use the local harness

Extract the release ZIP into a local folder and open a terminal there. Choose an explicit short writable data root and a reviewed seed project with local assets. The source ZIP does not include production or smoke-test assets. Replace these example paths with locations inside your approved workspace and permissions; the seed directory must already exist.

The current MCP-created engine expects **1920x1080** output and the renderer uses **30 fps**. Keep the seed and brief at those settings; arbitrary output resolutions are not supported by this documented setup.

```powershell
$srData = 'C:/sr/jobs'
$srSeed = 'C:/sr/seed'
python scripts/client.py scene_relay_health --data-root $srData --seed-directory $srSeed
```

Health checks send no model prompt. They check dependency presence, not full browser, authentication, or model compatibility.

On Windows, the normalized absolute data-root path must be at most **120 UTF-16 code units**. Checked file paths are limited to **259** and directory paths to **247**. An overlong root fails before configuration, lock, prompt, or job writes. The installation folder is separate from the data root, but nested assets must still fit the path limits. Use a shorter permitted location if rejected; extended-length/device paths and automatic system long-path changes are unsupported.

Create `brief.private.json` with your authorized brief:

```json
{
  "prompt": "Create an editable 40-second 1920x1080 launch video using the supplied brief and reviewed local assets. Do not install or publish anything.",
  "model": "claude-opus-5-5",
  "wall_timeout": 900,
  "max_turns": 12,
  "require_existing_claude_login": true
}
```

The personal-login constraint stops the job unless preflight and authoring use the same `claude.ai` / `firstParty` route with no detected API/provider overrides. It does not alter credentials. This is the tested per-job setup; other official authentication alternatives are not disabled globally and need their own compatible configuration and authorization.

Start authoring:

```powershell
python scripts/run_job.py author --input brief.private.json --data-root $srData --seed-directory $srSeed --record runtime/author-status.json
```

The harness prints a job ID and monitors that job. Keep the approved outer invocation alive: managed hosts may terminate descendants when it ends. Do not detach to bypass host policy or automatically resubmit after a timeout. Inspect the existing job ID first.

Review the returned source before an authorized render:

```powershell
python scripts/client.py get_video_result --job-id YOUR_JOB_ID --data-root $srData --seed-directory $srSeed
python scripts/run_job.py render --job-id YOUR_JOB_ID --data-root $srData --seed-directory $srSeed --record runtime/render-status.json
```

For status, use `get_video_job`. For an explicitly requested cancellation, use `cancel_video_job`; both accept `--job-id YOUR_JOB_ID --data-root $srData --seed-directory $srSeed`. Revision briefs can set `seed_job_ids: ["YOUR_PREVIOUS_JOB_ID"]` to create a separate take. Use the same data root, seed directory, and executable environment for every call in a collection. Never replace old jobs or change their configuration mid-run.

## Configure executables or a manual MCP host

Optional launch-environment variables select existing executables and assets:

| Variable | Value |
|---|---|
| `SCENE_RELAY_CLAUDE` | Official Claude executable; use native `claude.exe` on Windows |
| `SCENE_RELAY_HYPERFRAMES_CLI` | Installed `hyperframes/bin/hyperframes.mjs` for version 0.8.169 |
| `SCENE_RELAY_BROWSER_PATH` | Existing compatible Chromium executable |
| `SCENE_RELAY_SEED_DIRECTORY` | Reviewed local seed project |

Node, FFmpeg, and ffprobe must be on PATH. Use deliberate executable paths where discovery is ambiguous. No dependencies or account settings are installed or changed automatically.

A manually configured local MCP host can launch this stdio server from the extracted package folder:

```powershell
python -u server/mcp_server.py --data-root C:/sr/jobs --seed-directory C:/sr/seed
```

Supply absolute script and data paths when your host uses a different working directory. This launch command does not register anything persistently. The bundled skill is at `skills/scene-relay/SKILL.md`.

The portable manifests retain `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` for a compatible host. Native plugin installation and those installed-host paths were not exercised by this release. No turnkey installer is supplied. Follow the [official packaging guide](https://developers.openai.com/plugins/build/plugins) for that separate setup; an independent GitHub release is not an official directory listing.

## Verification and limits

Use a full model identifier. Authoring requires a valid initialization model and nonempty final `modelUsage`; every observed initialization model, supplied assistant model, and final usage key must equal the request exactly. No alias, case, or provider normalization occurs. Missing, invalid, incomplete, or mismatched evidence fails verification. This check happens after execution and cannot reverse inference or charges already incurred.

Prompts travel on stdin; shared progress retains selected metadata, not raw prompts, tool arguments, transcripts, or credentials. Authoring uses restricted file tools in its managed project. Timeout/cancel cleanup targets only the managed process tree. Path traversal, symlinks, and Windows reparse points are refused. Browser rendering is not a general hostile-code sandbox: use trusted reviewed projects and local assets.

Media probing and decode checks establish technical validity. Review playback and representative frames for quality; no human listening review is claimed for the smoke test. Stop on quota, authentication, model, permission, or dependency failures; there is no automatic provider fallback or unbounded retry.

Run offline tests with `python -m unittest discover -s tests -v`. They use synthetic workers and make no model calls. Rebuilding with `python scripts/package.py` produces a new local archive; the updated repository docs mean it will not reproduce the immutable release ZIP byte for byte.

## MCP tools

| Tool | Purpose |
|---|---|
| `scene_relay_health` | Check local dependency presence |
| `start_video_job` | Queue an authorized authoring job |
| `get_video_job` | Read bounded progress and state |
| `get_video_result` | List source and verified output artifacts |
| `render_video_job` | Queue a reviewed project's controlled render |
| `cancel_video_job` | Request cancellation of that managed job |

[MIT licensed](LICENSE) plugin source. Claude/Codex access, third-party tools, fonts, and media retain their own terms and licenses. Creator credit: **@adamdesgns**.
