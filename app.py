import logging
import os
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import stripe
import yaml
from flask import Flask, jsonify, request

from s3_storage import save_binary_file_to_s3, save_file_to_s3


def validate_payment_intent_data(payment_intent: dict) -> None:
    """
    Validate Stripe payment_intent webhook data (payment_intent.succeeded).

    Args:
        payment_intent: Stripe payment_intent object from webhook

    Raises:
        ValueError: If validation fails
    """
    # Validate payment_id
    payment_id = payment_intent.get("id")
    if not payment_id or not isinstance(payment_id, str) or len(payment_id) > 200:
        raise ValueError("Invalid payment ID")

    # Validate payment_amount (must be positive integer in cents)
    amount = payment_intent.get("amount")
    if not isinstance(amount, int) or amount <= 0 or amount > 999999900:  # Max ~$10M
        raise ValueError("Invalid payment amount")

    # Validate currency (must be 3-letter code)
    currency = payment_intent.get("currency")
    if not currency or not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError("Invalid currency code")

    # Validate timestamp
    created = payment_intent.get("created")
    if not isinstance(created, int) or created <= 0:
        raise ValueError("Invalid payment timestamp")

    # Validate status (must be "succeeded")
    status = payment_intent.get("status")
    if status != "succeeded":
        raise ValueError("Payment status is not 'succeeded'")

    # Validate metadata if present (optional field)
    metadata = payment_intent.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Invalid metadata format")


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_db_connection():
    import psycopg2

    return psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DB"],
        user=os.environ["PG_USER"],
        password=os.environ["PG_PASSWORD"],
    )


def process_payment_intent_webhook():
    """
    Process Stripe payment_intent.succeeded webhook.

    Returns:
        Tuple of (response_dict, status_code)
    """
    stripe_webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]
    fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    # Verify webhook signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, stripe_webhook_secret)
        logger.info(f"Webhook signature verified for event: {event.get('id', 'unknown')}")
    except Exception as e:
        logger.error(f"Webhook signature verification failed: {e}")
        return jsonify({"status": "error", "message": "Invalid webhook signature"}), 400

    # Check event type
    if event["type"] != "payment_intent.succeeded":
        logger.info(f"Ignoring event type: {event['type']}")
        return jsonify({"status": "ignored", "event_type": event["type"]}), 200

    payment_intent = event["data"]["object"]

    # Validate webhook data for security
    try:
        validate_payment_intent_data(payment_intent)
    except ValueError as e:
        logger.error(f"Webhook data validation failed: {e}")
        return jsonify({"status": "error", "message": "Invalid webhook data"}), 400

    # Extract payment data from payment_intent
    payment_id = payment_intent.get("id")
    payment_time = payment_intent.get("created")
    payment_time_utc = datetime.fromtimestamp(payment_time, tz=ZoneInfo("UTC"))
    payment_time_local = payment_time_utc.astimezone(fina_timezone)
    payment_amount = payment_intent.get("amount", 0) / 100
    payment_currency = payment_intent.get("currency")

    # Extract invoice_id from metadata or description
    metadata = payment_intent.get("metadata", {})
    invoice_id = metadata.get("invoice_id") or metadata.get("order_id") or payment_intent.get("description")

    # Check for idempotency - prevent duplicate processing
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, zki, jir, receipt_number FROM fina_receipt WHERE stripe_id = %s", [payment_id]
                )
                existing_record = cur.fetchone()

                if existing_record:
                    existing_id, existing_status, existing_zki, existing_jir, existing_receipt_number = existing_record
                    logger.info(f"Payment {payment_id} already processed with status: {existing_status}")

                    # Return appropriate response based on existing status
                    if existing_status == "completed":
                        return (
                            jsonify(
                                {
                                    "status": "success",
                                    "message": "Payment already processed successfully",
                                    "payment_amount": payment_amount,
                                    "ZKI": existing_zki,
                                    "JIR": existing_jir,
                                    "receipt_number": existing_receipt_number,
                                    "idempotent": True,
                                }
                            ),
                            200,
                        )
                    elif existing_status == "processing":
                        return (
                            jsonify(
                                {
                                    "status": "processing",
                                    "message": "Payment is currently being processed",
                                    "receipt_number": existing_receipt_number,
                                    "idempotent": True,
                                }
                            ),
                            202,
                        )  # 202 Accepted - processing
                    else:  # failed status
                        return (
                            jsonify(
                                {
                                    "status": "failed",
                                    "message": "Payment processing previously failed",
                                    "receipt_number": existing_receipt_number,
                                    "idempotent": True,
                                }
                            ),
                            422,
                        )  # 422 Unprocessable Entity
    except Exception as e:
        logger.error(f"Error checking for existing payment {payment_id}: {e}")
        # Continue with processing if we can't check - better than failing

    # Prepare data for S3 storage
    payment_time_local_yaml = payment_time_local.strftime("%Y-%m-%d %H:%M:%S")
    parsed = {
        "payment_id": payment_id,
        "payment_time": payment_time_local_yaml,
        "payment_amount": payment_amount,
        "payment_currency": payment_currency,
        "invoice_id": invoice_id,
    }

    # Create folder structure: YYYY-MM-DD-HH-MM-SS-stripe-payment-intent-event_id-hostname-pid (UTC time)
    # Include hostname and PID to avoid conflicts between dev and production environments
    event_id = event.get("id", "unknown")
    hostname = socket.gethostname()
    pid = os.getpid()
    payment_time_folder_utc = payment_time_utc.strftime("%Y-%m-%d-%H-%M-%S")
    folder_path = f"{payment_time_folder_utc}-stripe-payment-intent-{event_id}-{hostname}-{pid}"

    # Save webhook data to S3
    logger.info(f"Saving webhook data to S3: {folder_path}")
    save_binary_file_to_s3(payload, f"{folder_path}/stripe-webhook.json")
    yaml_content = yaml.dump(parsed, allow_unicode=True)
    save_file_to_s3(yaml_content, f"{folder_path}/stripe-webhook.yaml")

    # Flow configuration: stripe_payment_intent -> fina (currently hardcoded)
    fiscal_system = "fina"  # TODO: make this configurable
    logger.info(f"Processing payment with fiscal system: {fiscal_system}")

    if fiscal_system == "fina":
        # FINA requires EUR currency only
        if payment_currency.upper() != "EUR":
            logger.error(f"FINA fiscalization requires EUR currency, got: {payment_currency}")
            result = {
                "status": "error",
                "message": f"FINA fiscalization only supports EUR currency, received: {payment_currency}",
                "payment_id": payment_id,
                "payment_currency": payment_currency,
            }
            return jsonify(result), 422  # 422 Unprocessable Entity

        from fina import process_fina_fiscalization

        result = process_fina_fiscalization(
            payment_id,
            payment_time_local,
            payment_amount,
            payment_currency,
            invoice_id,
            folder_path,  # Pass the shared folder path
        )
        if result.get("JIR"):
            logger.info(f"Fiscalization successful - JIR: {result.get('JIR')}, ZKI: {result.get('ZKI')}")
        else:
            logger.warning(f"Fiscalization failed - no JIR received: {result}")
    else:
        logger.error(f"Unsupported fiscal system: {fiscal_system}")
        result = {
            "status": "error",
            "message": f"Unsupported fiscal system: {fiscal_system}",
        }

    return jsonify(result), 200


@app.route("/health", methods=["GET"])
def health_check():
    logger.info("Health check requested")

    # Required environment variables
    required_env_vars = [
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "STRIPE_WEBHOOK_SECRET",
        "P12_PATH",
        "P12_PASSWORD",
        "FINA_CA_DIR_PATH",
        "FINA_TIMEZONE",
        "FINA_ENDPOINT",
        "OIB_COMPANY",
        "OIB_OPERATOR",
        "LOCATION_ID",
        "REGISTER_ID",
        "PG_HOST",
        "PG_PORT",
        "PG_USER",
        "PG_PASSWORD",
        "PG_DB",
    ]

    # Check for missing environment variables
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
        logger.error(f"Health check failed: Missing environment variables: {', '.join(missing_vars)}")
        return (
            jsonify(
                {
                    "status": "unhealthy",
                    "environment": "incomplete",
                    "missing_vars": missing_vars,
                    "error": "Required environment variables not set",
                    "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
                }
            ),
            503,
        )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        # Cleanup stale processing records during health checks
        # This runs periodically when health checks are called by monitoring systems
        try:
            from fina import cleanup_stale_processing_records

            cleaned_count = cleanup_stale_processing_records(max_age_minutes=30)
            logger.info(f"Health check cleanup: {cleaned_count} stale records processed")
        except Exception as cleanup_error:
            logger.warning(f"Cleanup during health check failed: {cleanup_error}")
            # Don't fail health check if cleanup fails

        logger.info("Health check passed")
        return (
            jsonify(
                {
                    "status": "healthy",
                    "database": "connected",
                    "environment": "complete",
                    "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return (
            jsonify(
                {
                    "status": "unhealthy",
                    "database": "disconnected",
                    "environment": "complete",
                    "error": "Database connection failed",
                    "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(),
                }
            ),
            503,
        )


@app.route("/stripe/payment-intent", methods=["POST"])
def stripe_payment_intent_webhook():
    """Handle Stripe payment_intent.succeeded webhook events."""
    logger.info("Stripe payment_intent webhook received")
    return process_payment_intent_webhook()


if __name__ == "__main__":
    # Only enable debug mode in development environment
    debug_mode = os.environ.get("APP_ENV", "production").lower() in ["dev", "development"]
    app.run(debug=debug_mode)
