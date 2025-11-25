#!/usr/bin/env python3
"""
Temporary utility to test SSL connection to FINA endpoint with CA certificate verification.
Tests only the SSL handshake without sending any data.

Usage:
    python test_ssl_connection.py --ca-dir cert/ca \\
        --endpoint https://cis.porezna-uprava.hr:8449/FiskalizacijaService
    python test_ssl_connection.py --ca-dir cert/ca_demo \\
        --endpoint https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest
"""

import argparse
import glob
import os
import sys
import tempfile
from urllib.parse import urlparse

import requests


def test_ssl_connection(ca_dir: str, endpoint: str) -> bool:
    """
    Test SSL connection to FINA endpoint using CA certificates.
    Returns True if connection succeeds, False otherwise.
    """
    print(f"🔍 Testing SSL connection to: {endpoint}")
    print(f"📁 Using CA certificates from: {ca_dir}")
    print()

    # Find all .pem files in the CA directory
    ca_pem_files = glob.glob(os.path.join(ca_dir, "*.pem"))
    if not ca_pem_files:
        print(f"❌ No .pem files found in {ca_dir}")
        return False

    print(f"📜 Found {len(ca_pem_files)} CA certificate(s):")
    for ca_file in sorted(ca_pem_files):
        print(f"   - {os.path.basename(ca_file)}")
    print()

    # Read and combine all CA certificates
    combined_ca = b""
    for ca_file in sorted(ca_pem_files):
        with open(ca_file, "rb") as f:
            combined_ca += f.read()
            combined_ca += b"\n"

    # Create temporary CA bundle file
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False) as ca_bundle_file:
        ca_bundle_file.write(combined_ca)
        ca_bundle_path = ca_bundle_file.name

    try:
        print("🔐 Testing SSL handshake...")

        # Try to establish SSL connection without sending actual data
        # We use a simple GET request with a short timeout
        response = requests.get(
            endpoint,
            verify=ca_bundle_path,
            timeout=10,
        )

        print("✅ SSL handshake successful!")
        print(f"   Status code: {response.status_code}")
        print(f"   Server responded: {len(response.content)} bytes")

        # Show SSL certificate info if available
        try:
            if hasattr(response, "raw") and hasattr(response.raw, "connection") and response.raw.connection:
                sock = response.raw.connection.sock
                if sock and hasattr(sock, "getpeercert"):
                    cert = sock.getpeercert()
                    if cert:
                        print()
                        print("📋 Server certificate info:")
                        subject = dict(x[0] for x in cert.get("subject", []))
                        issuer = dict(x[0] for x in cert.get("issuer", []))
                        print(f"   Subject: {subject.get('commonName', 'N/A')}")
                        print(f"   Issuer: {issuer.get('commonName', 'N/A')}")
                        print(f"   Valid until: {cert.get('notAfter', 'N/A')}")
        except Exception:
            # Ignore certificate info extraction errors - handshake success is what matters
            pass

        return True

    except requests.exceptions.SSLError as e:
        print("❌ SSL verification failed!")
        print(f"   Error: {e}")
        print()
        print("💡 Possible reasons:")
        print("   - CA certificates are incorrect or incomplete")
        print("   - Server certificate is not signed by the provided CA")
        print("   - CA certificates are in wrong format")
        print("   - Certificate chain is incomplete")
        return False

    except requests.exceptions.ConnectionError as e:
        print("❌ Connection failed!")
        print(f"   Error: {e}")
        print()
        print("💡 Possible reasons:")
        print("   - Endpoint URL is incorrect")
        print("   - Server is not accessible from this network")
        print("   - Firewall blocking the connection")
        return False

    except requests.exceptions.Timeout as e:
        print("⏱️  Connection timeout!")
        print(f"   Error: {e}")
        print()
        print("💡 Server might be slow or unreachable")
        return False

    except Exception as e:
        print("❌ Unexpected error!")
        print(f"   Error: {e}")
        return False

    finally:
        # Clean up temporary file
        try:
            os.unlink(ca_bundle_path)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Test SSL connection to FINA endpoint with CA certificate verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test demo endpoint
  python test_ssl_connection.py --ca-dir cert/ca_demo \\
      --endpoint https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest

  # Test production endpoint
  python test_ssl_connection.py --ca-dir cert/ca \\
      --endpoint https://cis.porezna-uprava.hr:8449/FiskalizacijaService
        """,
    )

    parser.add_argument(
        "--ca-dir",
        type=str,
        required=True,
        help="Directory containing CA certificate .pem files",
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        required=True,
        help="FINA endpoint URL to test",
    )

    args = parser.parse_args()

    # Validate CA directory exists
    if not os.path.isdir(args.ca_dir):
        print(f"❌ Directory not found: {args.ca_dir}")
        return 1

    # Validate endpoint URL
    parsed = urlparse(args.endpoint)
    if not parsed.scheme or not parsed.netloc:
        print(f"❌ Invalid endpoint URL: {args.endpoint}")
        return 1

    # Run the test
    success = test_ssl_connection(args.ca_dir, args.endpoint)

    print()
    if success:
        print("🎉 SSL connection test PASSED")
        return 0
    else:
        print("💔 SSL connection test FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
