#!/usr/bin/env python3
"""
Simple CLI tool for FINA fiscalization operations.

Usage:
    python fina_cli.py --retry-receipt 123
    python fina_cli.py --create-receipt --amount 100.00 --payment-time "2025-01-15 10:30:00"
    python fina_cli.py --generate-pdf 1 --template template.md --font RobotoMonoNerdFont-Medium
    python fina_cli.py --generate-pending-pdfs --template template.md --font RobotoMonoNerdFont-Medium
"""

import argparse
import io
import logging
import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import qrcode
from fpdf import FPDF, FontFace
from jinja2 import Template
from markdown_it import MarkdownIt
from psycopg2.extras import RealDictCursor

from fina import fiscalize, get_db_connection, process_fina_fiscalization
from s3_storage import save_binary_file_to_s3

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


def sanitize_filename(text: str) -> str:
    """
    Sanitize text for use in filenames (S3-safe).

    Args:
        text: Text to sanitize

    Returns:
        Sanitized text safe for filenames
    """
    import re

    # Replace spaces and special characters with hyphens
    sanitized = re.sub(r"[^\w\-.]", "-", text)
    # Replace multiple hyphens with single hyphen
    sanitized = re.sub(r"-+", "-", sanitized)
    # Remove leading/trailing hyphens
    sanitized = sanitized.strip("-")
    return sanitized


def generate_verification_url(jir: str, payment_time: datetime, amount: float) -> str:
    """
    Generate Croatian tax authority verification URL.

    Args:
        jir: JIR (Jedinstveni identifikator računa) from FINA
        payment_time: Payment timestamp (will be converted to FINA timezone)
        amount: Payment amount in EUR

    Returns:
        Verification URL
    """
    # Format: https://porezna.gov.hr/rn?jir=<JIR>&datv=<YYYYMMDD_HHMM>&izn=<SUM>
    # Sum: euros + cents without dot (e.g., 11.22 → 1122)
    # Convert to FINA timezone for verification
    fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
    payment_time_local = payment_time.astimezone(fina_timezone)
    date_str = payment_time_local.strftime("%Y%m%d_%H%M")
    amount_cents = int(amount * 100)  # Convert to cents

    verification_url = f"https://porezna.gov.hr/rn?jir={jir}&datv={date_str}&izn={amount_cents}"
    return verification_url


def generate_qr_code_image(verification_url: str) -> str:
    """
    Generate QR code for verification URL according to Croatian tax authority requirements.

    Requirements (ISO/IEC 15415):
    - QR code model 1 or 2, smallest possible version
    - At least 2x2 cm size
    - At least 2mm empty space on all sides (border)
    - Error correction level at least "L"
    - No images or logos

    Args:
        verification_url: Full verification URL

    Returns:
        Path to temporary QR code image file
    """
    qr = qrcode.QRCode(
        version=2,  # Smallest version (21x21 modules)
        error_correction=qrcode.constants.ERROR_CORRECT_L,  # Required: at least "L" level
        border=4,  # Border in boxes (quiet zone) - 4 is standard minimum for QR codes
    )
    qr.add_data(verification_url)
    qr.make(fit=True)  # Auto-adjust version if data doesn't fit

    img = qr.make_image(fill_color="black", back_color="white")

    # Save to temporary file
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".png", delete=False) as tmp_file:
        img.save(tmp_file, format="PNG")
        return tmp_file.name


def generate_pdf_for_receipt(receipt_id: int, template_name: str, font_name: str) -> dict:
    """
    Generate PDF receipt for a specific receipt ID with Jinja2 templating.

    Args:
        receipt_id: Receipt ID from database
        template_name: Template filename (e.g., 'template.md')
        font_name: Font name without extension (e.g., 'RobotoMonoNerdFont-Medium')

    Returns:
        Dictionary with generation results
    """
    # Get receipt data from database
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, year, location_id, register_id, receipt_number,
                       order_id, stripe_id, amount, currency, zki, jir,
                       payment_time, status, receipt_created, receipt_updated,
                       s3_folder_path, pdf_status, pdf_created
                FROM fina_receipt
                WHERE id = %s
                """,
                [receipt_id],
            )
            receipt = cur.fetchone()

    if not receipt:
        raise ValueError(f"Receipt ID {receipt_id} not found")

    if receipt["status"] != "completed":
        raise ValueError(f"Receipt {receipt_id} status is '{receipt['status']}', must be 'completed' to generate PDF")

    if not receipt["jir"]:
        raise ValueError(f"Receipt {receipt_id} has no JIR, cannot generate PDF")

    if not receipt["s3_folder_path"]:
        raise ValueError(f"Receipt {receipt_id} has no S3 folder path, cannot upload PDF")

    logger.info(f"Generating PDF for receipt {receipt_id} (receipt_number: {receipt['receipt_number']})")

    # Mark as processing
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fina_receipt
                SET pdf_status = 'processing'
                WHERE id = %s
                """,
                [receipt_id],
            )
            conn.commit()

    qr_code_path = None

    try:
        # Generate verification URL
        verification_url = generate_verification_url(receipt["jir"], receipt["payment_time"], float(receipt["amount"]))
        logger.info(f"Verification URL: {verification_url}")

        # Generate QR code with verification URL
        qr_code_path = generate_qr_code_image(verification_url)
        logger.info(f"Generated QR code: {qr_code_path}")

        # Read template file
        template_path = os.path.join("templates", template_name)
        if not os.path.exists(template_path):
            raise ValueError(f"Template '{template_name}' not found in templates/ directory")

        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        # Prepare template data with special QR code placeholder
        # Convert payment_time to Croatian timezone for display
        fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
        payment_time_local = receipt["payment_time"].astimezone(fina_timezone)

        template_data = {
            "receipt_number": (
                f"{receipt['year']}/{receipt['location_id']}/" f"{receipt['register_id']}/{receipt['receipt_number']}"
            ),
            "order_id": receipt["order_id"] or "N/A",
            "amount": f"{float(receipt['amount']):.2f}",
            "register_id": receipt["register_id"],
            "location_id": receipt["location_id"],
            "payment_time": payment_time_local.strftime("%d.%m.%Y %H:%M:%S"),
            "zki": receipt["zki"],
            "jir": receipt["jir"],
            "verification_link": f'<font size="7"><a href="{verification_url}">{verification_url}</a></font>',
            "qr_code": "<!--QR_CODE_PLACEHOLDER-->",  # Special marker for QR code insertion
        }

        # Render Jinja2 template
        logger.info("Rendering Jinja2 template")
        jinja_template = Template(template_content)
        rendered_markdown = jinja_template.render(**template_data)

        # Convert markdown to HTML
        logger.info("Converting markdown to HTML")
        md = MarkdownIt("commonmark", {"breaks": True, "html": True}).enable("table")
        html_content = md.render(rendered_markdown)

        # Replace QR code placeholder with actual image tag
        # QR code must be at least 2x2 cm (20mm) according to Croatian tax authority requirements
        # fpdf2 HTML rendering uses 96 DPI (Windows standard) not 72 DPI
        # Conversion: 1 inch = 25.4 mm, so 1 mm = 96/25.4 ≈ 3.779528 pixels
        # For 20mm: 20 * (96/25.4) ≈ 75.59 pixels
        qr_size_mm = 20  # 2 cm = 20 mm (minimum required size)
        qr_size_pixels = int(qr_size_mm * 96 / 25.4)  # Convert mm to pixels at 96 DPI
        qr_code_html = f'<img src="{qr_code_path}" width="{qr_size_pixels}" />'
        html_content = html_content.replace("<!--QR_CODE_PLACEHOLDER-->", qr_code_html)

        # Create PDF
        logger.info("Generating PDF")
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)

        # Add and set custom font
        font_path = os.path.join("fonts", f"{font_name}.ttf")
        if not os.path.exists(font_path):
            raise ValueError(f"Font '{font_name}.ttf' not found in fonts/ directory")

        logger.info(f"Adding custom font: {font_path}")
        pdf.add_font(font_name, style="", fname=font_path)
        pdf.set_font(font_name, size=10)

        # Render HTML with custom font (QR code is already in HTML)
        pdf.write_html(
            html_content,
            font_family=font_name,
            tag_styles={
                "h1": FontFace(family=font_name, size_pt=16),
                "h2": FontFace(family=font_name, size_pt=14),
                "h3": FontFace(family=font_name, size_pt=12),
                "pre": FontFace(family=font_name, size_pt=9),
                "code": FontFace(family=font_name, size_pt=9),
            },
        )

        # Save to BytesIO for S3 upload
        pdf_bytes = pdf.output()
        pdf_buffer = io.BytesIO(pdf_bytes)

        # Upload to S3 with sanitized order_id in filename
        order_id_safe = sanitize_filename(receipt["order_id"]) if receipt["order_id"] else "no-order-id"
        pdf_filename = f"fina-receipt-{order_id_safe}.pdf"
        s3_path = f"{receipt['s3_folder_path']}/{pdf_filename}"
        logger.info(f"Uploading PDF to S3: {s3_path}")
        save_binary_file_to_s3(pdf_buffer.getvalue(), s3_path)

        # Update database - mark as completed
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE fina_receipt
                    SET pdf_status = 'completed', pdf_created = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    [receipt_id],
                )
                conn.commit()

        logger.info(f"✅ PDF generated successfully for receipt {receipt_id}")
        return {
            "success": True,
            "receipt_id": receipt_id,
            "receipt_number": receipt["receipt_number"],
            "s3_path": s3_path,
        }

    except Exception as e:
        # Mark as failed
        logger.error(f"PDF generation failed for receipt {receipt_id}: {e}")
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE fina_receipt
                    SET pdf_status = 'failed'
                    WHERE id = %s
                    """,
                    [receipt_id],
                )
                conn.commit()
        raise
    finally:
        # Clean up temporary QR code file
        if qr_code_path and os.path.exists(qr_code_path):
            try:
                os.unlink(qr_code_path)
            except Exception as e:
                logger.warning(f"Failed to delete temporary QR code file: {e}")


def generate_pending_pdfs(template_name: str, font_name: str, limit: int = 100) -> dict:
    """
    Batch process pending PDF receipts.

    Args:
        template_name: Template filename (e.g., 'template.md')
        font_name: Font name without extension (e.g., 'RobotoMonoNerdFont-Medium')
        limit: Maximum number of receipts to process in one batch

    Returns:
        Dictionary with batch processing results
    """
    # Get pending receipts
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, receipt_number
                FROM fina_receipt
                WHERE pdf_status = 'pending' AND status = 'completed'
                ORDER BY id ASC
                LIMIT %s
                """,
                [limit],
            )
            pending_receipts = cur.fetchall()

    if not pending_receipts:
        logger.info("No pending receipts to process")
        return {"success": True, "processed": 0, "failed": 0, "total": 0}

    logger.info(f"Found {len(pending_receipts)} pending receipts to process")

    processed = 0
    failed = 0

    for receipt in pending_receipts:
        try:
            generate_pdf_for_receipt(receipt["id"], template_name, font_name)
            processed += 1
        except Exception as e:
            logger.error(f"Failed to generate PDF for receipt {receipt['id']}: {e}")
            failed += 1

    logger.info(f"Batch processing complete: {processed} successful, {failed} failed, {len(pending_receipts)} total")

    return {
        "success": True,
        "processed": processed,
        "failed": failed,
        "total": len(pending_receipts),
    }


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
    operation.add_argument("--generate-pdf", type=int, metavar="ID", help="Generate PDF for receipt ID")
    operation.add_argument(
        "--generate-pending-pdfs", action="store_true", help="Batch generate PDFs for all pending receipts"
    )

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

    # Arguments for --generate-pdf and --generate-pending-pdfs
    parser.add_argument("--template", type=str, help="Template filename (e.g., 'template.md')")
    parser.add_argument(
        "--font",
        type=str,
        help="Font name without extension (e.g., 'RobotoMonoNerdFont-Medium' for Unicode support)",
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

        elif args.generate_pdf:
            # Validate required arguments
            if not args.template:
                parser.error("--generate-pdf requires --template")
            if not args.font:
                parser.error("--generate-pdf requires --font")

            result = generate_pdf_for_receipt(args.generate_pdf, args.template, args.font)
            print(f"✅ PDF generated for receipt {result['receipt_number']}")
            print(f"   Receipt ID: {result['receipt_id']}")
            print(f"   S3 path: {result['s3_path']}")
            return 0

        elif args.generate_pending_pdfs:
            # Validate required arguments
            if not args.template:
                parser.error("--generate-pending-pdfs requires --template")
            if not args.font:
                parser.error("--generate-pending-pdfs requires --font")

            result = generate_pending_pdfs(args.template, args.font)
            print("✅ Batch processing complete:")
            print(f"   Processed: {result['processed']}")
            print(f"   Failed: {result['failed']}")
            print(f"   Total: {result['total']}")
            return 0

    except Exception as e:
        logger.error(f"Error: {e}")
        print(f"❌ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
