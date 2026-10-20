# Resend setup

Delivery is disabled by default. Configure your own sender and recipient in a protected environment file:

```dotenv
EMAIL_ENABLED=true
EMAIL_PROVIDER=resend
RESEND_API_KEY=
EMAIL_FROM=
EMAIL_TO=
SMARTMAIL_ENABLED=true
SMARTMAIL_MODE=shadow
```

Use a sender allowed by your Resend account. Keep the API key outside source control. For an interactive Linux deployment, `tools/set_resend_key.py` accepts the key with a hidden terminal prompt and writes it to a protected environment file. Review the script before running it.

```bash
python3 tools/set_resend_key.py --env-file /etc/kap-bildirim.env
```

Check shadow-mode decisions before selecting `p0` or `live` in your environment file. The helper’s optional `--activate-p0` flag sends a test email to the configured recipient, enables P0 mode, and restarts the systemd service; use it only when you intend those actions.

Resend accepting a request is not proof of mailbox delivery. Inspect provider delivery events when validating a deployment. Provider account limits and sender restrictions still apply.
