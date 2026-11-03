# Install guide for IT

**สรุปสำหรับฝ่าย IT (ภาษาไทย):** ระบบนี้คือ Docker image หนึ่งตัวกับฐานข้อมูล PostgreSQL หนึ่งตัว
ติดตั้งบนเซิร์ฟเวอร์ของบริษัทหรือคลาวด์ใดก็ได้ ต้องมี HTTPS (เช่น hr.ytrc.co.th ผ่าน Cloudflare หรือ nginx),
Google OAuth client ของ Google Workspace บริษัท, และสำรองข้อมูลทุกวัน ขั้นตอนครบอยู่ในข้อ 1-8,
ตัวอย่าง reverse proxy ในข้อ 10 และลำดับงานวันเปิดใช้ระบบในข้อ 11 ข้อมูลเงินเดือนและบัญชีธนาคารไม่อยู่ในระบบนี้ (อยู่ใน Bplus เท่านั้น)

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

## 9. Settings that matter on day one

| Variable | Why |
|---|---|
| `APP_ENV=production` | Turns on secure cookies and refuses the default `SECRET_KEY` |
| `BASE_URL` | Used in Google sign-in and links; must be the HTTPS address people open |
| `DEMO_MODE=false` | Never load SAMPLE people on the company server |
| `STORAGE_DIR` | Scans of personnel documents, policies, CVs and form photos. Mount a volume and back it up with the database. Only the app user reads it. |
| `AI_PROVIDER` | `none` until the company has agreed a provider (see `ai.md`). Everything works without AI; only the photo reading is switched off. |
| `LOCAL_LOGIN_ENABLED` | `true` while operators and supervisors use app-only logins |

## 10. HTTPS in front of the app

The container listens on port 8000 (plain HTTP). Put it behind a proxy that
holds the certificate and passes the original host and scheme.

**Caddy** (gets a certificate by itself):

```
hr.ytrc.co.th {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 20MB
    }
}
```

**nginx**:

```
server {
    listen 443 ssl http2;
    server_name hr.ytrc.co.th;
    ssl_certificate     /etc/ssl/hr.crt;
    ssl_certificate_key /etc/ssl/hr.key;
    client_max_body_size 20m;          # photos of forms, PDF policies, Excel imports
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

**Cloudflare** (the company website already uses it): add a DNS record for the
HR address pointing at the server, proxied, SSL mode *Full (strict)* with an
origin certificate on the server. Do not cache anything under this address
(add a Cache Rule "bypass" for the hostname): every page is personal.

Change `ports: "8000:8000"` in `docker-compose.yml` to `"127.0.0.1:8000:8000"`
so the app is reachable only through the proxy.

## 11. Go-live day, in order

Each step takes minutes; the whole day is mostly HR checking data.

1. **IT**: install as in section 3 with `DEMO_MODE=false`. Create the first admin
   with `create-admin`, sign in, change the temporary password.
2. **IT**: Admin > Users: create the HR Manager (role `hr_manager`) and HR
   officers (`hr`). Link each user to their Employee ID after step 4.
3. **HR Manager**: Admin > Holidays: upload the company calendar workbook for
   this year (and next year in December).
4. **HR Manager**: Admin > Go-live loading > Employee Master: upload the
   `Employee_Master_v1` workbook. The app shows every row as New, Update,
   No change or Error without saving. Fix errors in the workbook, upload
   again, then press Load. Department, job, grade and group differences for
   people already in the app are listed, not changed.
5. **HR Manager**: Org & positions: set the Department Head of each
   department, check the budgeted positions and seats, put each active person
   on a position (the go-live checklist counts who is missing).
6. **HR**: Admin > Go-live loading > Opening leave balances: download the sheet
   (one row per active person and leave type), fill in the days already taken
   this year on paper and the vacation days carried in, upload, check, load.
7. **IT**: Admin > Users: create logins for Department Heads, supervisors and
   anyone else (Google sign-in for people with a company account, app-only
   logins for the others), with their roles and Employee ID.
8. **HR Manager**: Admin > Settings: company name for letters
   (`company_name_th`), and review the rule settings.
9. **Everyone**: the go-live checklist on Admin > Go-live loading should be all
   OK. Then announce the address.

From that day on, the Excel master is no longer edited: changes go through the
app (data change with a second HR check, job events with approvals).

If something goes wrong on the day, restore the database backup taken before
step 4 and start again from step 4; nothing outside the app has changed.

## 12. Monitoring

- `GET /healthz` from the monitoring tool every minute.
- `docker compose logs app --since 24h` for errors.
- Disk space on the `files` and `dbdata` volumes (about 1 GB a year for 300 people with scans).
- The nightly `run-jobs` output (number of events applied, data health counts).

## 13. Open questions for IT

- Where it will run (company server or which cloud) and who looks after it.
- The HTTPS address.
- Who creates the Google OAuth client.
- The exact Bplus 7.4 import layout (column order and pay codes are set in
  Admin > Settings, no code change needed).
- How face scan files get from the machine to the app.
- Which AI provider the company allows for reading paper forms (see `ai.md`).
