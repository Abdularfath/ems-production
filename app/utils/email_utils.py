import os
import base64
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition


def send_ticket_email(to_email, subject, html_content):
    """Sends an HTML email using SendGrid."""
    sg_api_key = os.getenv('SENDGRID_API_KEY')
    from_email = os.getenv('SENDGRID_SENDER_EMAIL')

    print(f"[DEBUG EMAIL] Starting email send to {to_email}", flush=True)
    print(f"[DEBUG EMAIL] API Key exists: {bool(sg_api_key)}", flush=True)
    print(f"[DEBUG EMAIL] Sender Email: {from_email}", flush=True)

    if not sg_api_key or not from_email:
        print("[ERROR EMAIL] SendGrid API Key or Sender Email is missing in .env", flush=True)
        return False

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )

    try:
        sg = SendGridAPIClient(sg_api_key)
        response = sg.send(message)
        print(f"[INFO EMAIL] Email sent successfully! Status Code: {response.status_code}", flush=True)
        return True
    except Exception as e:
        print(f"[ERROR EMAIL] Failed to send email: {str(e)}", flush=True)
        if hasattr(e, 'body'):
            print(f"[ERROR EMAIL] SendGrid API Response: {e.body}", flush=True)
        return False  # was "return Fals" — fixed


def send_email_with_attachment(to_email, subject, html_content, attachment_bytes,
                                attachment_filename, mime_type='application/pdf'):
    """
    Sends an HTML email with a single file attached (e.g. a certificate PDF).
    attachment_bytes: raw bytes of the file (not base64-encoded yet).
    """
    sg_api_key = os.getenv('SENDGRID_API_KEY')
    from_email = os.getenv('SENDGRID_SENDER_EMAIL')

    if not sg_api_key or not from_email:
        print("[ERROR EMAIL] SendGrid API Key or Sender Email is missing in .env", flush=True)
        return False

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )

    encoded = base64.b64encode(attachment_bytes).decode()
    attachment = Attachment(
        FileContent(encoded),
        FileName(attachment_filename),
        FileType(mime_type),
        Disposition('attachment')
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(sg_api_key)
        response = sg.send(message)
        print(f"[INFO EMAIL] Email with attachment sent! Status Code: {response.status_code}", flush=True)
        return True
    except Exception as e:
        print(f"[ERROR EMAIL] Failed to send email with attachment: {str(e)}", flush=True)
        if hasattr(e, 'body'):
            print(f"[ERROR EMAIL] SendGrid API Response: {e.body}", flush=True)
        return False