# Deploy guide for IT

The app is one Docker image plus one SQL database. It runs on a company
server, a virtual machine or any cloud that runs containers. Nothing ties it
to one provider.

## 1. What you need

| Item | Minimum |
|---|---|
| Server | Linux with Docker and Docker Compose, 2 CPU, 4 GB RAM, 40 GB disk (about 300 people) |
| Database | PostgreSQL 15+ (included in docker-compose). MySQL 8, SQL Server 2019+ also work via `DATABASE_URL`. |
| Address | An internal or public HTTPS name, for example `hr.ytrc.co.th`, kept separate from the company website |
| HTTPS | A reverse proxy in front (nginx, Caddy, or the cloud load balancer) with a certificate |
| Google Workspace | Admin access to create an OAuth client |
| Face scan PC | A way to copy `timelog_*.txt` files (upload in the app; automatic pickup can be added) |

## 2. Google sign-in

1. Google Cloud console, project owned by the company > APIs & Services > OAuth consent screen: type **Internal**.
2. Credentials > Create OAuth client ID > Web application.
3. Authorised redirect URI: `https://<your address>/auth/google/callback`.
4. Put the client ID and secret in `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).
5. `GOOGLE_ALLOWED_DOMAINS=ytrc.co.th` keeps everyone else out.

Operators and supervisors without a Google account get an app-only login
(Admin > Users). Passwords are hashed with scrypt; 5 wrong tries lock the
login for 15 minutes.

## 3. Install

```bash
git clone <repo> hr-hub && cd hr-hub
cp .env.example .env
# set SECRET_KEY, DB_PASSWORD, BASE_URL, Google values, AI values
docker compose up -d --build
docker compose exec app python -m app.cli create-admin it.admin
```

On start the container upgrades the database (`migrate`) and loads the
reference data (org units, shifts, calendars, grades). It never loads SAMPLE
people unless `DEMO_MODE=true`.

The app refuses to start in production with the default secret key.

Behind a company proxy that inspects HTTPS, build with the company
certificate: `docker build --secret id=ca,src=company-ca.crt .`

## 4. Nightly job

Run once a night (cron, or the cloud scheduler):

```bash
docker compose exec -T app python -m app.cli run-jobs
```

It applies job events whose date has come and refreshes the data health checks.

## 5. Back up

| What | How | How often |
|---|---|---|
| Database | `docker compose exec -T db pg_dump -U hrhub hrhub \| gzip > hrhub-$(date +%F).sql.gz` | Daily, keep 30 days, copy off the server |
| Form photos | The `files` volume (`/data/files`) | Daily with the database |
| `.env` | Keep in the company password vault | When changed |

Test a restore every quarter on a spare machine.

## 6. Update

```bash
git pull
docker compose up -d --build
```

Database changes run by themselves on start (Alembic migrations in
`migrations/versions`). Back up first.

## 7. Other ways to host

- **Google Cloud Run + Cloud SQL (PostgreSQL)**: push the image, set the same
  environment variables, mount a Cloud Storage bucket at `/data/files`.
- **Any VM**: the docker-compose file as above.
- **Without Docker**: Python 3.13, `pip install -r requirements.txt`, run
  `python -m app.cli migrate` then `uvicorn app.main:app --host 0.0.0.0 --port 8000`
  behind nginx.

## 8. Health check

`GET /healthz` returns `ok`. Use it for the load balancer and monitoring.

## 9. Open questions for IT

- Where it will run (company server or which cloud) and who looks after it.
- The HTTPS address.
- Who creates the Google OAuth client.
- The exact Bplus 7.4 import layout (column order and pay codes are set in
  Admin > Settings, no code change needed).
- How face scan files get from the machine to the app.
- Which AI provider the company allows for reading paper forms (see `ai.md`).
