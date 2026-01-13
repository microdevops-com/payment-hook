import hashlib
import logging
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import requests
import xmlsec
import yaml
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
from lxml import etree as ET
from psycopg2.extras import RealDictCursor

from s3_storage import save_file_to_s3

logger = logging.getLogger(__name__)


def get_config():
    return {
        "fina_timezone": ZoneInfo(os.environ["FINA_TIMEZONE"]),
        "p12_path": os.environ["P12_PATH"],
        "p12_password": os.environ["P12_PASSWORD"],
        "fina_endpoint": os.environ["FINA_ENDPOINT"],
        "oib_company": os.environ["OIB_COMPANY"],
        "oib_operator": os.environ["OIB_OPERATOR"],
        "location_id": os.environ["LOCATION_ID"],
        "register_id": os.environ["REGISTER_ID"],
    }


FILE_REQUEST = "fina-request"
FILE_RESPONSE = "fina-response"


def get_db_connection():
    return psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DB"],
        user=os.environ["PG_USER"],
        password=os.environ["PG_PASSWORD"],
    )


def reserve_receipt_number(
    year, location_id, register_id, order_id, stripe_id, amount, currency, payment_time, s3_folder_path
):
    """
    Atomically reserve the next receipt number by inserting a new row with 'processing' status.
    Uses PostgreSQL sequence for atomic receipt number generation, eliminating race conditions.
    """
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Insert new row with 'processing' status - sequence automatically assigns receipt_number
            # receipt_created and receipt_updated are set automatically by database defaults
            cur.execute(
                """
                INSERT INTO fina_receipt (
                    year, location_id, register_id,
                    order_id, stripe_id, amount, currency,
                    zki, jir, payment_time, status, s3_folder_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, 'processing', %s)
                RETURNING receipt_number
                """,
                [year, location_id, register_id, order_id, stripe_id, amount, currency, payment_time, s3_folder_path],
            )
            row = cur.fetchone()
            receipt_number = row["receipt_number"]
            conn.commit()

            logger.info(f"Reserved receipt number {receipt_number} for year {year} (stripe_id: {stripe_id})")
            return receipt_number


def update_receipt_with_fiscalization(stripe_id, zki, jir, status):
    """
    Update the reserved receipt record with fiscalization results.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fina_receipt
                SET zki = %s, jir = %s, status = %s, receipt_updated = CURRENT_TIMESTAMP
                WHERE stripe_id = %s
                """,
                [zki, jir, status, stripe_id],
            )
            if cur.rowcount == 0:
                logger.error(f"No receipt found to update for stripe_id: {stripe_id}")
                raise ValueError(f"No receipt found to update for stripe_id: {stripe_id}")

            conn.commit()
            logger.info(f"Updated receipt for stripe_id {stripe_id} with status {status}")


def cleanup_stale_processing_records(max_age_minutes=30):
    """
    Mark old 'processing' records as 'failed' for cleanup.
    This prevents orphaned records from staying in processing state forever.

    Args:
        max_age_minutes: Records older than this will be marked as failed

    Returns:
        int: Number of records cleaned up
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fina_receipt
                SET status = 'failed', receipt_updated = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                AND receipt_created < NOW() - INTERVAL '%s minutes'
                RETURNING stripe_id, receipt_number
                """,
                [max_age_minutes],
            )

            cleaned_records = cur.fetchall()
            conn.commit()

            if cleaned_records:
                logger.warning(f"Cleaned up {len(cleaned_records)} stale processing records")
                for stripe_id, receipt_number in cleaned_records:
                    logger.warning(f"Marked as failed: stripe_id={stripe_id}, receipt_number={receipt_number}")

            return len(cleaned_records)


def process_fina_fiscalization(
    payment_id,
    payment_time,
    payment_amount,
    payment_currency,
    invoice_id,
    shared_folder_path,
):
    """
    Process FINA fiscalization for a payment.

    IMPORTANT: Database operations are intentionally NOT wrapped in a single transaction.
    This prevents losing successful fiscalizations if later database updates fail.
    The pattern is:
    1. Reserve receipt number (commit immediately)
    2. Call FINA external API (not part of any transaction)
    3. Update with results (separate commit)

    If step 3 fails, we have the fiscalization result but a 'processing' record.
    This is preferable to losing a successful fiscalization due to rollback.
    Stale 'processing' records can be cleaned up with cleanup_stale_processing_records().
    """
    location_id = os.environ["LOCATION_ID"]
    register_id = os.environ["REGISTER_ID"]
    year = payment_time.year

    logger.info(f"Starting fiscalization for payment {payment_id}, year: {year}")

    # Step 1: Reserve receipt number by inserting record with 'processing' status
    receipt_number = reserve_receipt_number(
        year,
        location_id,
        register_id,
        invoice_id,
        payment_id,
        payment_amount,
        payment_currency,
        payment_time,
        shared_folder_path,
    )

    try:
        # Step 2: Perform fiscalization
        result = fiscalize(payment_time, payment_amount, receipt_number, shared_folder_path)
        logger.info(f"Fiscalization result: {result}")

        # Step 3: Update record with fiscalization results
        if result.get("JIR"):
            update_receipt_with_fiscalization(payment_id, result.get("ZKI"), result.get("JIR"), "completed")
            logger.info(f"Fiscalization completed successfully for payment {payment_id}")
        else:
            update_receipt_with_fiscalization(payment_id, result.get("ZKI"), None, "failed")
            logger.warning(f"Fiscalization failed - no JIR received for payment {payment_id}")

        return result

    except Exception as e:
        # Step 4: Mark as failed if any exception occurs
        logger.error(f"Fiscalization failed for payment {payment_id}: {e}")
        try:
            # Try to preserve any ZKI that was generated before failure
            zki = None
            if hasattr(e, "zki") and e.zki:
                zki = e.zki
            update_receipt_with_fiscalization(payment_id, zki, None, "failed")
            logger.info(f"Marked receipt as failed for payment {payment_id}")
        except Exception as update_error:
            logger.error(f"Failed to update receipt status to failed: {update_error}")
            # This is critical - we have a receipt in 'processing' state that can't be updated
            # Log extensively for manual intervention
            logger.critical(
                f"ORPHANED RECEIPT: payment_id={payment_id}, receipt_number={receipt_number}, "
                f"status=processing - manual database intervention required"
            )

        # Re-raise the original exception with context
        raise ValueError(f"Fiscalization failed for payment {payment_id}: {str(e)}") from e


def extract_cert_key(p12_path, password):
    with open(p12_path, "rb") as f:
        p12_data = f.read()
    priv_key, cert, _ = pkcs12.load_key_and_certificates(p12_data, password.encode(), backend=default_backend())
    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = priv_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return cert_pem, key_pem, priv_key


def generate_zki(oib, dt, br, pos, ur, amount, private_key):
    data = f"{oib}{dt}{br}{pos}{ur}{float(amount):.2f}"
    signed = private_key.sign(data.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    return hashlib.md5(signed).hexdigest()


def build_receipt(message_id, node_id, request_time, payment_time, zki, total, receipt_number, config):
    receipt = f"""<tns:RacunZahtjev xmlns:tns="http://www.apis-it.hr/fin/2012/types/f73"
             Id="{node_id}"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             xsi:schemaLocation="http://www.apis-it.hr/fin/2012/types/f73 ../schema/FiskalizacijaSchema.xsd">
        <tns:Zaglavlje>
            <tns:IdPoruke>{message_id}</tns:IdPoruke>
            <tns:DatumVrijeme>{request_time}</tns:DatumVrijeme>
        </tns:Zaglavlje>
        <tns:Racun>
            <tns:Oib>{config['oib_company']}</tns:Oib>
            <tns:USustPdv>true</tns:USustPdv>
            <tns:DatVrijeme>{payment_time}</tns:DatVrijeme>
            <tns:OznSlijed>P</tns:OznSlijed>
            <tns:BrRac>
                <tns:BrOznRac>{receipt_number}</tns:BrOznRac>
                <tns:OznPosPr>{config['location_id']}</tns:OznPosPr>
                <tns:OznNapUr>{config['register_id']}</tns:OznNapUr>
            </tns:BrRac>
            <tns:IznosOslobPdv>{total}</tns:IznosOslobPdv>
            <tns:IznosUkupno>{total}</tns:IznosUkupno>
            <tns:NacinPlac>K</tns:NacinPlac>
            <tns:OibOper>{config['oib_operator']}</tns:OibOper>
            <tns:ZastKod>{zki}</tns:ZastKod>
            <tns:NakDost>false</tns:NakDost>
        </tns:Racun>
    </tns:RacunZahtjev>"""

    return receipt


def sign_with_cert(xml_string, cert_pem, key_pem, node_id):
    parser = ET.XMLParser(remove_blank_text=True)
    root = ET.fromstring(xml_string.encode(), parser)

    root.set("Id", node_id)

    xmlsec.tree.add_ids(root, ["Id"])

    signature = xmlsec.template.create(root, xmlsec.Transform.EXCL_C14N, xmlsec.Transform.RSA_SHA1)

    ref = xmlsec.template.add_reference(signature, xmlsec.Transform.SHA1, uri=f"#{node_id}")
    xmlsec.template.add_transform(ref, xmlsec.Transform.ENVELOPED)
    xmlsec.template.add_transform(ref, xmlsec.Transform.EXCL_C14N)

    key_info = xmlsec.template.ensure_key_info(signature)
    x509_data = xmlsec.template.add_x509_data(key_info)
    xmlsec.template.x509_data_add_certificate(x509_data)

    cert_obj = x509.load_pem_x509_certificate(cert_pem, default_backend())
    issuer_el = ET.SubElement(x509_data, "{http://www.w3.org/2000/09/xmldsig#}X509IssuerSerial")
    name_el = ET.SubElement(issuer_el, "{http://www.w3.org/2000/09/xmldsig#}X509IssuerName")
    name_el.text = cert_obj.issuer.rfc4514_string()
    serial_el = ET.SubElement(issuer_el, "{http://www.w3.org/2000/09/xmldsig#}X509SerialNumber")
    serial_el.text = str(cert_obj.serial_number)

    root.append(signature)
    ctx = xmlsec.SignatureContext()
    key = xmlsec.Key.from_memory(key_pem, xmlsec.KeyFormat.PEM)
    key.load_cert_from_memory(cert_pem, xmlsec.KeyFormat.PEM)
    ctx.key = key
    ctx.sign(signature)

    return ET.tostring(root, encoding="utf-8").decode()


def wrap_soap(xml_signed):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
  {xml_signed}
  </soapenv:Body>
</soapenv:Envelope>"""


def fiscalize_request(payload, config, cert_pem, key_pem):
    import glob
    import tempfile

    headers = {"Content-Type": "text/xml; charset=utf-8"}

    # Combine CA certificates from the directory
    ca_dir = os.environ.get("FINA_CA_DIR_PATH")
    if not ca_dir:
        raise ValueError("FINA_CA_DIR_PATH environment variable is required for SSL verification")

    ca_pem_files = glob.glob(os.path.join(ca_dir, "*.pem"))
    if not ca_pem_files:
        raise ValueError(f"No .pem files found in {ca_dir}")

    # Read and combine all CA certificates
    combined_ca = b""
    for ca_file in sorted(ca_pem_files):
        with open(ca_file, "rb") as f:
            combined_ca += f.read()
            combined_ca += b"\n"  # Ensure separation between certificates

    logger.info(f"Loaded {len(ca_pem_files)} CA certificate(s) from {ca_dir}")

    # Use in-memory temporary files that are automatically cleaned up
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".pem") as cert_file, tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem"
    ) as key_file, tempfile.NamedTemporaryFile(mode="wb", suffix=".pem") as ca_bundle_file:
        cert_file.write(cert_pem)
        key_file.write(key_pem)
        ca_bundle_file.write(combined_ca)
        cert_file.flush()
        key_file.flush()
        ca_bundle_file.flush()

        r = requests.post(
            config["fina_endpoint"],
            data=payload.encode(),
            headers=headers,
            cert=(cert_file.name, key_file.name),
            verify=ca_bundle_file.name,
        )

        logger.info(f"📤 Sent to FINA: {r.status_code}")
        return r.text
        # Files are automatically deleted when exiting the 'with' block


def xml_to_yaml(xml_string: str):
    try:
        root = ET.fromstring(xml_string.encode("utf-8"))

        def element_to_dict(elem):
            children = list(elem)
            if not children:
                return elem.text
            result = {}
            for child in children:
                tag = child.tag.split("}")[-1]
                result[tag] = element_to_dict(child)
            return result

        root_tag = root.tag.split("}")[-1]
        data = {root_tag: element_to_dict(root)}

        return yaml.dump(data, allow_unicode=True)

    except Exception as e:
        logger.error(f"❌ Failed to convert XML to YAML: {e}")


def soap_response_to_yaml(xml_string):
    root = ET.fromstring(xml_string.encode("utf-8"))
    body = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Body")
    if body is None or not list(body):
        raise ValueError("No SOAP Body found or it is empty")

    main_node = list(body)[0]

    def element_to_dict(elem):
        if elem.tag.endswith("Signature"):
            return None
        children = list(elem)
        if not children:
            return elem.text
        return {
            child.tag.split("}")[-1]: element_to_dict(child) for child in children if element_to_dict(child) is not None
        }

    data = {main_node.tag.split("}")[-1]: element_to_dict(main_node)}

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def extract_jir(xml_string):
    root = ET.fromstring(xml_string.encode("utf-8"))
    body = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Body")
    if body is not None:
        for el in body.iter():
            if el.tag.endswith("Jir"):
                return el.text
    return None


def fiscalize(payment_time, payment_amount, receipt_number, shared_folder_path) -> dict:
    config = get_config()

    # Format payment time (original transaction time)
    payment_time_xml = payment_time.strftime("%d.%m.%YT%H:%M:%S")
    payment_time_zki = payment_time.strftime("%Y%m%d_%H%M%S")

    # Get current time for request timestamp (Zaglavlje)
    fina_timezone = ZoneInfo(os.environ["FINA_TIMEZONE"])
    request_time = datetime.now(fina_timezone)
    request_time_xml = request_time.strftime("%d.%m.%YT%H:%M:%S")

    try:
        cert_pem, key_pem, private_key = extract_cert_key(config["p12_path"], config["p12_password"])
        logger.info("Certificate and private key extracted successfully")
    except Exception as e:
        logger.error(f"Failed to extract certificate/key: {e}")
        raise ValueError(f"Certificate extraction failed: {e}")

    try:
        zki = generate_zki(
            config["oib_company"],
            payment_time_zki,
            receipt_number,
            config["location_id"],
            config["register_id"],
            payment_amount,
            private_key,
        )
        logger.info(f"Generated ZKI: {zki}")
    except Exception as e:
        logger.error(f"Failed to generate ZKI: {e}")
        raise ValueError(f"ZKI generation failed: {e}")

    request_id = str(uuid.uuid4())
    signature_node_id = f"G{uuid.uuid4().hex[:15]}"

    try:
        receipt_content = build_receipt(
            request_id,
            signature_node_id,
            request_time_xml,
            payment_time_xml,
            zki,
            f"{payment_amount:.2f}",
            receipt_number,
            config,
        )
        receipt_signed = sign_with_cert(receipt_content, cert_pem, key_pem, signature_node_id)
        receipt = wrap_soap(receipt_signed)
        logger.info("Receipt built and signed successfully")
    except Exception as e:
        logger.error(f"Failed to build/sign receipt: {e}")
        raise ValueError(f"Receipt building failed: {e}")

    try:
        response = fiscalize_request(receipt, config, cert_pem, key_pem)
        logger.info("FINA request completed successfully")
    except Exception as e:
        logger.error(f"FINA request failed: {e}")
        raise ValueError(f"FINA communication failed: {e}")

    # Extract JIR before saving files
    jir = extract_jir(response)

    # Save fiscal receipt files to S3 - supplementary audit trail
    # NOTE: S3 storage is NOT critical. JIR and ZKI in database are sufficient for compliance.
    # S3 failures should NOT fail the fiscalization process - log only.
    logger.info(f"Saving fiscal receipt files to S3: {shared_folder_path}")
    try:
        s3_results = []
        s3_results.append(save_file_to_s3(receipt, f"{shared_folder_path}/{FILE_REQUEST}.xml"))
        s3_results.append(save_file_to_s3(xml_to_yaml(receipt_content), f"{shared_folder_path}/{FILE_REQUEST}.yaml"))
        s3_results.append(save_file_to_s3(response, f"{shared_folder_path}/{FILE_RESPONSE}.xml"))
        s3_results.append(
            save_file_to_s3(soap_response_to_yaml(response), f"{shared_folder_path}/{FILE_RESPONSE}.yaml")
        )

        if not all(s3_results):
            logger.warning("Some S3 file saves failed - supplementary audit files missing but fiscalization successful")
        else:
            logger.info("All fiscal receipt files saved to S3 successfully")
    except Exception as e:
        logger.warning(f"S3 file saving failed: {e} - supplementary audit files missing but fiscalization successful")

    return {"payment_amount": payment_amount, "ZKI": zki, "JIR": jir}
