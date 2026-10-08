# Security Policy

## Reporting a Vulnerability

We take the security of this project seriously. If you discover a vulnerability or security flaw, please **do not report it through public GitHub issues or public channels**.

### Preferred Reporting Method

1. **GitHub Private Vulnerability Reporting (PVR)**:
   - Navigate to the repository's **Security** tab on GitHub.
   - Click **Report a vulnerability** under "Private vulnerability reporting".
   - Fill in the advisory form with detailed reproduction steps, impact, and proof of concept.

2. **Email Disclosure**:
   - Alternatively, email `security@brainstormity.com` with the subject `[SECURITY] Jev X Sentiment Analysis Vulnerability`.
   - Include:
     - Clear description of the vulnerability
     - Affected component(s)
     - Step-by-step reproduction instructions or PoC
     - Potential remediation suggestions

### Response Timeline

- **Initial Acknowledgment**: Within 48 hours.
- **Triage & Assessment**: Within 5 business days.
- **Fix & Advisory Release**: Coordinated with the reporter before public disclosure.

---

## Security Practices for Self-Hosting

1. **Network Binding**:
   - Use `sh scripts/launch_local.sh` (explicit 127.0.0.1, one worker, no forwarded-header interpretation), or use `uvicorn app.main:app --host 127.0.0.1 --port 8787 --no-proxy-headers` with matching exact ALLOWED_HOSTS/ALLOWED_ORIGINS. HOST in configuration does not set the socket binding.
   - If binding to `0.0.0.0`, configure a reverse proxy (e.g. Caddy, Nginx) with TLS and authentication.

2. **Settings Endpoint Protection**:
   - All analysis, market and settings APIs require a configured ADMIN_TOKEN, even from loopback. Without one only direct numeric loopback requests with allowed Host/Origin and no forwarding headers are accepted.
   - For every proxy or remote access, set `ADMIN_TOKEN=<random-secret>` in `.env` and pass `X-Admin-Token: <random-secret>` in requests.

3. **CORS Restrictions**:
   - Specify your exact frontend domain(s) in `ALLOWED_ORIGINS` (comma-separated).
   - Never use wildcard `*` with authenticated credentials in production.

A proxy that removes every forwarding indication cannot be distinguished from a local client. A token is mandatory for every proxy; CORS does not replace authentication. Configure ALLOWED_HOSTS and exact ALLOWED_ORIGINS. The UI retains the token only in page memory.

The local launcher configures exact loopback Origins for its --port before app import. Diagnostic clients can explicitly read ADMIN_TOKEN via --token-env or --token-stdin without printing or persisting it; never pass the value in argv.

The local launcher is only for direct local use: it overrides ALLOWED_HOSTS and ALLOWED_ORIGINS in runtime, ignoring configured proxy domains without modifying the configuration file. For a proxy, start uvicorn directly with an explicit appropriate bind, exact Host/Origin allowlists and ADMIN_TOKEN; do not use the local launcher for proxy configuration.
