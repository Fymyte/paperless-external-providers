# paperless-sources

Pulls documents from external services and uploads them to Paperless-ngx with
the REST API. It runs as a separate container next to Paperless-ngx and needs
no change to it.

Supported providers:

- `edocperso`: [eDocPerso](https://edocperso.fr), a French digital safe used
  for payslips. It has no public API. The provider uses the endpoints the web
  app calls, so it can break when eDocPerso changes them.

## How It Works

Every `poll_interval` seconds, for each source:

1. Ask Paperless-ngx for the result of the uploads still in progress.
2. Log in to the provider and list its documents.
3. Skip documents already recorded in the state database.
4. Download each new document and compute its SHA-256.
5. If Paperless-ngx already has a document with that checksum, record it and
   skip the upload.
6. Otherwise upload it with `POST /api/documents/post_document/` and record
   the task ID.

The state database (`/data/state.sqlite3`) is keyed on the provider's
document ID. A document deleted in Paperless-ngx is not uploaded again.
The checksum query covers a lost state database and documents that were
already in Paperless-ngx before the first run. It only sees documents the
token's user can view, and not the trash.

A failed upload or consume task is retried on the next cycles, up to
`max_attempts`. Paperless-ngx reports a rejected duplicate as a failed task.
This tool records it as done.

## Setup

1. Create an API token in Paperless-ngx (My Profile, API Auth Token). The
   uploaded documents are owned by that user.
2. Create the tags, correspondent and document type you want to assign.
3. Copy `config.example.toml` to `config.toml` and edit it.
4. Put the secrets in files:

   ```console
   $ mkdir -p secrets
   $ read -rs V && printf '%s' "$V" > secrets/paperless_token
   $ read -rs V && printf '%s' "$V" > secrets/edocperso_password
   $ unset V && chmod 600 secrets/*
   $ mkdir -p data
   $ printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" >> .env
   ```

   Run these in the directory of the Paperless-ngx compose file. The
   container runs as that UID and GID, so it can read the secrets and write
   `data/` without making them readable by other users. `read -rs` keeps the
   values out of the shell history.

5. Add the service from `compose.example.yml` to the Paperless-ngx compose
   file, then `docker compose up -d sources`.

Run one cycle by hand with `docker compose run --rm sources --once`.

`--list-only` logs in to each source and logs what the next cycle would do
with each document (`new`, `queued`, `done`, `retry`, `gave-up`). It downloads
nothing and does not call Paperless-ngx. Use it before the first real run.

## Published Image

CI publishes `ghcr.io/fymyte/paperless-external-providers` for `linux/amd64`
and `linux/arm64`:

- `:main` and `:sha-<commit>` on every push to `main`
- `:X.Y.Z`, `:X.Y` and `:latest` on a `vX.Y.Z` tag

To use it instead of a local build, replace `build: .` with
`image: ghcr.io/fymyte/paperless-external-providers:latest` in the compose
file. Each image has a build provenance attestation. Check that an image
was built by this repository's CI with:

```console
$ gh attestation verify oci://ghcr.io/fymyte/paperless-external-providers:latest \
    --owner Fymyte
```

## Configuration

Secrets are never read from `config.toml`. For a source named `payslips`, a
provider field `password` comes from `SOURCE_PAYSLIPS_PASSWORD` or from the
file named by `SOURCE_PAYSLIPS_PASSWORD_FILE`. Any other field can be set the
same way, and the environment wins over the file.

| Variable | Use |
| --- | --- |
| `PAPERLESS_TOKEN`, `PAPERLESS_TOKEN_FILE` | API token, required |
| `PAPERLESS_URL` | Overrides `paperless.url` |
| `PAPERLESS_SOURCES_CONFIG` | Config path, default `/config/config.toml` |
| `PAPERLESS_SOURCES_LOG_LEVEL` | Default `INFO` |

`[sources.upload]` takes `tags`, `correspondent`, `document_type`,
`storage_path` and `custom_fields`, as names or IDs. Names are resolved at
each cycle and must exist in Paperless-ngx.

## Security

The Paperless-ngx token has every right of its user. Do not use a token of
an admin account. If the secret file leaks, the whole archive does.

Create a dedicated user instead, such as `sources`, with only these
permissions: add documents, and view tags, correspondents, document types
and storage paths. Give it view permission on the objects that
`[sources.upload]` names, because name lookup only finds what the token's
user can see. A leaked token then lets someone add documents, but not read
yours.

That user owns what it uploads. To give each document to its real owner,
add a marker tag per person to the source and let a workflow move it:

1. Create a tag such as `owner:pierrick` and let `sources` view it.
2. Add it to the source: `tags = ["Payslip", "owner:pierrick"]`.
3. Create a workflow with the trigger "Document Added", filtered on
   "has any of these tags: `owner:pierrick`", and an assignment action
   that sets you as owner. A "Consumption Started" trigger does not work
   here, because it cannot filter on tags.

One marker tag and one workflow per person give several users their own
documents with a single token.

Once a document belongs to you, `sources` cannot see it, so the checksum
check no longer finds it. Dedupe then relies on the state file alone. Back
up `data/`, or set `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` in
Paperless-ngx so it rejects exact copies itself. Do not give `sources`
view permission on the imported documents to keep the checksum check. That
would let a leaked token read them.

Other rules:

- In Docker, use `PAPERLESS_TOKEN_FILE` and `SOURCE_*_FILE`, not the plain
  variables. Anyone who can run `docker inspect` or read
  `/proc/<pid>/environ` sees plain variables.
- Reach Paperless-ngx over HTTPS, or over the internal Docker network as in
  `compose.example.yml`. Over plain HTTP the token travels in clear text.
- Providers run in the same process as the token. Install only providers
  you trust.
- `compose.example.yml` runs the container read-only, without Linux
  capabilities and without privilege escalation. It writes only to `/data`
  and to a tmpfs on `/tmp`.

## Writing a Provider

A provider is a class that matches `SourceProvider` in
`src/paperless_sources/providers/__init__.py`. Register it under the
`paperless_sources.providers` entry point group. The entry point name is the
`provider` value in the config. A provider only talks to its remote service
and does not import anything from Paperless-ngx.

## Development

```console
$ uv sync --group dev
$ uv run pytest
$ uv run ruff check && uv run ruff format --check
```
