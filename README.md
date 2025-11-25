# ✨ Overview

This repository contains a **payment webhook processor** application that integrates payment providers with fiscal systems.
Currently it supports **Stripe → FINA** flow, but it is designed to support multiple payment providers and fiscal systems in the future.

**Current Implementation:** Stripe payments → Croatian FINA fiscalization.
As defined by Croatian fiscalization law, it is mandatory to issue fiscal receipts when the payment is made through Payment Cards including Stripe.

**Important:** FINA fiscalization only supports **EUR currency**. Payments in other currencies will be rejected.

When a payment is made through Stripe, this service (`payment-hook`) performs the following steps:

1. Receives a webhook from Stripe (`payment_intent.succeeded` event)
2. Stores the webhook data in S3-compatible storage (organized by timestamp and event ID)
3. Issues a fiscal receipt via FINA:
   - Generates ZKI (protective code)
   - Creates and signs XML
   - Sends to FINA endpoint
4. Stores the result:
   - Saves signed request and response files to S3 storage
   - Saves fiscal receipt data in database
   - All files for one transaction are organized in a single S3 folder

# 📚 References

- Fina Demo Certificates: https://www.fina.hr/finadigicert/certifikati-za-testiranje-i-demonstraciju/fina-demo-ca-certifikati
- Fina Production Certificates https://www.fina.hr/eng/finadigicert/ca-certificates
- Stripe Test Api Keys https://dashboard.stripe.com/test/apikeys
- Stripe Live Api Keys https://dashboard.stripe.com/apikeys
- Another Open Source related project https://github.com/senko/fiskal-hr and their [Integration Documentation](https://github.com/senko/fiskal-hr/blob/main/doc/integration.md) which helped a lot during development

# 📁 Project Structure

Apart standard docker python project files:

```
payment-hook/
├── app.py            # Flask app that receives webhooks
├── fina.py           # FINA fiscalization logic
├── s3_storage.py     # S3-compatible storage module
├── migrate.py        # Database migration system
├── .env              # Secrets (Stripe, FINA, S3 settings) - should be added during deployment
├── cert/             # FINA certificate files - should be added during deployment
└── migrations/       # SQL migration files
```

# ⚖ Requirements

- Docker
- FINA demo or production certificate and settings for registered account
- Stripe account
- S3-compatible storage (e.g., Hetzner Object Storage, AWS S3, MinIO)

# ⚙ Configuration

This docker compose configuration is designed for local development and testing only. It doesn't contain SSL termination etc.

Production deployment is intended to use docker image built from this repository but with separate ingress and database services, without stripe-cli service and docker compose.

- Add `cert/` to the project root with FINA certificate files:
  - `cert/1111111.1.F1.p12` - your client certificate.
  - `cert/ca_demo/demo2014_root_ca.pem` - download from https://www.fina.hr/finadigicert/certifikati-za-testiranje-i-demonstraciju/fina-demo-ca-certifikati
  - `cert/ca_demo/demo2014_sub_ca.pem` - download from https://www.fina.hr/finadigicert/certifikati-za-testiranje-i-demonstraciju/fina-demo-ca-certifikati
  - `cert/ca_demo/demo2020_sub_ca.pem` - download from https://www.fina.hr/finadigicert/certifikati-za-testiranje-i-demonstraciju/fina-demo-ca-certifikati
  - `cert/ca/FinaRootCA.pem` - download from https://www.fina.hr/finadigicert/fina-ca-root-certifikati
  - `cert/ca/FinaRDCCA2015.pem` - download from https://www.fina.hr/finadigicert/fina-ca-root-certifikati
  - `cert/ca/FinaRDCCA2020.pem` - download from https://www.fina.hr/finadigicert/fina-ca-root-certifikati
  - `cert/ca/FinaRDCCA2025.pem` - download from https://www.fina.hr/finadigicert/fina-ca-root-certifikati
  - `cert/ca/cis.porezna-uprava.hr.pem` - download `cis.porezna-uprava.hr.cer` from https://porezna-uprava.gov.hr/hr/certifikati-za-preuzimanje/4549, rename to `.pem`
  - `cert/ca/fiskalcis.pem` - download `fiskalcis.cer` from https://porezna-uprava.gov.hr/hr/certifikati-za-preuzimanje/4549, rename to `.pem`, remove extra header before `-----BEGIN CERTIFICATE-----`
- Create a `.env` file in the project root, use `.env.example` as a template.
- Configure S3-compatible storage credentials:
  ```
  S3_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxx
  S3_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  S3_ENDPOINT_URL=https://hel1.your-objectstorage.com
  S3_BUCKET_NAME=my-bucket-name
  ```

# 🚀 Running the Webhook Locally

## Start Containers

```bash
docker compose up --pull always --build
```

## Find Webhook Signing Secret

In the `docker compose up ...` command output, look for the webhook signing secret from the Stripe CLI:

```
stripe-cli-1 | Your webhook signing secret is whsec_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Add this secret to your `.env` file:

```
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Then stop the containers issuing Ctrl+C.

Then start the containers again to apply the env var changes:

```bash
docker compose up --pull always --build # --pull always --build is not needed on this specific restart, just to use history command
```

## Trigger Test Event

**Note:** FINA only accepts EUR currency. Use the `--override` flags to test with EUR:

```bash
docker compose exec stripe-cli sh -c 'stripe trigger payment_intent.succeeded --override payment_intent:currency=eur --override payment_intent:amount=1234 --api-key $STRIPE_API_SECRET_KEY'
```

**Stripe amount format:** Stripe API uses the currency's minor unit (cents for EUR). The amount `1234` represents 12.34 EUR (1234 cents). The application automatically converts cents to the major unit (EUR) for FINA fiscalization.

Without the currency override, the default USD payment will be rejected with error: "FINA fiscalization only supports EUR currency".

## Verify the Result

Check the database for the stored event and the fiscal receipt.

```bash
docker compose exec pg bash -c "echo 'SELECT * FROM fina_receipt ORDER BY id DESC LIMIT 5;' | psql -U paymenthook"
```

Database table `fina_receipt` should contain a new record with ZKI and JIR values.

`payment_time`, `receipt_created`, `receipt_updated` are of `timestamptz` type, that means they are stored in UTC timezone.

Also verify that files are stored in your S3 bucket with the structure:

```
YYYY-MM-DD-HH-MM-SS-stripe-payment-intent-{event_id}-{hostname}-{pid}/
├── stripe-webhook.json     # Raw Stripe webhook payload
├── stripe-webhook.yaml     # Parsed webhook data
├── fina-request.xml        # SOAP request sent to FINA
├── fina-request.yaml       # Parsed request data
├── fina-response.xml       # SOAP response from FINA
└── fina-response.yaml      # Parsed response data
```

Timestamps in those files should be in your configured timezone (e.g., Europe/Zagreb), as Fina requires Croatian local time for fiscal receipts.

**Folder naming includes:**
- UTC timestamp for chronological ordering
- Event ID for traceability to Stripe event
- Hostname to distinguish between environments (dev/production/staging)
- Process ID to handle multiple workers/containers

This prevents conflicts when the same Stripe event is sent to multiple environments (e.g., via stripe-cli to local dev and webhook to production simultaneously).

# 🛠️ CLI Tools for Manual Operations

The `fina_cli.py` tool provides manual control over FINA fiscalization operations:

## Retry Failed Receipt

If fiscalization fails (e.g., FINA service unavailable), you can retry it manually:

```bash
docker compose exec payment-hook python fina_cli.py --retry-receipt 123
```

This will:
- Fetch the failed receipt from the database (by receipt number)
- Use the original payment time and amount
- Retry the fiscalization process
- Update the database with the new result (ZKI/JIR)

**When to use:**
- FINA service was temporarily unavailable
- Network issues during initial fiscalization
- Certificate or configuration errors that have been fixed
- Any receipt with `status='failed'` or `status='processing'`

## Create Manual Receipt

Create a fiscal receipt manually without a Stripe webhook:

```bash
# Simple - with current time
docker compose exec payment-hook python fina_cli.py --create-receipt --amount 100.00

# With specific payment time
docker compose exec payment-hook python fina_cli.py --create-receipt --amount 150.50 \
    --payment-time "2025-01-15 14:30:00" --order-id "order_123"

# With custom identifiers
docker compose exec payment-hook python fina_cli.py --create-receipt --amount 200.00 \
    --stripe-id "pi_custom123" --order-id "order_456"
```

**Arguments:**
- `--amount` (required): Payment amount in EUR
- `--payment-time` (optional): Payment timestamp in format "YYYY-MM-DD HH:MM:SS" (defaults to current time in FINA_TIMEZONE)
- `--order-id` (optional): Order/invoice identifier
- `--stripe-id` (optional): Payment ID (auto-generated as `manual_<uuid>` if not provided)

**When to use:**
- Manual payment received (bank transfer, cash, etc.) that needs fiscalization
- Fixing missing fiscal receipts for completed payments
- Testing FINA integration without triggering Stripe webhooks
- Migrating historical receipts into the system

**Important notes:**
- Currency is always EUR (FINA requirement)
- Receipt number is auto-assigned using the same sequence as webhook processing
- Payment time without timezone is assumed to be in FINA_TIMEZONE
- All data is stored in database and S3 storage, same as webhook processing
- Files are stored in S3 with folder name: `YYYY-MM-DD-HH-MM-SS-fina-manual-{payment_id}-{hostname}-{pid}`

## View Help

```bash
docker compose exec payment-hook python fina_cli.py --help
```

## Test SSL Connection to FINA

Before deploying to production or when switching between demo/production FINA endpoints, you can verify that your CA certificates are correct:

```bash
# Test demo endpoint
docker compose exec payment-hook python test_ssl_connection.py \
    --ca-dir cert/ca_demo \
    --endpoint https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest

# Test production endpoint
docker compose exec payment-hook python test_ssl_connection.py \
    --ca-dir cert/ca_prod \
    --endpoint https://cis.porezna-uprava.hr:8449/FiskalizacijaService
```

**What it does:**
- Tests SSL handshake with the FINA endpoint
- Validates CA certificate chain
- Does NOT send any fiscal data (safe to test production)
- Does NOT require client certificate

**When to use:**
- Before deploying to production (verify CA certificates are correct)
- When switching from demo to production endpoint
- Troubleshooting SSL connection issues
- After updating CA certificates

**Expected output:**
```
✅ SSL handshake successful!
   Status code: 405
   Server responded: 232 bytes

🎉 SSL connection test PASSED
```

Status code 405 (Method Not Allowed) is expected - we're testing SSL, not sending actual requests.

**If it fails:**
- Check that all required CA certificates are present in the directory
- Verify certificates are in `.pem` format
- Ensure you're using the correct CA certificates for the endpoint (demo CA for demo endpoint, production CA for production endpoint)

# 🚢 Production/Test Deployment

This section describes how to deploy the application to a production or test environment (non-local development).

## Prerequisites

- External PostgreSQL database (PostgreSQL 17 or compatible)
- Nginx or similar reverse proxy with SSL/TLS termination
- Docker runtime environment
- S3-compatible storage bucket
- FINA certificate files
- Stripe account (test or production)

## Deployment Steps

### 1. Deploy PostgreSQL Database

Deploy an external PostgreSQL database. No special configuration is required - standard PostgreSQL setup is sufficient.

### 2. Configure Nginx Reverse Proxy

Create an nginx configuration file to proxy requests to the application container:

```nginx
upstream payment-hook {
    server localhost:8000;
}

server {
    listen 443 ssl http2;
    server_name payment-hook.example.com;

    # SSL certificate configuration
    ssl_certificate /path/to/fullchain.cer;
    ssl_certificate_key /path/to/key.key;
    include snippets/ssl-params.conf;

    root /var/www/payment-hook/static/public;
    charset UTF-8;
    autoindex off;

    location / {
        proxy_pass http://payment-hook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        # Preserve original request body for webhook validation
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
```

**Important nginx settings:**
- `proxy_buffering off` and `proxy_request_buffering off` preserve the original webhook payload for signature verification
- Timeouts should match or exceed `GUNICORN_TIMEOUT` environment variable

### 3. Build and Deploy Docker Image

Build the Docker image (typically done via CI/CD pipeline):

```bash
docker build -t payment-hook:latest .
```

Run the container with required environment variables and volume mounts:

```bash
docker run -d \
  --name payment-hook \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /path/to/cert:/app/cert:ro \
  -e APP_ENV=production \
  -e S3_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxx \
  -e S3_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  -e S3_ENDPOINT_URL=https://hel1.your-objectstorage.com \
  -e S3_BUCKET_NAME=my-bucket-name \
  -e P12_PATH=cert/1111111.1.F1.p12 \
  -e P12_PASSWORD=xxxxxxxxxxxxxxx \
  -e FINA_TIMEZONE=Europe/Zagreb \
  -e FINA_ENDPOINT=https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest \
  -e OIB_COMPANY=11111111111 \
  -e OIB_OPERATOR=11111111111 \
  -e LOCATION_ID=Online \
  -e REGISTER_ID=1 \
  -e STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxxx \
  -e PG_HOST=pg \
  -e PG_PORT=5432 \
  -e PG_USER=paymenthook \
  -e PG_PASSWORD=xxxxxxxxxxxxxxx \
  -e PG_DB=paymenthook \
  -e GUNICORN_WORKERS=2 \
  -e GUNICORN_TIMEOUT=60 \
  payment-hook:latest
```

**Volume mounts:**
- `/path/to/cert:/app/cert:ro` - Mount FINA certificate directory (read-only)

**Environment variables:**
- `APP_ENV` - Set to `production` or `development`/`dev`
- `FINA_ENDPOINT` - Use test endpoint for staging: `https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest`
- `FINA_ENDPOINT` - Use production endpoint for production: `https://cis.porezna-uprava.hr:8449/FiskalizacijaService`
- `STRIPE_WEBHOOK_SECRET` - Obtain from Stripe Dashboard (see next step)
- All other variables as documented in the Configuration section

See [the specification](doc/fina/Fiskalizacija - Tehnicka specifikacija za korisnike 2.5.pdf) for more details on FINA settings, endpoints, and certificate requirements.

### 4. Configure Stripe Webhook

1. Log in to [Stripe Dashboard](https://dashboard.stripe.com/)
2. Navigate to **Developers** → **Webhooks**
3. Click **Add endpoint**
4. Configure the webhook:
   - **Endpoint URL**: `https://payment-hook.example.com/stripe/payment-intent`
   - **Events to send**: Select `payment_intent.succeeded`
   - **API version**: Use latest or match your Stripe SDK version
5. After creating the webhook, copy the **Signing secret** (`whsec_...`)
6. Add the signing secret to your deployment as `STRIPE_WEBHOOK_SECRET` environment variable
7. Restart the container to apply the new secret

### 5. Connect Your E-commerce Platform

Connect your e-commerce platform (WordPress/WooCommerce, Shopify, custom app, etc.) to use your Stripe account:

- For test environment: Use Stripe test API keys
- For production environment: Use Stripe live API keys

The platform will process payments through Stripe, and Stripe will send `payment_intent.succeeded` webhooks to your deployed application.

### 6. Verify Deployment

After a successful payment is processed:

1. **Check application logs**: `docker logs payment-hook`
2. **Verify database record**: Query the `fina_receipt` table for the transaction
3. **Verify S3 storage**: Check your S3 bucket for the transaction folder with all files
4. **Check FINA receipt**: Verify ZKI and JIR values are populated in the database

**Health check endpoint**: `https://payment-hook.example.com/health`

Expected response when healthy:
```json
{
  "status": "healthy",
  "database": "connected",
  "environment": "complete",
  "timestamp": "2025-10-24T12:34:56+01:00"
}
```

## ⚠️ Important: Multiple Instances Caveat

**WARNING:** Running multiple instances that receive the same Stripe webhooks will result in duplicate fiscal receipts being issued for a single payment.

### Problem Scenarios

This issue occurs when:
- **Local development + production**: stripe-cli forwards webhooks to localhost AND Stripe sends webhooks to production deployment
- **Multiple deployments**: Two or more production/test deployments are both configured as webhook endpoints in Stripe
- **Multiple webhook endpoints**: Same Stripe account has multiple webhook endpoints pointing to different instances

### Why This Happens

Each instance that receives a `payment_intent.succeeded` webhook will:
1. Process the payment independently
2. Generate a unique fiscal receipt with a new receipt number
3. Issue a separate FINA fiscalization request
4. Store separate records in their respective databases

This means **one payment = multiple fiscal receipts**, which is incorrect for accounting and compliance purposes.

### How to Avoid This Issue

**Choose ONE of these approaches:**

1. **During Development:**
   - Use stripe-cli for local testing only
   - Do NOT configure production webhook endpoint during local development
   - OR temporarily disable production webhook endpoint while testing locally

2. **For Multiple Environments:**
   - Create separate Stripe accounts for different environments:
     - Stripe Test account → Development/staging deployments
     - Stripe Live account → Production deployment
   - Each account has its own webhook configuration
   - Test payments go to test account only
   - Real payments go to live account only

3. **For Production Deployments:**
   - Configure only ONE webhook endpoint per Stripe account
   - Use load balancer if you need multiple application instances for high availability
   - All instances should share the same database to prevent duplicate processing

### Detection and Recovery

If duplicate fiscal receipts have been issued:
1. Check database for multiple records with the same `stripe_id`
2. Check S3 storage for multiple folders with the same event ID (but different hostname/PID)
3. Contact FINA support to cancel incorrect fiscal receipts if necessary

**Best practice:** Always ensure only one instance receives webhooks for any given Stripe payment.

## Troubleshooting

- **Webhook signature validation fails**: Ensure nginx has `proxy_buffering off` and `proxy_request_buffering off`
- **Database connection errors**: Verify PostgreSQL credentials and network connectivity
- **S3 upload failures**: Check S3 credentials and bucket permissions
- **FINA fiscalization errors**: Verify certificate path, password, and endpoint URL
- **Currency errors**: Ensure all payments are in EUR (FINA requirement)
- **Duplicate fiscal receipts**: See "Multiple Instances Caveat" section above

# Git Pre-Push Hook

If needed, add git pre-push hook before pushing changes to ensure code quality:

```bash
ln -s ../../.githooks/pre-push .git/hooks/pre-push
```

This will automatically run code formatting and linting checks before each push.

# 📆 Roadmap

- Fiscalization of B2C transactions with a country dependent VAT
- Operations other then fiscalize: correct, cancel, delayed fiscalization
