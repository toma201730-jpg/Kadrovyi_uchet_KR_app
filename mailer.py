"""Отправка email через SMTP. Настройка — переменные окружения:
SMTP_HOST, SMTP_FROM (обязательны), SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY (starttls | ssl | none)."""
import os, smtplib, ssl
from email.message import EmailMessage

def configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))

def send(recipients, subject, body):
    mode = os.environ.get("SMTP_SECURITY", "starttls").lower()
    port = int(os.environ.get("SMTP_PORT", 465 if mode == "ssl" else 587))
    host = os.environ["SMTP_HOST"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_FROM"], ", ".join(recipients), subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) if mode == "ssl" else smtplib.SMTP(host, port, timeout=20)
    with server:
        if mode == "starttls":
            server.starttls(context=ctx)
        if os.environ.get("SMTP_USER"):
            server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)
