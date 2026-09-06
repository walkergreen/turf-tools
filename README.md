# Turf Tools

Open source canvassing platform and mobile app.

The software in this repository includes three key components: (1) performant voter file data processing (including a fully open-source geocoding pipeline) (2) a canvassing application that runs natively on iOS and Android (3) a web platform for creating segments, scripts, and campaigns, and for cutting turf.

The project is in active development and currently undergoing early testing.

## Prerequisites

You'll need to have these installed to run the development environment.

- [Node.js](https://nodejs.org/) >= 22.12
- [pnpm](https://pnpm.io/) enabled via `corepack enable`
- [Docker](https://www.docker.com/) (for the development Postgres)
- [uv](https://docs.astral.sh/uv/) `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [Quickwit](https://quickwit.io/) `curl -L https://install.quickwit.io | sh` (move binary to `/usr/local/bin/`)
- [Xcode](https://developer.apple.com/xcode/) (optional, for iOS simulator)

## Setup

Once the above are installed, run this to install both Node and Python dependencies.

```bash
pnpm bootstrap
```

Then create local env files from the committed examples:

```bash
for d in apps/web apps/data apps/native packages/db; do cp $d/.env.example $d/.env; done
```

Two things to fill in: a [MapTiler](https://www.maptiler.com/) API key (`VITE_MAPTILER_KEY` in `apps/web/.env` and `EXPO_PUBLIC_MAPTILER_KEY` in `apps/native/.env`), and `AUTH_DISABLED=1` uncommented in `apps/web/.env` to skip magic-link auth during development. Everything else works with the defaults.

## Architecture

The overall structure is as follows:

```
apps/
  web/       TanStack Start (admin UI, oRPC API, system orchestrator)
  native/    Expo/React Native (mobile canvassing app)
  data/      FastAPI + DuckDB (voter file processing, geocoding, search indexing)

packages/
  db/        Drizzle schema + Postgres client
```

We leverage a two database design to get the best of both where needed:

- **Postgres** (via Drizzle) — operational data: users, campaigns, segments, zone groups, turfs, canvass results
- **DuckLake** (via DuckDB) — analytical columnar data: voter files, persons, buildings, doors

## Development

Start all services by calling:

```bash
pnpm dev
```

This starts:

- `db` — Postgres via Docker Compose (port 5432)
- `web` — TanStack Start admin UI and oRPC API (port 3000)
- `native` — Expo web preview of the mobile app (port 8081)
- `data` — FastAPI data service (including DuckDB/DuckLake) (port 8000)
- `search` — Quickwit search engine (port 7280)

The first time you run `dev`, the web server automatically pushes the Drizzle schema and seeds reference data once Postgres is up.

To work with voter data, create a dataset in the admin UI and import it from a source URL.

You can also start individual services with the following commands:

```bash
pnpm dev:web
pnpm dev:data
pnpm dev:search
pnpm dev:native
pnpm dev:ios
```

The `dev:ios` command is required to build and connect to the native app for iOS, but once it's been built, if you just run `dev:native` it should automatically bundle and connect to the latest version.

## Testing

### Unit tests

Run the unit tests with:

```bash
pnpm test
```

And run the type checks and linters with:

```bash
pnpm check
```

## Service API

A token-authenticated API at `/api/service/*` lets DSA's Org Tools automation (zapctl, driven by the Zendesk VAN-request process) provision a chapter in Turf Tools once Compliance has approved its request: create the org, grant it a dataset, invite its admins, and create approved survey questions. It is server-to-server only and is never exposed in the admin UI.

### Tokens

Tokens are minted by an operator with database access and stored hashed (sha256) in `app.service_tokens`; the raw token is printed once. Each token acts as an "actor" user, so everything the API creates is attributed to a real `users` row (the actor has no org membership and cannot log in).

```bash
pnpm service-token:create --name zapctl-prod --actor-email automation@example.org
pnpm service-token:revoke --list
pnpm service-token:revoke --prefix tt_AbCdEfGhI    # or --name zapctl-prod
```

The `app.service_tokens` table and the provenance columns below reach a database through the usual `pnpm db:push`.

### Calling it

Send `Authorization: Bearer <token>` with a JSON body. A missing, unknown, or revoked token returns `401 {"error":"unauthorized"}`; handler errors are oRPC JSON (`code`, `status`, `message`), e.g. `404 NOT_FOUND` for an unknown org or dataset and `400 BAD_REQUEST` for invalid input.

| Method + path                            | Body                                                                                                                        | Returns                                                                          |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `GET /api/service/healthcheck`           | –                                                                                                                           | `status`, `db`, `token.name`                                                     |
| `POST /api/service/organizations/status` | `slug`                                                                                                                      | `found`, `provisioned` (has a grant), `organization`, `grants[]` with provenance |
| `POST /api/service/organizations/ensure` | `slug`, `name`                                                                                                              | `created`, `organization`; idempotent, never renames                             |
| `POST /api/service/datasets/list`        | `{}`                                                                                                                        | every dataset with `latestReadyVersionId`                                        |
| `POST /api/service/datasets/grant`       | `orgSlug`, `datasetSlug`, `approvalTicketId`, `approvedAt?`, `contributionReportedAt?`, `note?`, `activate?` (default true) | `created`, `datasetOrganizationId`, `activated`                                  |
| `POST /api/service/users/invite`         | `orgSlug`, `email`, `name?`, `role` (owner/admin/lead), `sendEmail?` (default true)                                         | `userId`, `membership` (created/reactivated/existing), `emailSent`               |
| `POST /api/service/questions/create`     | `orgSlug`, `name`, `text`, `responseType?`, `options[]`                                                                     | `created`, `questionId`, `optionIds[]`; idempotent on name                       |

`users/invite` writes the membership before it sends the sign-in email; if the send fails, the response is still `200` with `emailSent: false` (the failure is logged server-side), and repeating the call resends without changing the membership.

`approvalTicketId` must be the Zendesk ticket number (digits only): it is shown verbatim to the chapter's admins and, once recorded, is never overwritten. `approvedAt` and `contributionReportedAt` are ISO timestamps; a JSON `null` for either reads as not supplied, while numbers, booleans, and unparseable strings are rejected. A grant records where the approval came from on `app.dataset_organizations` (`approval_ticket_id`, `approved_at`, `contribution_reported_at`, `approval_note`, `granted_by_user_id`); a repeat grant fills only columns that are still null, so the first recorded approval stands. The Data page shows this provenance under the dataset name. With `activate`, an org that has no active dataset version yet is pointed at the dataset's newest ready version.

Every call logs one line, `{"event":"service.rpc","token":…,"procedure":…,"orgSlug":…}` — never emails or request bodies.

### Cloudflare Access

When the deployment sits behind Cloudflare Access, the caller also sends `CF-Access-Client-Id` / `CF-Access-Client-Secret` for an Access service token. Cloudflare validates those at the edge; Turf Tools passes them through untouched and authenticates only the bearer token.

## Database commands

These subcommands help manage data lifecycle during testing:

```bash
pnpm db:push     # push drizzle schema to the dev Postgres
pnpm db:mock     # populate Postgres with sample data
pnpm db:clear    # wipe the dev Postgres (drops the Docker volume)
pnpm data:clear  # wipe DuckLake + local turf blobs
pnpm clear       # wipe everything (Postgres + DuckLake + turf blobs)
```
