from email.message import EmailMessage
import hashlib
import json
import smtplib
import ssl
import urllib.error
import urllib.request

from .source import retry_after_seconds


class DeliveryError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, config):
        self.token = config.telegram_token
        self.timeout = config.timeout_seconds

    def call(self, method, payload):
        # Never include this URL, API response body or token in logs/exceptions.
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self.token + "/" + method,
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            wait = retry_after_seconds(exc.headers.get("Retry-After"))
            try:
                data = json.loads(exc.read(65536))
                parameter = data.get("parameters", {}).get("retry_after")
                if isinstance(parameter, int) and parameter > 0:
                    wait = max(wait or 0, parameter)
            except (ValueError, AttributeError, TypeError):
                pass
            if exc.code in {400, 401, 403, 404}:
                wait = max(wait or 0, 3600)
            raise DeliveryError("telegram_http_" + str(exc.code), wait) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeliveryError("telegram_transport_error") from None
        except (ValueError, UnicodeError):
            raise DeliveryError("telegram_invalid_json") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DeliveryError("telegram_api_rejected", 3600)
        return result.get("result")

    def send(self, destination, body):
        if len(body.encode("utf-16-le")) // 2 > 4096:
            raise DeliveryError("telegram_message_too_long", 3600)
        result = self.call("sendMessage", {"chat_id": destination, "text": body,
                            "link_preview_options": {"is_disabled": True}})
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise DeliveryError("telegram_invalid_send_response")
        return result["message_id"]


class Email:
    def __init__(self, config):
        self.config = config

    def send(self, destination, body):
        config = self.config
        message = EmailMessage()
        message["From"] = config.email_from
        message["To"] = destination
        message["Subject"] = body.splitlines()[0][:150]
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if config.smtp_tls == "ssl":
                client = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port,
                                         timeout=config.timeout_seconds, context=context)
            else:
                client = smtplib.SMTP(config.smtp_host, config.smtp_port,
                                     timeout=config.timeout_seconds)
            with client:
                if config.smtp_tls == "starttls":
                    client.starttls(context=context)
                if config.smtp_user:
                    client.login(config.smtp_user, config.smtp_password)
                refused = client.send_message(message)
                if refused:
                    raise DeliveryError("smtp_some_recipients_refused", 3600)
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError("smtp_authentication_failed", 3600) from None
        except (smtplib.SMTPException, OSError, TimeoutError):
            raise DeliveryError("smtp_delivery_error") from None


class Resend:
    def __init__(self, config):
        self.config = config

    def send(self, destination, body):
        payload = {"from": self.config.email_from, "to": [destination],
                   "subject": body.splitlines()[0][:150], "text": body}
        # Same recipient/payload retries are deduplicated by Resend for 24 hours.
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        request = urllib.request.Request("https://api.resend.com/emails",
            data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.config.resend_api_key,
                     "Content-Type": "application/json", "Idempotency-Key": key,
                     "User-Agent": "kap-bildirim-pilot/0.2"})
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                result = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            wait = retry_after_seconds(exc.headers.get("Retry-After"))
            if exc.code in {400, 401, 403, 404, 409, 422}:
                wait = max(wait or 0, 3600)
            # Never log response bodies: they can include account or message information.
            raise DeliveryError("resend_http_" + str(exc.code), wait) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DeliveryError("resend_transport_error") from None
        except (ValueError, UnicodeError):
            raise DeliveryError("resend_invalid_json") from None
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise DeliveryError("resend_invalid_send_response")
        return result["id"]


def email_sender(config):
    return Resend(config) if config.email_provider == "resend" else Email(config)


def senders(config):
    result = {}
    if config.telegram_enabled:
        result["telegram"] = Telegram(config)
    if config.email_enabled:
        result["email"] = email_sender(config)
    return result
