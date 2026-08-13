# Design: minimally scoped runtime GitHub tokens for containerized execution

Status: proposed — 2026-07-02

## Problem

Both agent environments currently authenticate to GitHub with broad, long-lived
credentials:

- The devcontainer resolves static PATs from 1Password
  (`envs/claude-david-igou-github-token.env`, `envs/claude-igou-io-github-token.env`)
  into `GH_TOKEN`. Each PAT has standing write access to everything it covers.
- The Hermes terminal backend (podman containers of `ghcr.io/igou-io/igou-devenv`)
  gets a static `GH_TOKEN` via `docker_forward_env`, plus mounted `~/.config/gh`
  and `~/.ssh` — human-grade identity inside an autonomous agent's shell.

This plugin (forked to test) already mints GitHub App installation tokens, but it
was not designed for containerized execution:

1. **Full-scope minting.** `GitHubAppAuth.get_installation_token` POSTs to
   `/app/installations/{id}/access_tokens` with **no request body**, so every
   token carries the whole installation's repositories and permissions. The
   `--repo` flag is only a metadata tag.
2. **Key and consumer share a process space.** `gh-app` / `git-app` load the
   private key from local config and mint in-process. Inside a terminal
   container that means mounting the PEM into the agent's shell — the agent
   could read it and mint max-scope tokens directly.
3. **Single installation.** One `installation_id`; the fleet needs both
   `david-igou` and `igou-io`.

## Goals

- Tokens minted at runtime, per operation: scoped to **one repository** and a
  **minimal permission set**, TTL ≤ 1 hour, never written to disk.
- Usable in the devcontainer and in Hermes docker-based terminal execution with
  the same client UX (`git push` and `gh` just work).
- In Hermes, the App private key is **never readable by any agent-controlled
  process** (neither the gateway nor terminal containers).
- Every mint is audited (repo, permissions, caller, decision) via journald,
  feeding the existing journald→EDA pipeline.
- Retire the static PATs and the Hermes `.config/gh` / `.ssh` mounts.

## Decisions (settled)

- **Key custody: hybrid.** Devcontainer mints locally with the key fetched via
  `op read` at mint time. Hermes runs a host-side broker daemon that holds the
  key and enforces policy; containers get only a thin client.
- **App topology: per-agent apps.** Two GitHub Apps — `igou-dev` and
  `igou-hermes` — each installed on both `david-igou` and `igou-io`.
  Separate keys, separate bot identities in history, independently scoped
  installations (Hermes's repo allowlist can be narrower than Claude's).
- **Code home: evolve this fork** into the shared package.

## Architecture

One Python package, three layers:

```
┌─────────────────────────────────────────────────────────────┐
│ core      JWT → down-scoped installation token              │
│           multi-installation (owner → installation_id)      │
│           key sources: file | env | command (op read)       │
├─────────────────────────────────────────────────────────────┤
│ broker    HTTP-over-unix-socket daemon (Hermes VM host)     │
│           policy engine: allowlist repos, clamp permissions │
│           journald audit per mint                           │
├─────────────────────────────────────────────────────────────┤
│ client    git credential helper + gh wrapper + token CLI    │
│           backend = local core (devcontainer)               │
│                   | broker socket (Hermes containers)       │
│           holds NO key material in broker mode              │
└─────────────────────────────────────────────────────────────┘
```

### core: down-scoped minting

The mint call gains a request body:

```text
POST /app/installations/{installation_id}/access_tokens
{
  "repositories": ["igou-ansible"],
  "permissions": {"contents": "write"}
}
```

Configuration becomes multi-installation, keyed by owner:

```yaml
github_app:
  client_id: Iv1.xxxx
  app_slug: igou-dev
  installations:
    david-igou: "11111111"
    igou-io: "22222222"
  private_key_cmd: op read op://lab_external_api_keys/igou-dev-github-app/private_key
```

Key sources, in precedence order: `private_key` (inline env),
`private_key_path` (file), `private_key_cmd` (executed at mint time — the op
Connect read; key material exists only in process memory for the mint).
The owner half of `--repo OWNER/REPO` selects the installation.

### client: transparent git and gh

- `git-credential-ghapp` — a git credential helper. Configured with
  `useHttpPath = true` so the helper receives the repo path, derives
  `OWNER/REPO`, requests a token scoped to `{contents: write}` for that one
  repo, and answers `username=x-access-token` / `password=<token>`. Plain
  `git push` on an HTTPS remote works with a fresh 1-repo token every time.
  (SSH remotes are already a dead end in both environments.)
- `gh-app` (kept) / a `gh` shim — mints a token for the target repo with a
  default permission set (`contents: write`, `pull_requests: write`,
  `issues: write`) and execs `gh` with `GH_TOKEN`/`GITHUB_TOKEN` set.
- `ghapp token --repo OWNER/REPO --permission contents=write ...` — explicit
  mint for scripts; `--json` includes expiry and auth metadata.

Backend selection is one environment variable:

- `GHAPP_BROKER_SOCKET=/run/ghbroker/ghbroker.sock` → broker mode (Hermes
  terminal containers). The client never touches key config.
- unset → local mode (devcontainer): load core config and mint directly.

Both the devcontainer and the Hermes terminal backend run the same
`igou-devenv` image, so the client ships once in that image and behaves
correctly in both places purely via environment.

### broker: the trust boundary on the Hermes VM

A small HTTP-over-unix-domain-socket daemon (`ghapp broker serve`), run as a
**dedicated system user** so neither the Hermes gateway (agent brain, `hermes`
user) nor terminal containers can read the key:

- systemd system unit `hermes-github-broker.service` with `User=ghbroker`,
  `RuntimeDirectory=ghbroker`; socket `/run/ghbroker/ghbroker.sock`, mode
  `0660`, group `hermes`.
- Key at `/etc/ghbroker/igou-hermes.pem`, `0600 ghbroker:ghbroker`, delivered
  by the AAP provision playbook from 1Password. Not in any container mount.
- Terminal containers reach it via a socket bind mount — no TCP listener, no
  nftables/EgressFirewall changes, works under rootless podman + pasta where
  host loopback is unreachable:

```yaml
docker_volumes:
  - "/run/ghbroker/ghbroker.sock:/run/ghbroker/ghbroker.sock:Z"
```

  With `--userns=keep-id` the container user maps to `hermes`, which is in the
  socket's group.

API (single endpoint):

```
POST /token
{"repo": "igou-io/igou-ansible", "permissions": {"contents": "write"}}
→ 200 {"token": "ghs_…", "expires_at": "…", "repo": "…", "permissions": {…}}
→ 403 {"error": "repo not in policy"} | {"error": "permission exceeds policy"}
```

Policy file `/etc/ghbroker/policy.yaml` (root-owned, agent-immutable):

```yaml
policy:
  allowed_repos:
    - igou-io/igou-ansible
    - igou-io/igou-inventory
    - david-igou/hermes_github_app_plugin
  max_permissions:
    contents: write
    pull_requests: write
    issues: write
  default_permissions:
    contents: read
  token_ttl_note: GitHub fixes installation-token TTL at 1h; broker cannot shorten it,
    but every token is single-repo and permission-clamped.
```

Requests are **denied** (not clamped silently) when they exceed policy, with
the denial audited — silent clamping makes agent failures confusing.

Every decision emits a structured journald line
(`SYSLOG_IDENTIFIER=ghbroker`): repo, requested vs granted permissions,
verdict, token expiry, requesting PID/UID. This slots into the existing
journald audit + EDA alert pipeline.

Defense-in-depth layers, outermost first:

1. GitHub installation repo allowlist (hard boundary even against the broker).
2. App-level permission ceiling (the app only *has* contents/PRs/issues/etc.).
3. Broker policy (repo allowlist ∩ permission clamp, per mint).
4. Token properties (single repo, minimal perms, 1h, memory-only).

A leaked *token* is bounded by 3–4; a leaked *key* is bounded by 1–2; the
agent never sees the key because of the `ghbroker` user boundary.

## GitHub-side setup

| | `igou-dev` | `igou-hermes` |
|---|---|---|
| Installed on | david-igou + igou-io | david-igou + igou-io |
| Installation repos | broad (working repos) | narrow (Hermes-managed repos only) |
| Permissions | contents, pull_requests, issues, workflows RW; actions read; (optional) secrets RW to fix the PAT 403 on Actions secrets | contents, pull_requests, issues RW |
| Key storage | `op://lab_external_api_keys/igou-dev-github-app` | `op://awx/igou-hermes-github-app` (AAP delivers to VM) |

1Password item fields (as created): `client_id`, `app_slug`, `private_key`,
`installation_id_david-igou`, `installation_id_igou-io`.

Notes:

- Pushes made with app installation tokens **do** trigger Actions workflows
  (unlike `GITHUB_TOKEN` inside Actions) — CI keeps working.
- Rate limit is ≥5k req/h per installation — a non-issue here.
- Apps support two active keys, so rotation is: add key → update 1P →
  reprovision/re-`op read` → remove old key.

## Delivery: everything through Ansible, validated on test VMs first

No hand-edits to the live devcontainer host or the live `hermes` VM. All host
integration ships as Ansible (the `david_igou.devhost` collection + the
existing `igou-ansible` playbooks), and is validated end-to-end on disposable
KubeVirt test VMs on `ocp.igou.systems` — the devenv burst-VM pattern that is
already live (playbooks/devenv/* on the casval burst node, AAP workflow
`devenv-provision-e2e`) and a parallel test instance of the Hermes VM
lifecycle. Every new behavior is gated behind a default-off variable so the
shared playbooks stay safe for the live hosts until go-live.

### New role: `david_igou.devhost.ghapp`

One role in the devhost collection owns all host-side integration, alongside
`docker`/`packages`/`podman`. Modes via variables:

```yaml
# Client / local-mint mode (devenv hosts)
ghapp_install: true                    # pipx install from PyPI (or git ref pre-release)
ghapp_client_config:                   # -> ~/.config/ghapp/config.yaml for ghapp_user
  client_id: "{{ ghapp_client_id }}"
  app_slug: igou-dev
  installations: "{{ ghapp_installations }}"   # owner -> installation_id map
  private_key_path: "{{ ghapp_private_key_path | default(omit) }}"
  private_key_cmd: "{{ ghapp_private_key_cmd | default(omit) }}"
ghapp_configure_git_credential_helper: true    # gitconfig include: helper=ghapp, useHttpPath

# Broker mode (Hermes VM) — default false
ghapp_broker_enabled: false
ghapp_broker_user: ghbroker            # dedicated system user, owns the key
ghapp_broker_socket_group: hermes      # /run/ghbroker/ghbroker.sock 0660 root-managed unit
ghapp_broker_private_key: ""           # PEM content, delivered no_log from AAP (op://awx item)
ghapp_broker_policy: {}                # -> /etc/ghbroker/policy.yaml (allowed_repos, max_permissions)
```

The role gets a molecule scenario in the collection's existing
`extensions/molecule/` layout: client-mode config rendering + credential-helper
wiring, and broker-mode unit/policy/socket with a mocked key (no live GitHub
calls in molecule; the live-mint path is covered by the VM e2e below).

### Test topology on OpenShift

**devenv test VM** (exists today): `playbooks/devenv/provision-vm.yml` →
`playbooks/devenv/bootstrap.yml` via AAP workflow `devenv-provision-e2e`.
`bootstrap.yml` adds a gated role call:

```yaml
- role: david_igou.devhost.ghapp
  when: devenv_ghapp_enabled | default(false) | bool
```

For the test the key is delivered as an AAP-injected variable (written to
`~igou/.config/ghapp/key.pem`, `0600`, `no_log`) since the test VM has no op
Connect session; `private_key_cmd`/`op read` is the devcontainer go-live
variant of the same config. Validation on the VM (and inside an `igou-devenv`
container on it, mounting `~/.config/ghapp` ro to mirror the real
devcontainer): clone over HTTPS, `git push` via the credential helper,
`gh-app pr create`, one repo per org to prove installation selection, and a
negative probe against a repo outside the installation.

**hermes test VM**: same playbook chain as the live agent with overrides —
`provision-vm.yml` with `vm_name`/`vm_namespace` set to `hermes-test`, then
`setup-os.yml` → `setup-hermes.yml` → lock egress → `configure.yml`, all
targeted via `ansible_limit=hermes-test-hermes-test` so the live
`hermes-hermes` host is never in scope. Gated additions
(`hermes_ghapp_broker_enabled`, default **false**):

- `setup-hermes.yml`: call `david_igou.devhost.ghapp` with
  `ghapp_broker_enabled: true` (creates `ghbroker` user, installs the package,
  renders policy + system unit, key from the AAP-supplied variable backed by
  `op://awx/igou-hermes-github-app`).
- `configure.yml` terminal config deltas, same gate:
  - add `/run/ghbroker/ghbroker.sock` to `docker_volumes`
  - add `GHAPP_BROKER_SOCKET` to `docker_env`
  - remove `GH_TOKEN` from `docker_forward_env`
  - drop the `~/.config/gh` mount (and `~/.ssh` once nothing else needs it)
- Test-VM policy allowlists only scratch repos.

Validation from an agent terminal container on the test VM: `git push` via
helper (token minted through the socket), key unreadable from the container
and from the `hermes` user, out-of-policy repo/permission denied with the
denial visible in `journalctl SYSLOG_IDENTIFIER=ghbroker`, gateway
`github_app_*` tools working socket-backed.

### AAP mechanics (per the devenv burst-VM session)

- Merge order: fork release/tag → devhost collection PR →
  `igou-awx-ee` rebuild (the EE ships the devhost collection) →
  igou-ansible/igou-inventory PRs → refresh the `igou_inventory_github`
  inventory source (id 10) → `aap_sync_templates` (JT 13) → launch.
- igou-inventory: JT extra_vars/survey entries for the gates + a
  `hermes-test` provision/converge workflow (mirroring `devenv-provision-e2e`).
- Burst gotcha #262: the devenv VM needs a manual
  `oc scale machineset.cluster.x-k8s.io/casval-worker -n openshift-cluster-api --replicas=1`
  before provisioning (autoscaler scale-from-zero for KubeVirt VMs is broken).
- Shared checkouts: all PR branches built in git worktrees off `origin/main`,
  staging only these files.

### Repo change map

| Repo | Change |
|---|---|
| `hermes_github_app_plugin` (fork) | package rework: core down-scoping + multi-installation, client (credential helper, `gh` wrapper, backend selection), broker `serve` + policy + journald audit; installable from a git ref for testing |
| `ansible-collection-devhost` | new `ghapp` role + molecule scenario |
| `igou-ansible` | `playbooks/devenv/bootstrap.yml` gated role call; `playbooks/hermes/setup-hermes.yml` + `configure.yml` gated broker/terminal wiring |
| `igou-inventory` | JT/workflow + extra_vars for the gates and the `hermes-test` chain |

## Rollout

1. Rework this fork (tests for policy clamp/deny, helper protocol, multi-
   installation selection).
2. Create both GitHub Apps; **install them only on scratch/test repos
   initially** (the installation allowlist is the outermost boundary, so a
   test-phase leak is bounded to scratch repos). Keys into 1Password.
3. devhost `ghapp` role + molecule green.
4. igou-ansible / igou-inventory changes, all default-off; EE rebuild + AAP
   sync.
5. e2e on the devenv test VM (local-mint mode), then the hermes test VM
   (broker mode), including the negative/audit cases above.
6. Go-live — deliberately deferred, each a small flag-flip or env change once
   the test VMs are green:
   - live Hermes VM: set `hermes_ghapp_broker_enabled=true`, reconverge,
     migrate skills/prompts, drop the `GH_TOKEN` forward and gh/ssh mounts.
   - this devcontainer/host: adopt via `igou-devenv` (package in image,
     `envs/github-app-dev.env` with
     `GHAPP_PRIVATE_KEY_CMD=op read op://lab_external_api_keys/igou-dev-github-app/private_key`,
     gitconfig helper) — **not before the test phase concludes**.
   - expand the app installations from scratch repos to the real repo lists;
     revoke the static PATs last.

## Out of scope (deliberately)

- Central in-cluster broker — revisit if a third consumer appears; the core/
  broker split makes `serve` reusable over TCP+mTLS later without client
  changes.
- `op://awx/github/token` and other automation PATs — separate migration.
- Fine-grained *user* PATs as an alternative — they can't be minted at
  runtime and don't give per-agent bot identity.
