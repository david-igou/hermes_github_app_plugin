# Hermes GitHub App Plugin / ghapp

Runtime-minted, **minimally scoped** GitHub tokens from a GitHub App: every
token is bound to a **single repository** with an explicit **permission set**,
expires within an hour, and is never written to disk. Ships as a Hermes agent
plugin and as a standalone CLI (`ghapp`) usable in any dev environment.

Two operating modes, selected by one environment variable:

- **Local mode** (default): this process holds the App private key (file,
  inline env, or a command like `op read ...` executed at mint time) and mints
  directly. For interactive/dev environments.
- **Broker mode** (`GHAPP_BROKER_SOCKET=/run/ghbroker/ghbroker.sock`, or
  `GHAPP_BROKER_URL=http://ghbroker:8085` for an in-cluster broker pod): tokens
  are requested from a broker daemon. The client
  process holds **no key material** — for containerized agent execution, where
  the key must not be readable from the agent's shell. The broker enforces a
  repository allowlist + permission ceiling and audits every decision.

```
agent container                      trusted host side
┌────────────────────────┐          ┌─────────────────────────────┐
│ git push (cred helper) │──UDS────▶│ ghapp serve                 │──▶ GitHub
│ gh-app / ghapp token   │  socket  │  policy: repos ∩ perms      │   App API
│ github_app_* tools     │          │  audit → journald           │
│ (no key material)      │          │  key: 0600, own user        │
└────────────────────────┘          └─────────────────────────────┘
```

## Configuration (local mode / broker host)

```yaml
github_app:
  client_id: "Iv23exampleclientid"
  app_slug: "igou-dev"
  installations:            # one entry per account the app is installed on
    david-igou: "143866260"
    igou-io: "143866153"
  private_key_cmd: op read op://lab_external_api_keys/igou-dev-github-app/private_key
  # or: private_key_path: ~/.config/ghapp/key.pem
  default_permissions:      # used when a mint doesn't specify permissions
    contents: write
    pull_requests: write
    issues: write
```

Config lives in `~/.hermes/config.yaml` (or set `GHAPP_CONFIG=/path/to/file`).
Environment variables override: `GITHUB_APP_CLIENT_ID`,
`GITHUB_APP_INSTALLATIONS` (`owner=id[,owner=id]`), `GITHUB_APP_PRIVATE_KEY`,
`GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_PRIVATE_KEY_CMD`,
`GITHUB_APP_SLUG`, `GITHUB_API_URL`.

The owner half of `OWNER/REPO` selects the installation, so one app installed
on several accounts needs no per-call installation plumbing. The legacy
single `installation_id` key still works.

## Install

```bash
pip install "hermes-github-app-plugin @ git+https://github.com/david-igou/hermes_github_app_plugin@main"
ghapp setup            # interactive; --non-interactive with flags for scripts
ghapp doctor --repo OWNER/REPO
```

## Everyday use

### Plain git via the credential helper

```ini
# gitconfig
[credential "https://github.com"]
  helper = ghapp
  useHttpPath = true
```

With that in place, `git clone/fetch/push` on HTTPS GitHub remotes just works:
the helper receives the repo path, mints a `contents: write` token for exactly
that repository, and hands it to git. Nothing is stored (`store`/`erase` are
no-ops). SSH remotes bypass the App identity — don't use them for bot work.

### gh and scripts

```bash
gh-app --repo OWNER/REPO -- pr list             # GH_TOKEN scoped to that repo
gh-app --repo OWNER/REPO --permission contents=read -- run list
git-app --repo OWNER/REPO -- push origin branch # askpass variant, no helper needed
ghapp token --repo OWNER/REPO --permission contents=read [--json]
ghapp api /repos/OWNER/REPO/issues --method POST --data '{"title": "..."}'
ghapp status --repo OWNER/REPO
```

`--repo` is required everywhere a token is minted: tokens are per-repository
by design. Request only the permissions the operation needs; unspecified
mints use `default_permissions` (or the broker's defaults in broker mode).

## The broker (containerized execution)

On the trusted host, as a dedicated user that owns the key:

```bash
ghapp serve --socket /run/ghbroker/ghbroker.sock --policy /etc/ghbroker/policy.yaml
```

```yaml
# /etc/ghbroker/policy.yaml — root-owned, immutable to the agent
policy:
  allowed_repos:            # OWNER/REPO, fnmatch globs allowed
    - igou-io/igou-ansible
    - igou-io/igou-inventory
  max_permissions:
    contents: write
    pull_requests: write
    issues: write
  default_permissions:
    contents: read
  repo_max_permissions:       # optional per-repo caps (fnmatch globs)
    igou-io/igou-inventory:   # tightening only — cannot add permissions;
      contents: read          # requests above the cap are denied,
    igou-io/igou-docs:        # default mints are clamped down to it.
      contents: read          # level "none" removes the permission entirely.
```

Mount the socket into the agent container and set
`GHAPP_BROKER_SOCKET=/run/ghbroker/ghbroker.sock`; every client above (and the
Hermes tools) switches to broker mode automatically.

Where clients are other **pods** and a socket cannot cross the boundary
(Kubernetes), run the broker as its own pod with a TCP listener instead:

```bash
ghapp serve --listen 0.0.0.0:8085 --policy /etc/ghbroker/policy.yaml
```

and point clients at it with `GHAPP_BROKER_URL=http://<service>:8085`
(`GHAPP_BROKER_SOCKET` wins if both are set). The TCP listener itself is
unauthenticated — reachability IS the client authorization, exactly like
socket possession in unix mode — so scope it with NetworkPolicy and never
expose it outside the namespace. The policy engine and audit stream are
identical in both modes; TCP audit lines carry the peer address instead of
pid/uid/gid (no `SO_PEERCRED` on TCP). Requests exceeding policy
are **denied, not clamped**, and every decision is a structured JSON line on
stdout (journald under systemd): decision, repo, requested vs granted
permissions, peer pid/uid/gid, expiry.

API: `POST /token {"repo": "OWNER/REPO", "permissions": {...}}`,
`GET /status`, `GET /healthz`.

Layered enforcement, outermost first: GitHub installation repo allowlist →
app permission ceiling → broker policy → per-token scope (single repo,
minimal perms, ≤1 h TTL).

## Hermes tools

The plugin registers `github_app_status`, `github_app_verify_identity`,
`github_app_api`, `github_app_graphql`, `github_app_create_issue`,
`github_app_comment_issue`, `github_app_create_pr`, `github_app_comment_pr`.
All of them mint per-repo tokens through the active backend and return auth
metadata (app slug, installation, scoped repositories/permissions, redacted
token, expiry). Mutating tools use the minimal permission set for the
operation (`issues: write` for issues, `pull_requests: write` for PRs, ...).

```bash
hermes plugins enable github-app
```

## Migrating agent skills and jobs

- Prefer `github_app_*` tools for API operations.
- `gh ...` → `gh-app --repo OWNER/REPO -- ...`.
- `git push` → plain push over HTTPS with the credential helper, or
  `git-app --repo OWNER/REPO -- push ...`.
- No SSH remotes for bot-managed worktrees; no `gh auth status` as identity
  proof; no `@me` (the actor is `<app-slug>[bot]`).
- Write summaries should include `auth_mode`, `app_slug`, `installation_id`,
  repository, operation, and the granted `scoped_permissions`.

## Development

```bash
pip install -e '.[dev]'
ruff format --check . && ruff check . && mypy && pytest
```

Design docs: [docs/design-minimal-runtime-tokens.md](docs/design-minimal-runtime-tokens.md),
[docs/github-app-setup.md](docs/github-app-setup.md).

## Lineage and licensing

Started as a fork of `PickNikRobotics/hermes_github_app_plugin` and has since
diverged permanently (down-scoped multi-installation minting, the broker, the
credential helper). MIT licensed; original attribution retained in LICENSE.
This package is **not** published to PyPI (that name belongs to upstream) —
install from a git ref.
