# MTA SMTP Testing Guide — Apache James on GDC

Testing email delivery from Kubernetes pods to the Apache James Mail Transfer Agent (MTA)
deployed on Google Distributed Cloud.

## Prerequisites

| Item | Details |
|------|---------|
| **MTA** | Apache James (Spring Boot) deployed and running in GDC |
| **Protocol** | SMTP (port 25 or 587) |
| **Cluster Access** | `kubectl` access to the namespace where James is deployed |

Before testing, identify your James service:

```bash
# Find the James service name and ports
kubectl get svc -n YOUR_NAMESPACE | grep james

# Example output:
# james-smtp-service   ClusterIP   10.96.x.x   <none>   25/TCP,587/TCP,993/TCP   5d
```

> [!IMPORTANT]
> Replace `james-service` in all examples below with your actual James ClusterIP service name.
> Replace `yourdomain.com` with the domain configured in your James server.

---

## Step 1: Verify Connectivity

Before sending email, confirm the pod can reach the James SMTP service.

### DNS Resolution

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nslookup james-service
```

### Port Check

```bash
# Check SMTP port (25)
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 25

# Check submission port (587) — typically requires STARTTLS + auth
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 587

# Check IMAP port (993) — for reading mail after sending
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 993
```

**Expected output:**
```
james-service (10.96.x.x:25) open
```

> [!WARNING]
> If you get `connection refused` or `timed out`, check:
> - Is the James pod running? `kubectl get pods -n YOUR_NAMESPACE | grep james`
> - Is the service targeting the right pods? `kubectl describe svc james-service -n YOUR_NAMESPACE`
> - Are NetworkPolicies blocking traffic? `kubectl get networkpolicies -n YOUR_NAMESPACE`

---

## Step 2: Test Sending Email

### Option A: Raw SMTP via Netcat (no dependencies)

The most basic test — talks directly to the SMTP server using the SMTP protocol:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- /bin/sh -c '
(
echo "EHLO testclient"
sleep 1
echo "MAIL FROM:<test@yourdomain.com>"
sleep 1
echo "RCPT TO:<recipient@yourdomain.com>"
sleep 1
echo "DATA"
sleep 1
echo "Subject: SMTP Test from GDC Pod"
echo "From: test@yourdomain.com"
echo "To: recipient@yourdomain.com"
echo "Date: $(date -R)"
echo "Message-ID: <test-$(date +%s)@yourdomain.com>"
echo ""
echo "This is a test email sent directly from a Kubernetes pod"
echo "in the GDC cluster using raw SMTP."
echo "."
sleep 1
echo "QUIT"
) | nc james-service 25
'
```

**Expected output:**
```
220 james-service SMTP Server ready
250-james-service Hello testclient
250 OK
250 2.1.0 Sender <test@yourdomain.com> OK
250 2.1.5 Recipient <recipient@yourdomain.com> OK
354 Start mail input; end with <CRLF>.<CRLF>
250 2.6.0 Message received
221 2.0.0 james-service closing connection
```

### Option B: Python `smtplib` (recommended)

More robust and supports TLS/auth. Works from any pod with Python installed:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- python3 -c "
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────
SMTP_HOST = 'james-service'    # Your James ClusterIP service name
SMTP_PORT = 25                 # 25 for relay, 587 for submission
USE_TLS   = False              # Set True if James requires STARTTLS
AUTH_USER  = ''                # Set if authentication is required
AUTH_PASS  = ''                # Set if authentication is required
SENDER    = 'test@yourdomain.com'
RECIPIENT = 'recipient@yourdomain.com'
# ──────────────────────────────────────────────────────────────

msg = MIMEMultipart()
msg['Subject'] = f'SMTP Test from GDC Pod — {datetime.now():%Y-%m-%d %H:%M}'
msg['From'] = SENDER
msg['To'] = RECIPIENT
msg.attach(MIMEText(
    'This is a test email sent from a Kubernetes pod in the GDC cluster.\n\n'
    f'Timestamp: {datetime.now().isoformat()}\n'
    f'SMTP Host: {SMTP_HOST}:{SMTP_PORT}\n'
    f'TLS: {USE_TLS}\n',
    'plain'
))

try:
    print(f'Connecting to {SMTP_HOST}:{SMTP_PORT} ...')
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
    s.ehlo()
    print(f'  EHLO OK')

    if USE_TLS:
        s.starttls()
        s.ehlo()
        print(f'  STARTTLS OK')

    if AUTH_USER:
        s.login(AUTH_USER, AUTH_PASS)
        print(f'  AUTH OK')

    s.sendmail(SENDER, [RECIPIENT], msg.as_string())
    print(f'✅ Email sent successfully to {RECIPIENT}')
    s.quit()
except smtplib.SMTPAuthenticationError as e:
    print(f'❌ Authentication failed: {e}')
    print(f'   Check username/password or auth method')
except smtplib.SMTPRecipientsRefused as e:
    print(f'❌ Recipient refused: {e}')
    print(f'   Does the mailbox exist in James? Create it via James admin API')
except smtplib.SMTPException as e:
    print(f'❌ SMTP error: {e}')
except Exception as e:
    print(f'❌ Connection error: {e}')
"
```

### Option C: `swaks` — Swiss Army Knife for SMTP

Launch a temporary debug pod with `swaks` pre-installed:

```bash
# Basic test (no auth)
kubectl run smtp-test --rm -it --restart=Never \
  --image=jetbrainsinfra/swaks -n YOUR_NAMESPACE -- \
  --to recipient@yourdomain.com \
  --from test@yourdomain.com \
  --server james-service \
  --port 25 \
  --body "Test email from GDC pod via swaks" \
  --header "Subject: Swaks SMTP Test"
```

```bash
# With authentication (port 587 + STARTTLS)
kubectl run smtp-test --rm -it --restart=Never \
  --image=jetbrainsinfra/swaks -n YOUR_NAMESPACE -- \
  --to recipient@yourdomain.com \
  --from test@yourdomain.com \
  --server james-service \
  --port 587 \
  --tls \
  --auth LOGIN \
  --auth-user "testuser" \
  --auth-password "testpass" \
  --body "Authenticated test email from GDC" \
  --header "Subject: Auth SMTP Test"
```

### Option D: `curl` (SMTP URL mode)

If `curl` is available in the pod:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- \
  curl --url "smtp://james-service:25" \
       --mail-from "test@yourdomain.com" \
       --mail-rcpt "recipient@yourdomain.com" \
       -T - <<< "Subject: Curl SMTP Test

Test email sent via curl from a GDC pod."
```

---

## Step 3: Verify Email Delivery

### Check James Logs

```bash
kubectl logs deployment/james-server -n YOUR_NAMESPACE --tail=50 | grep -i "mail\|smtp\|deliver"
```

### Check Mailbox via IMAP (if IMAP is enabled)

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- python3 -c "
import imaplib

# Connect to James IMAP
imap = imaplib.IMAP4_SSL('james-service', 993)
imap.login('recipient@yourdomain.com', 'password')
imap.select('INBOX')

# List recent messages
status, messages = imap.search(None, 'ALL')
msg_ids = messages[0].split()
print(f'📬 {len(msg_ids)} messages in INBOX')

# Show the latest message subject
if msg_ids:
    status, data = imap.fetch(msg_ids[-1], '(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])')
    print(f'Latest: {data[0][1].decode()}')

imap.logout()
"
```

### Check via James Admin API

Apache James exposes an admin REST API (typically port 8000):

```bash
# List mailboxes for a user
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- \
  curl -s http://james-admin-service:8000/users/recipient@yourdomain.com/mailboxes | python3 -m json.tool

# Check mail queue
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- \
  curl -s http://james-admin-service:8000/mailQueues | python3 -m json.tool
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `Connection refused` on port 25 | James SMTP not listening or Service misconfigured | Check `kubectl get svc`, verify `targetPort` matches James config |
| `Relay access denied` | James not configured to relay for your sender domain | Add domain to James `domainlist.xml` or admin API |
| `Recipient refused` | Mailbox doesn't exist | Create user via James admin API: `curl -X PUT http://james-admin:8000/users/user@domain` |
| `STARTTLS required` | James requires TLS on port 587 | Use port 587 with TLS enabled, or port 25 without TLS |
| `Authentication required` | James requires auth for submission | Provide credentials via `s.login()` or `--auth` flag |
| Email sent but not received | Check mail queue for stuck messages | `curl http://james-admin:8000/mailQueues/spool` |
| `NetworkPolicy` blocking | Pod-to-pod traffic blocked | Check `kubectl get networkpolicies` and add allow rules |

---

## Common James SMTP Ports

| Port | Service | Auth Required | TLS |
|------|---------|--------------|-----|
| 25 | SMTP (relay) | Usually no | Optional STARTTLS |
| 587 | Submission | Yes | STARTTLS required |
| 465 | SMTPS | Yes | Implicit TLS |
| 993 | IMAPS | Yes | Implicit TLS |
| 8000 | Admin API | Depends on config | Optional |
