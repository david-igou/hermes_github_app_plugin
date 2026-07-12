# GitHub App setup: igou-dev and igou-hermes

One-time manual setup (GitHub UI + 1Password). Companion to
[design-minimal-runtime-tokens.md](design-minimal-runtime-tokens.md).

Both apps follow the same recipe; they differ only in name, permissions, and
where the key lands in 1Password.

## 1. Register the apps

Register under the `igou-io` org so both agent identities are administered in
one place: <https://github.com/organizations/igou-io/settings/apps/new>
(registering under `david-igou` works identically via Settings → Developer
settings → GitHub Apps).

Per app:

| Field | `igou-dev` | `igou-hermes` |
|---|---|---|
| GitHub App name | `igou-dev` (bot appears as `igou-dev[bot]`) | `igou-hermes` |
| Homepage URL | `https://github.com/david-igou/hermes_github_app_plugin` | same |
| Webhook | **uncheck Active** (no URL needed) | same |
| Where can this app be installed? | **Any account** — required to install on both `david-igou` and `igou-io` | same |

Leave Callback URL, setup URL, and OAuth options empty — the apps are never
user-facing.

### Repository permissions

| Permission | `igou-dev` | `igou-hermes` | Why |
|---|---|---|---|
| Contents | Read and write | Read and write | clone/push |
| Pull requests | Read and write | Read and write | create/comment/merge PRs |
| Issues | Read and write | Read and write | issue workflows |
| Metadata | Read (forced) | Read (forced) | automatic |
| Workflows | Read and write | — | push commits touching `.github/workflows/` (Contents alone is rejected) |
| Actions | Read | — | view workflow runs (`gh run list/view`) |
| Checks | Read | Read | `gh pr checks` |
| Commit statuses | Read | Read | status on PRs |
| Secrets | Read and write (optional) | — | fixes the PAT 403 on Actions secrets; grant only if wanted |

Organization permissions: none. Account permissions: none.

## 2. Collect identifiers and keys

After **Create GitHub App**, from the app's settings page record:

- **Client ID** (`Iv1.…`) — used as the JWT `iss` claim.
- **App slug** (lowercased name, e.g. `igou-dev`).

Then **Generate a private key** (bottom of the settings page). It downloads a
`.pem`; GitHub keeps no copy. An app supports two active keys, which is what
makes later rotation zero-downtime (add new → roll out → delete old).

## 3. Install on both accounts (scratch repos only, for now)

From the app settings → **Install App**:

1. Install on `igou-io` → **Only select repositories** → the scratch/test
   repo(s) only (e.g. `igou-io/ghapp-test`; create it if needed).
2. Install on `david-igou` → **Only select repositories** → scratch/test
   repo(s) only (e.g. `david-igou/hermes_github_app_plugin` is a reasonable
   test target for `igou-dev`).

The installation repo list is the outermost security boundary — it stays
scratch-only until the test phase concludes (design doc, Rollout step 6).

Record each **installation ID**: it's the trailing number in the URL when
viewing the installation —
`https://github.com/organizations/igou-io/settings/installations/<ID>` and
`https://github.com/settings/installations/<ID>`. (Also listable later via
`GET /app/installations` with an app JWT.)

Four installations total: `igou-dev` × {david-igou, igou-io} and
`igou-hermes` × {david-igou, igou-io}.

## 4. Store in 1Password

> The devcontainer's `op` runs in Connect mode and **cannot create items** —
> create these from the 1Password desktop app or an authenticated host CLI.

Two items, same field layout:

| Item | Vault |
|---|---|
| `igou-dev-github-app` | `lab_external_api_keys` |
| `igou-hermes-github-app` | `awx` (AAP delivers it to the Hermes VM) |

Fields (as created — underscore labels, so `op read
op://lab_external_api_keys/igou-dev-github-app/private_key` works):

```text
client_id                    Iv23…
app_slug                     igou-dev | igou-hermes
private_key                  <full PEM contents, BEGIN/END lines included>
installation_id_david-igou   <number>
installation_id_igou-io      <number>
```

## Status: DONE and verified (2026-07-02)

Both apps created, installed, stored, and validated end-to-end from the
devcontainer (JWT → `GET /app` → down-scoped mints → token use → negative
probes; all passed):

| | `igou-dev` | `igou-hermes` |
|---|---|---|
| client_id | `Iv23liP6aQEsyKXnZYiB` | `Iv23li8tQAbO2YZrIl7W` |
| installation david-igou | 143866260 | 143866643 |
| installation igou-io | 143866153 | 143866709 |
| test repos (selected) | david-igou/hermes_github_app_plugin, igou-io/igou-infrastructure | same |
| granted perms | contents/issues/PRs/actions/statuses/secrets/workflows write, checks read | contents/issues/PRs write; actions/checks/statuses read |

## 5. Verify a mint (before any code exists)

From any machine with the PEM (do this outside agent-controlled environments;
`PyJWT` + `httpx`/`curl`):

```bash
python3 - "$CLIENT_ID" /path/to/key.pem <<'EOF'
import sys, time, jwt
cid, key = sys.argv[1], open(sys.argv[2]).read()
now = int(time.time())
print(jwt.encode({"iat": now - 60, "exp": now + 540, "iss": cid}, key, algorithm="RS256"))
EOF
```

```bash
curl -s -X POST \
  -H "Authorization: Bearer $APP_JWT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens \
  -d '{"repositories": ["ghapp-test"], "permissions": {"contents": "read"}}'
```

Expect `201` with a `ghs_…` token whose `repositories` array lists exactly the
one repo. Two useful negative probes: request a repo not in the installation
(`422`), and request a permission the app lacks (`422`).

## Rotation

1. App settings → generate a second private key.
2. Update the 1Password item's `private key` field.
3. Reconverge consumers (devcontainer picks it up on next `op read`; Hermes
   VM via AAP reconverge).
4. Delete the old key in app settings.

## Deferred to go-live

- Expanding both installations from scratch repos to the real repo lists
  (Hermes narrower than dev).
- Revoking the static PATs (`claude-david-igou-github-token`,
  `claude-igou-io-github-token`) — last step, after both environments are
  validated.
