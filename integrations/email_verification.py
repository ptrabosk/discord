import os
import smtplib
import ssl
from email.message import EmailMessage

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
    # Without an explicit context, starttls() does not verify the server certificate.
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        username = os.getenv("SMTP_USERNAME")
        password = os.getenv("SMTP_PASSWORD")
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
