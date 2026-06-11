"""
adobe_pdf_service.py — Adobe PDF Services integration for Praesidium.
Handles PDF-to-DOCX conversion via Adobe's Python SDK.

Deploy to: /app/modules/dms/services/adobe_pdf_service.py
"""
from __future__ import annotations
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _get_credentials():
    """Load Adobe credentials from credentials_vault or env vars."""
    client_id = os.environ.get("PDF_SERVICES_CLIENT_ID", "")
    client_secret = os.environ.get("PDF_SERVICES_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        # Try credentials_vault
        try:
            import psycopg2
            conn = psycopg2.connect(
                host='172.28.0.1', port=5432,
                user='praesidium', password='hS8_3bJg6NImUwFpFVJ4',
                dbname='praesidium'
            )
            cur = conn.cursor()
            cur.execute("""
                SELECT encrypted_key FROM credentials_vault
                WHERE provider = 'adobe_pdf_services'
                  AND key_type = 'client_id'
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                client_id = row[0]

            cur.execute("""
                SELECT encrypted_key FROM credentials_vault
                WHERE provider = 'adobe_pdf_services'
                  AND key_type = 'client_secret'
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                client_secret = row[0]

            conn.close()
        except Exception as e:
            log.warning("Could not load Adobe creds from vault: %s", e)

    return client_id, client_secret


def convert_pdf_to_docx(pdf_path: str, output_dir: str = None) -> dict:
    """Convert a PDF file to DOCX using Adobe PDF Services API.

    Args:
        pdf_path: Absolute path to source PDF file
        output_dir: Directory for output DOCX (defaults to same dir as source)

    Returns:
        dict with keys: success, output_path, error, filename
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {"success": False, "error": f"File not found: {pdf_path}"}
    if pdf_path.suffix.lower() != '.pdf':
        return {"success": False, "error": "Not a PDF file"}

    client_id, client_secret = _get_credentials()
    if not client_id or not client_secret:
        return {"success": False, "error": "Adobe PDF Services credentials not configured. Go to Connectors to set up."}

    try:
        from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
        from adobe.pdfservices.operation.exception.exceptions import ServiceApiException, ServiceUsageException, SdkException
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
        from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
        from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
        from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
        from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult
    except ImportError:
        return {"success": False, "error": "Adobe PDF Services SDK not installed. Run: pip install pdfservices-sdk"}

    if output_dir is None:
        output_dir = str(pdf_path.parent)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    docx_name = pdf_path.stem + '.docx'
    output_path = output_dir / docx_name

    # Avoid overwriting — add suffix if exists
    counter = 1
    while output_path.exists():
        docx_name = f"{pdf_path.stem} ({counter}).docx"
        output_path = output_dir / docx_name
        counter += 1

    try:
        credentials = ServicePrincipalCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        pdf_services = PDFServices(credentials=credentials)

        # Upload PDF
        with open(pdf_path, 'rb') as f:
            input_asset = pdf_services.upload(
                input_stream=f,
                mime_type=PDFServicesMediaType.PDF
            )

        # Create export job
        export_params = ExportPDFParams(target_format=ExportPDFTargetFormat.DOCX)
        export_job = ExportPDFJob(input_asset=input_asset, params=export_params)

        # Submit and poll
        location = pdf_services.submit(job=export_job)
        response = pdf_services.get_job_result(
            location=location,
            result_type=ExportPDFResult
        )

        # Download result
        result_asset = response.result.asset
        stream_asset = pdf_services.get_content(asset=result_asset)

        with open(output_path, 'wb') as out:
            out.write(stream_asset.input_stream.read())

        log.info("Adobe PDF->DOCX: %s -> %s", pdf_path.name, output_path.name)
        return {
            "success": True,
            "output_path": str(output_path),
            "filename": docx_name,
        }

    except ServiceApiException as e:
        log.error("Adobe API error: %s", e)
        return {"success": False, "error": f"Adobe API error: {e}"}
    except ServiceUsageException as e:
        log.error("Adobe usage limit: %s", e)
        return {"success": False, "error": f"Adobe usage limit exceeded: {e}"}
    except SdkException as e:
        log.error("Adobe SDK error: %s", e)
        return {"success": False, "error": f"Adobe SDK error: {e}"}
    except Exception as e:
        log.error("PDF conversion error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def get_status() -> dict:
    """Check if Adobe PDF Services is configured and reachable."""
    client_id, client_secret = _get_credentials()
    configured = bool(client_id and client_secret)
    sdk_installed = False
    try:
        import adobe.pdfservices
        sdk_installed = True
    except ImportError:
        pass

    return {
        "configured": configured,
        "sdk_installed": sdk_installed,
        "client_id_hint": client_id[:8] + '...' if client_id and len(client_id) > 8 else None,
    }
