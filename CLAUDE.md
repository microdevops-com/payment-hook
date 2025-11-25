# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Docker Development (Recommended)
```bash
# Start all services (app, nginx, postgres)
docker compose up --build

# Run in background
docker compose up -d --build

# View logs
docker compose logs -f payment-hook

# Stop services
docker compose down

# Reset database (removes all data)
docker compose down -v && docker compose up -d pg
```

### Production Docker (Single Container)
```bash
# Build production image
docker build -t payment-hook .

# Run with external PostgreSQL and nginx
docker run -d \
  --name payment-hook \
  --env-file .env \
  -v $(pwd)/payment:/app/payment \
  -v $(pwd)/receipt:/app/receipt \
  -v $(pwd)/cert:/app/cert:ro \
  -p 8000:8000 \
  payment-hook
```

### Database Migrations
```bash
# Run migrations manually (inside container)
docker compose exec payment-hook python migrate.py

# Check migration status
docker compose exec payment-hook python migrate.py status

# Create new migration
docker compose exec payment-hook python migrate.py create add_new_column
```

### Stripe Testing (Docker Development)
```bash
docker compose exec stripe-cli sh -c 'stripe trigger payment_intent.succeeded --override payment_intent:currency=eur --override payment_intent:amount=1234 --api-key $STRIPE_API_SECRET_KEY'
```

## Architecture Overview

This is a Python Flask application that processes payment webhooks and integrates them with fiscal systems. Currently supports **Stripe → FINA** flow, but designed for extensibility to support multiple payment providers and fiscal systems.

**Current Implementation:** Stripe payments → Croatian FINA fiscalization

### Core Components

**app.py** - Main Flask application
- Receives payment webhook events (currently `/stripe/payment-intent`)
- Validates webhook signatures and extracts payment data
- Routes payments to appropriate fiscal system (currently hardcoded to FINA)
- Saves raw webhook data to S3 storage in organized folders

**fina.py** - FINA fiscalization engine
- Handles FINA-specific database operations (`fina_receipt` table)
- Generates ZKI (protective code) using FINA certificate
- Builds XML receipt according to FINA schema
- Signs XML with PKCS#12 certificate
- Sends SOAP request to FINA endpoint
- Saves request/response files to S3 storage

**s3_storage.py** - S3-compatible storage module
- Handles file uploads to Hetzner Object Storage (S3-compatible)
- Supports both text and binary file uploads
- Provides logging for successful uploads and error handling

**migrate.py** - Database migration system
- Manages database schema changes over time
- Tracks applied migrations in `schema_migrations` table
- Supports creating new migrations and checking status
- Runs automatically on container startup

### Data Flow (Current: Stripe → FINA)
1. Stripe sends webhook → `app.py` `/stripe/payment-intent` endpoint
2. `app.py` validates signature and extracts payment data
3. `app.py` creates organized folder structure and saves webhook data to S3
4. `app.py` determines fiscal system routing (currently hardcoded to "fina")
5. `fina.py` handles FINA-specific processing:
   - Gets next receipt number from `fina_receipt` table
   - Calls `fiscalize()` to generate fiscal receipt
   - Communicates with FINA endpoint and returns ZKI/JIR
   - Saves successful transaction to `fina_receipt` table
   - Saves fiscal receipt files to same S3 folder as webhook data
6. All transaction files are organized in S3 with consistent folder structure

### File Structure
- `app.py` - Main Flask application (~400 lines)
- `fina.py` - FINA fiscalization logic (~500 lines)
- `fina_cli.py` - CLI tool for manual FINA operations (~350 lines)
- `s3_storage.py` - S3 storage module (~100 lines)
- `migrate.py` - Database migration system (~150 lines)
- `test_ssl_connection.py` - SSL connection testing utility (~180 lines)
- `migrations/` - SQL migration files
- `doc/` - FINA technical specifications and schemas
- `cert/` - FINA certificates (not committed to git)

### S3 Storage Structure
Files are stored in S3-compatible storage (Hetzner Object Storage) with organized folder structure:
```
YYYY-MM-DD-HH-MM-SS-stripe-payment-intent-{event_id}-{hostname}-{pid}/
├── stripe-webhook.json     # Raw Stripe webhook payload
├── stripe-webhook.yaml     # Parsed webhook data
├── fina-request.xml        # SOAP request sent to FINA
├── fina-request.yaml       # Parsed request data
├── fina-response.xml       # SOAP response from FINA
└── fina-response.yaml      # Parsed response data
```

**Folder naming convention:**
- Timestamp in UTC (YYYY-MM-DD-HH-MM-SS)
- Payment provider and event type (e.g., `stripe-payment-intent`)
- Stripe event ID for traceability
- Hostname to distinguish between environments (dev/production)
- Process ID to handle multiple workers/containers

This prevents file conflicts when the same Stripe event is sent to multiple environments (e.g., via stripe-cli to local dev and webhook to production).

### Database Schema
- `fina_receipt` table - Stores FINA fiscal receipt data
  - Primary key: `id` (serial)
  - Unique constraint: `(year, receipt_number)`
  - Fields: `year`, `location_id`, `register_id`, `receipt_number`, `order_id`, `stripe_id`, `amount`, `currency`, `zki`, `jir`, `payment_time`, `receipt_created`, `receipt_updated`, `status`
  - `payment_time` - The original Stripe payment timestamp (TIMESTAMPTZ)
  - `receipt_created` - Database row creation timestamp (TIMESTAMPTZ)
  - `receipt_updated` - Database row last update timestamp (TIMESTAMPTZ)
  - `status` - Receipt processing status ('pending', 'processing', 'completed', 'failed')
- `schema_migrations` table - Tracks applied migrations

**Note**: Table was renamed from `receipt` to `fina_receipt` to support future fiscal systems (e.g., `germany_receipt`, etc.)

### Environment Variables Required
```
STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB
P12_PATH, P12_PASSWORD (FINA certificate)
FINA_CA_DIR_PATH (directory containing FINA CA certificates in .pem format)
  - For demo/test environment: cert/ca_demo
  - For production environment: cert/ca (or cert/ca_prod)
FINA_TIMEZONE (e.g., Europe/Zagreb)
FINA_ENDPOINT (test/production URL)
  - Demo: https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest
  - Production: https://cis.porezna-uprava.hr:8449/FiskalizacijaService
OIB_COMPANY, OIB_OPERATOR (Croatian tax IDs)
LOCATION_ID, REGISTER_ID (fiscal identifiers)
S3_ACCESS_KEY, S3_SECRET_KEY, S3_ENDPOINT_URL, S3_BUCKET_NAME (S3-compatible storage)
```

### Optional Environment Variables (with defaults)
```
GUNICORN_WORKERS=2 (number of Gunicorn worker processes)
GUNICORN_TIMEOUT=60 (request timeout in seconds)
```

### CLI Tools
- **fina_cli.py** - Manual FINA operations
  - `--retry-receipt <receipt_number>` - Retry fiscalization for a failed receipt
  - `--create-receipt --amount <amount>` - Create manual fiscal receipt (for non-Stripe payments)
  - Supports custom payment times, order IDs, and Stripe IDs
  - Useful for fixing failed receipts, manual payments, and testing
- **test_ssl_connection.py** - SSL connection testing
  - `--ca-dir <path>` - Directory containing CA certificates
  - `--endpoint <url>` - FINA endpoint to test
  - Tests SSL handshake without sending fiscal data
  - Useful for verifying certificate configuration before deployment

### Docker Architecture
- **Development**: Docker Compose with nginx proxy, app container, PostgreSQL, stripe-cli, and migration service
- **Production**: Single container with Gunicorn, external nginx and database
- **Volumes**: `cert/`, `migrations/` directories are mounted for persistence; `payment/` and `receipt/` data now stored in S3
- **Networking**: App runs on port 8000 inside container, nginx proxies on port 8080
- **Health checks**: PostgreSQL health checks ensure database is ready before starting app
- **Migration service**: Runs once on startup, applies pending migrations, then exits

### Dependencies
Python 3.12+ with Flask, Stripe SDK, PostgreSQL (psycopg2), cryptography, xmlsec, lxml for XML processing and FINA integration. boto3 for S3-compatible storage. Gunicorn for WSGI server. Development tools: black (formatter), isort (import sorter), flake8 (linter), mypy (type checker). No python-dotenv needed (Docker handles environment variables).

### Code Quality Tools

**Manual formatting and checking:**
```bash
# Format code
docker run --rm -v $(pwd):/src -w /src payment-hook black .
docker run --rm -v $(pwd):/src -w /src payment-hook isort .

# Check style and types
docker run --rm -v $(pwd):/src -w /src payment-hook flake8 .
docker run --rm -v $(pwd):/src -w /src payment-hook mypy .
```

**Git hooks:**
The project includes a pre-push hook that automatically runs all code quality checks:
```bash
# Set up git hooks (run once after cloning)
ln -s ../../.githooks/pre-push .git/hooks/pre-push

# The pre-push hook will automatically:
# 1. Build a test Docker image
# 2. Run black --check (formatting)
# 3. Run isort --check-only (imports)
# 4. Run flake8 (linting)
# 5. Run mypy (type checking)
# 6. Block push if any checks fail
```

## Security Guidelines & Validation Patterns

When modifying or extending this codebase, follow these security recommendations:

### Input Validation
- **Always validate external inputs**: Use validation functions for webhook data, API parameters, file paths
- **S3 path validation**: Use `validate_s3_key()` from `s3_storage.py` for all S3 operations
- **Webhook validation**: Extend `validate_webhook_data()` in `app.py` for new webhook fields
- **Database inputs**: Continue using parameterized queries (already implemented)

### Error Handling Security
- **Never expose internal errors** to clients - use generic error messages
- **Log detailed errors** server-side with `logger.error()` for debugging
- **Catch specific exceptions** rather than broad `Exception` catches when possible
- **Example pattern**:
  ```python
  try:
      # risky operation
  except SpecificException as e:
      logger.error(f"Detailed error: {e}")
      return {"error": "Generic user message"}, 400
  ```

### Environment-Based Security
- **SSL verification**: Use `APP_ENV` to control security features
- **Debug mode**: Never hardcode `debug=True` - use environment variables
- **Pattern**: `os.environ.get("APP_ENV", "production").lower() in ["dev", "development"]`

### File and Data Handling
- **Temporary files**: Use `tempfile.NamedTemporaryFile()` with default `delete=True`
- **Sensitive data**: Never log secrets, certificates, or personal data
- **S3 storage**: Validate all paths before storage operations

### New Feature Security Checklist
When adding new features:
- [ ] Validate all inputs from external sources
- [ ] Use parameterized SQL queries
- [ ] Implement proper error handling (log details, return generic messages)
- [ ] Test with invalid/malicious inputs
- [ ] Use environment variables for security-sensitive configuration
- [ ] Add appropriate logging for security events

### Known Security Limitations
- **MD5 hash usage**: Required by FINA specification (not changeable)
- **Certificate handling**: PKCS#12 certificates required for FINA integration

## Important Notes

- Docker Compose is the recommended development and deployment method
- No test framework is currently configured
- Code formatting with Black, isort, flake8, and mypy configured in pyproject.toml
- Pre-push git hook automatically runs code quality checks before allowing pushes
- The system handles Croatian VAT-exempt transactions only
- All fiscal receipts and webhook data are stored permanently in S3 for audit compliance
- Certificate files in cert/ directory are required but not committed to git
- Database migrations run automatically on `docker compose up`
- Files are stored in S3-compatible storage (Hetzner Object Storage) instead of local directories
- No virtual environment setup needed - Docker handles all dependencies
