---
name: github-app-workflow
description: Use per-agent GitHub App identity with runtime-minted, repo-scoped tokens for GitHub operations.
version: 0.3.0
author: Hermes GitHub App Plugin Contributors
---

# GitHub App Workflow

Use this skill for GitHub operations from Hermes agents that are configured with a per-agent GitHub App. Every token is minted at runtime, scoped to a single repository with a minimal permission set, and expires within an hour. Nothing is persisted.

## How tokens are minted here

Check `GHAPP_BROKER_SOCKET`, then `GHAPP_BROKER_URL`:

- **Either set (broker mode — the normal case in terminal sessions):** tokens come from the broker — over a unix socket (`GHAPP_BROKER_SOCKET`, same-host) or plain HTTP (`GHAPP_BROKER_URL`, e.g. a broker Service in the same Kubernetes namespace). This process holds no key material. The broker enforces a repository allowlist and permission ceiling; out-of-policy requests are denied with an explanatory error, and every request is audited. If a mint is denied, the policy is the answer — do not look for another credential.
- **Both unset (local mode):** tokens are minted in-process from the configured App key.

## First-time setup (local mode only)

Run `ghapp setup` to write `github_app` config. Required: client ID, installations (`owner=id[,owner=id]` — one per account the app is installed on), and a private key source (path or command). Then `ghapp doctor --repo OWNER/REPO` verifies console scripts, config, minting, and repository access. Use `--skip-network` only for container/image builds.

## Rules

1. Prefer `github_app_*` plugin tools for GitHub API operations.
2. From the terminal, plain `git push`/`git pull` on **HTTPS remotes** works when the `ghapp` credential helper is configured (`credential.helper=ghapp` + `useHttpPath=true`) — a fresh single-repo token is minted per operation. Otherwise use `git-app --repo OWNER/REPO -- push ...`.
3. Prefer `gh-app --repo OWNER/REPO -- ...` over bare `gh`. The `--repo` is required: tokens are per-repository.
4. Never use SSH remotes for bot-managed worktrees. SSH uses local keys, not the App identity.
5. Do not rely on `gh auth status` as proof of identity; it reports local `gh` credentials and may show a human account.
6. Verify App mode before writes with `github_app_verify_identity` or `ghapp status --repo OWNER/REPO`.
7. Do not use `@me`; the actor is `<app-slug>[bot]`, not a human.
8. Request only the permissions the operation needs (`--permission contents=write`, `issues=write`, ...). Broker policy denies over-asks.
9. A 403/denial from the broker or a 404 from GitHub on another repo is the security design working, not a bug to work around.

## Verification

Before reporting success for a write, include: repository, operation, URL or API path, `auth_mode: github_app`, app slug, installation ID, and the granted `scoped_permissions` from the tool/CLI auth metadata.
