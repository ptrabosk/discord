import os
import smtplib
import logging
from email.message import EmailMessage


logger = logging.getLogger(__name__)


def send_verification_email(recipient, code):
    msg = EmailMessage()
    msg["Subject"] = "Discord verification"
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = recipient
    msg.set_content(
        f"Your Discord verification code is:\n\n{code}\n\n"
        "This code expires in 10 minutes.\n\n"
        "If you did not request this code, you can ignore this email."
    )
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            username = os.getenv("SMTP_USERNAME")
            password = os.getenv("SMTP_PASSWORD")
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    except Exception:
        logger.exception("SMTP verification delivery failed")
        raise
