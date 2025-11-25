#!/usr/bin/env python3
"""
Simple CLI tool for FINA fiscalization operations.

Usage:
    python fina_cli.py --retry-receipt 123
    python fina_cli.py --create-receipt --amount 100.00 --payment-time "2025-01-15 10:30:00"
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from fina import fiscalize, get_db_connection, process_fina_fiscalization

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def retry_receipt(receipt_number: int) -> dict:
    """Retry fiscalization for a failed receipt."""

    # Get receipt from database
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, year, location_id, register_id, receipt_number,
                       order_id, stripe_id, amount, currency, status, payment_time
                FROM fina_receipt
                WHERE receipt_number = %s
                """,
                [receipt_number],
            )
            receipt = cur.fetchone()

    if not receipt:
        raise ValueError(f"Receipt {receipt_number} not found")

    if receipt["status"] == "completed":
        raise ValueError(f"Receipt {receipt_number} already completed")

    logger.info(f"Retrying receipt {receipt_number} (status: {receipt['status']})")

    # Use original payment time from database for fiscalization
    # payment_time from DB is stored with timezone info (UTC), convert to local timezone
    fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
    payment_time_utc = receipt["payment_time"]
    payment_time_local = payment_time_utc.astimezone(fina_timezone)

    # Generate S3 folder path with current UTC time (consistent with normal flow)
    now_utc = datetime.now(ZoneInfo("UTC"))
    timestamp = now_utc.strftime("%Y-%m-%d-%H-%M-%S")
    hostname = os.environ.get("HOSTNAME", "unknown")
    pid = os.getpid()
    identifier = receipt["stripe_id"] or receipt["order_id"] or f"receipt-{receipt_number}"
    shared_folder_path = f"{timestamp}-fina-retry-{identifier}-{hostname}-{pid}"

    # Perform fiscalization with original payment time in local timezone
    result = fiscalize(payment_time_local, float(receipt["amount"]), receipt_number, shared_folder_path)

    # Update database with results
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if result.get("JIR"):
                cur.execute(
                    """
                    UPDATE fina_receipt
                    SET zki = %s, jir = %s, status = 'completed', receipt_updated = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    [result.get("ZKI"), result.get("JIR"), receipt["id"]],
                )
                status = "completed"
            else:
                cur.execute(
                    """
                    UPDATE fina_receipt
                    SET zki = %s, status = 'failed', receipt_updated = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    [result.get("ZKI"), receipt["id"]],
                )
                status = "failed"
            conn.commit()

    return {
        "receipt_number": receipt_number,
        "status": status,
        "zki": result.get("ZKI"),
        "jir": result.get("JIR"),
        "amount": receipt["amount"],
        "currency": receipt["currency"],
    }


def create_receipt(
    amount: Decimal,
    payment_time: datetime,
    order_id: str | None = None,
    stripe_id: str | None = None,
) -> dict:
    """Create and fiscalize a new receipt manually."""

    # FINA only accepts EUR
    currency = "eur"

    # Ensure payment_time has timezone info (assume FINA timezone if not provided)
    if payment_time.tzinfo is None:
        fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
        payment_time = payment_time.replace(tzinfo=fina_timezone)
        logger.info(f"Payment time had no timezone, assumed {os.environ['FINA_TIMEZONE']}: {payment_time}")

    # Convert to FINA timezone if it's in a different timezone
    fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
    payment_time_local = payment_time.astimezone(fina_timezone)

    # Generate unique payment_id for manual creation
    if not stripe_id:
        import uuid

        stripe_id = f"manual_{uuid.uuid4().hex[:16]}"
        logger.info(f"Generated manual payment ID: {stripe_id}")

    # Generate S3 folder path with current UTC time
    now_utc = datetime.now(ZoneInfo("UTC"))
    timestamp = now_utc.strftime("%Y-%m-%d-%H-%M-%S")
    hostname = os.environ.get("HOSTNAME", "unknown")
    pid = os.getpid()
    shared_folder_path = f"{timestamp}-fina-manual-{stripe_id}-{hostname}-{pid}"

    logger.info(f"Creating manual receipt: amount={amount} {currency}, payment_time={payment_time_local}")

    # Use the same flow as webhook processing
    try:
        result = process_fina_fiscalization(
            payment_id=stripe_id,
            payment_time=payment_time_local,
            payment_amount=float(amount),
            payment_currency=currency,
            invoice_id=order_id,
            shared_folder_path=shared_folder_path,
        )

        # Get receipt_number from database
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT receipt_number, status, zki, jir
                    FROM fina_receipt
                    WHERE stripe_id = %s
                    """,
                    [stripe_id],
                )
                receipt = cur.fetchone()

        if result.get("JIR"):
            logger.info(f"✅ Receipt created successfully: {receipt['receipt_number']}")
            return {
                "success": True,
                "receipt_number": receipt["receipt_number"],
                "status": receipt["status"],
                "zki": receipt["zki"],
                "jir": receipt["jir"],
                "amount": amount,
                "currency": currency,
                "stripe_id": stripe_id,
                "order_id": order_id,
            }
        else:
            logger.warning("❌ Receipt creation failed - no JIR received")
            return {
                "success": False,
                "receipt_number": receipt["receipt_number"],
                "status": receipt["status"],
                "zki": receipt.get("zki"),
                "error": "No JIR received from FINA",
                "stripe_id": stripe_id,
            }

    except Exception as e:
        logger.error(f"Failed to create receipt: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="FINA fiscalization CLI tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Retry failed receipt
  python fina_cli.py --retry-receipt 123

  # Create new receipt with current time
  python fina_cli.py --create-receipt --amount 100.00

  # Create new receipt with specific payment time
  python fina_cli.py --create-receipt --amount 150.50 \\
      --payment-time "2025-01-15 14:30:00" --order-id "order_123"

  # Create receipt with Stripe ID
  python fina_cli.py --create-receipt --amount 200.00 \\
      --stripe-id "pi_test123" --order-id "order_456"
        """,
    )

    # Mutually exclusive operations
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--retry-receipt", type=int, metavar="N", help="Retry fiscalization for receipt number N")
    operation.add_argument("--create-receipt", action="store_true", help="Create and fiscalize a new receipt")

    # Arguments for --create-receipt
    parser.add_argument("--amount", type=Decimal, help="Payment amount in EUR (required for --create-receipt)")
    parser.add_argument(
        "--payment-time",
        type=str,
        help='Payment timestamp in format "YYYY-MM-DD HH:MM:SS" (optional, defaults to current time)',
    )
    parser.add_argument("--order-id", type=str, help="Order ID (optional)")
    parser.add_argument(
        "--stripe-id", type=str, help="Stripe payment intent ID (optional, auto-generated if not provided)"
    )

    args = parser.parse_args()

    try:
        if args.retry_receipt:
            result = retry_receipt(args.retry_receipt)

            if result["status"] == "completed":
                print(f"✅ Receipt {result['receipt_number']} completed successfully")
                print(f"   Amount: {result['amount']} {result['currency']}")
                print(f"   ZKI: {result['zki']}")
                print(f"   JIR: {result['jir']}")
                return 0
            else:
                print(f"❌ Receipt {result['receipt_number']} failed")
                print(f"   ZKI: {result['zki']}")
                return 1

        elif args.create_receipt:
            # Validate required arguments
            if not args.amount:
                parser.error("--create-receipt requires --amount")

            # Parse payment time
            if args.payment_time:
                try:
                    payment_time = datetime.strptime(args.payment_time, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    parser.error('--payment-time must be in format "YYYY-MM-DD HH:MM:SS"')
            else:
                # Use current time in FINA timezone
                fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
                payment_time = datetime.now(fina_timezone)

            result = create_receipt(
                amount=args.amount,
                payment_time=payment_time,
                order_id=args.order_id,
                stripe_id=args.stripe_id,
            )

            if result["success"]:
                print(f"✅ Receipt {result['receipt_number']} created successfully")
                print(f"   Amount: {result['amount']} {result['currency']}")
                print(f"   Payment ID: {result['stripe_id']}")
                if result.get("order_id"):
                    print(f"   Order ID: {result['order_id']}")
                print(f"   ZKI: {result['zki']}")
                print(f"   JIR: {result['jir']}")
                return 0
            else:
                print(f"❌ Receipt {result['receipt_number']} creation failed")
                print(f"   Payment ID: {result['stripe_id']}")
                print(f"   ZKI: {result.get('zki', 'N/A')}")
                print(f"   Error: {result['error']}")
                return 1

    except Exception as e:
        logger.error(f"Error: {e}")
        print(f"❌ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
