# Authentication and usage

Scene Relay **0.1.1** by [@adamdesgns](https://x.com/adamdesgns). Official authentication, headless-operation, costs, and legal pages rechecked on **2026-10-19**.

## Local personal workflow

Scene Relay is a free local plugin. It requires a local Codex installation, the official unmodified Claude Code CLI, and your own eligible Claude access. The plugin does not include a Claude subscription, API credits, or inference. Local rendering also needs the tools listed in the installation README.

The historical v0.1.0 live test used the creator's existing Claude Code login for a personal launch-video brief. A later exact-package v0.1.1 smoke test also completed using existing `claude.ai` / `firstParty` Max authentication without API overrides or fallback. See the [public release addendum](RELEASE-0.1.1.md). That one account's success does not establish another account's entitlement, quota, or charge. Using or distributing this package does not itself authorize a model request, login, token generation, account change, terms acceptance, purchase, or API/provider fallback.

Claude Code performs authentication and sends the model request. Scene Relay must not inspect credential files, copy session tokens, implement its own Claude-account login, or expose secrets through prompts, command arguments, logs, job results, or the video. Sign-in and renewal belong in Anthropic's own CLI flow, performed by the user when needed.

## Authentication choices and billing

The official CLI supports Claude-account sign-in, Console/API access, and supported cloud-provider configurations. Scene Relay must preserve these official alternatives; it must not patch the binary or impose a subscription-only product restriction. Users select and authorize their own route through the official tools. [Claude Code authentication](https://code.claude.com/docs/en/authentication)

**Check the active route before a model job.** In non-interactive `-p` mode, an inherited `ANTHROPIC_API_KEY` is used when present, even if a subscription login also exists. Provider selectors, bearer tokens, credential helpers, and profiles can also affect the route. A prior subscription login alone does not prove the next call uses that subscription. [Authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence)

Use the CLI's read-only `claude auth status` command for a preflight; keep only needed status fields and omit account identifiers from shared evidence. It reports authentication, not remaining quota or guaranteed model entitlement. [CLI reference](https://code.claude.com/docs/en/cli-reference)

Scene Relay compares ordinary CLI authentication status with the same restricted configuration used for authoring. A route change stops the job. The example's `require_existing_claude_login: true` additionally requires `claude.ai` / `firstParty` and no detected credential/provider overrides. It is a per-job constraint for an authorized personal-login run, not a product-wide subscription requirement. Restricted authoring omits ordinary user/project customizations; settings-dependent routes must be supported and verified under that same scope.

Scene Relay must stop and report an unapproved route, expired login, unavailable model, or quota failure. It must not change accounts, remove credentials, switch providers/models, purchase credits, or retry through a paid API. A deliberately authorized official API/provider route remains available only where the effective configuration is supported and verified.

Subscription access still has usage limits. If the user has enabled additional usage credits, requests may use those credits after the plan allowance. Scene Relay does not inspect or change that account setting and cannot promise zero additional cost from authentication status alone. CLI cost estimates are not a guarantee of the actual bill. [Usage credits and costs](https://code.claude.com/docs/en/costs)

## Programmatic operation

Anthropic documents `claude -p` for scripts, piped input, and JSON/streamed JSON output. Scene Relay supervises that CLI process and applies its own job deadlines and output limits. Using `-p` is a supported calling mechanism; it does not itself settle licensing or distribution questions. `--bare` skips subscription OAuth credentials, so it is not a substitute for an authenticated personal CLI session. [Programmatic Claude Code](https://code.claude.com/docs/en/headless)

Version 0.1.1 also verifies model identity after execution. A valid initialization event and nonempty final model-usage record are required; observed initialization models, any supplied assistant model, and all final usage keys must exactly match the requested identifier. No alias/case/provider normalization occurs. A failed or incomplete check does not establish successful authoring and triggers no automatic retry or fallback. It cannot reverse usage already incurred. This stream contract was exercised by the later exact-package smoke test with Claude Code 2.1.301; other official authentication choices still need compatible output and a matched effective route.

## Current terms and distribution boundary

Anthropic permits end-user subscription sign-in to unmodified Claude Code, including on platforms. Products/services that run it require Commercial Terms, all built-in authentication choices, and direct end-user billing. Credential intermediation and usage resale are prohibited. Pro/Max limits assume ordinary individual usage; applicable terms depend on the account and use. [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)

This review found no explicit prohibition on Adam's personal local job. Distribution classification remains unresolved; free pricing establishes no exemption. No legal clearance or new terms acceptance is recorded. Review before public release; never turn the plugin into shared-account access.

Use Claude Code only as a factual plain-text reference, never as Scene Relay's brand or an endorsement/partnership claim. [Brand conditions](https://code.claude.com/docs/en/legal-and-compliance#can-customers-offer-claude-code-in-their-products)

## What remains to verify

- The historical v0.1.0 tests recorded initialization-only model evidence. The later v0.1.1 smoke test verified exact `claude-opus-5-5` initialization, four assistant messages, and final usage, with `model_verified: true`; see the [release addendum](RELEASE-0.1.1.md). Other accounts must verify their own access; a requested model name, installed string or authentication status alone does not prove entitlement.
- Authentication status does not verify remaining subscription allowance, usage-credit settings, or the final charge.
- Local test success does not authorize publication or establish how the terms apply to a distributed package.

The linked documentation can change. Re-check it before changing the authentication design or preparing a public release.
