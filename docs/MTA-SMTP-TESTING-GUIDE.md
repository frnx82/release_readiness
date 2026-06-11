# MTA SMTP Testing Guide — Apache James on GDC

Testing email delivery from Kubernetes pods to the Apache James Mail Transfer Agent (MTA)
deployed on Google Distributed Cloud.

## Prerequisites

| Item | Details |
|------|---------|
| **MTA** | Apache James (Spring Boot) deployed and running in GDC |
| **Protocol** | SMTP submission on **port 587** (STARTTLS + auth) |
| **Cluster Access** | `kubectl` access to the namespace where James is deployed |

> [!CAUTION]
> **Port 25 is blocked in GDC.** Google Distributed Cloud (and most cloud environments) blocks
> outbound port 25 to prevent spam. Use **port 587** (submission) with STARTTLS + authentication instead.
> This has been verified in our GDC deployment.

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
# Check submission port (587) — this is the working port in GDC
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 587

# Check IMAP port (993) — for reading mail after sending
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 993

# Port 25 — typically BLOCKED in GDC / cloud environments
# kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- nc -zv james-service 25
```

**Expected output:**
```
james-service (10.96.x.x:587) open
```

> [!WARNING]
> If you get `connection refused` or `timed out`, check:
> - Is the James pod running? `kubectl get pods -n YOUR_NAMESPACE | grep james`
> - Is the service targeting the right pods? `kubectl describe svc james-service -n YOUR_NAMESPACE`
> - Are NetworkPolicies blocking traffic? `kubectl get networkpolicies -n YOUR_NAMESPACE`

---

## Step 2: Test Sending Email

> [!NOTE]
> All examples below use **port 587** (submission). Port 25 is blocked in GDC.
> Our James deployment does **not** require STARTTLS or authentication on port 587 —
> it accepts plain SMTP relay within the cluster.

### Quick Test (simplest — copy, paste, run)

Replace `james-service` and email addresses, then run:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- python3 -c "
import smtplib
from email.mime.text import MIMEText

msg = MIMEText('Test email from GDC pod via port 587')
msg['Subject'] = 'SMTP Test from K8s'
msg['From'] = 'test@yourdomain.com'
msg['To'] = 'recipient@yourdomain.com'

try:
    s = smtplib.SMTP('james-service', 587, timeout=10)
    s.ehlo()
    # No STARTTLS or AUTH needed — James accepts relay within the cluster
    s.sendmail(msg['From'], [msg['To']], msg.as_string())
    s.quit()
    print('✅ Email sent successfully!')
except Exception as e:
    print(f'❌ Failed: {e}')
"
```

> [!TIP]
> If this works, you're done! The options below provide more detailed logging and alternative tools.

---

### Option A: Python `smtplib` — Detailed with diagnostics

Works from any pod with Python installed. Uses port 587 with authentication:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- python3 -c "
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────
SMTP_HOST = 'james-service'    # Your James ClusterIP service name
SMTP_PORT = 587                # 587 = submission (port 25 is BLOCKED in GDC)
SENDER    = 'test@yourdomain.com'
RECIPIENT = 'recipient@yourdomain.com'
AUTH_USER = 'your-username'    # James auth credentials
AUTH_PASS = 'your-password'
# ──────────────────────────────────────────────────────────────

msg = MIMEMultipart()
msg['Subject'] = f'SMTP Test from GDC Pod — {datetime.now():%Y-%m-%d %H:%M}'
msg['From'] = SENDER
msg['To'] = RECIPIENT
msg.attach(MIMEText(
    'This is a test email sent from a Kubernetes pod in the GDC cluster.\n\n'
    f'Timestamp: {datetime.now().isoformat()}\n'
    f'SMTP Host: {SMTP_HOST}:{SMTP_PORT}\n',
    'plain'
))

try:
    print(f'Connecting to {SMTP_HOST}:{SMTP_PORT} ...')
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
    s.ehlo()
    print(f'  EHLO OK')
    # NOTE: STARTTLS is not supported by our James deployment.
    # If your James requires TLS, uncomment:
    # s.starttls()
    # s.ehlo()
    # print(f'  STARTTLS OK')
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

### Option B: Raw SMTP via Netcat (port 587)

Basic test using the SMTP protocol directly (note: STARTTLS not supported via netcat,
so this only works if James allows plaintext on 587 — otherwise use Option A):

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
echo ""
echo "This is a test email sent from a GDC pod."
echo "."
sleep 1
echo "QUIT"
) | nc james-service 587
'
```

### Option C: `swaks` — Swiss Army Knife for SMTP

Launch a temporary debug pod with `swaks` pre-installed:

```bash
# Port 587 with STARTTLS + authentication (recommended for GDC)
kubectl run smtp-test --rm -it --restart=Never \
  --image=jetbrainsinfra/swaks -n YOUR_NAMESPACE -- \
  --to recipient@yourdomain.com \
  --from test@yourdomain.com \
  --server james-service \
  --port 587 \
  --tls \
  --auth LOGIN \
  --auth-user "your-username" \
  --auth-password "your-password" \
  --body "Test email from GDC pod via swaks" \
  --header "Subject: Swaks SMTP Test"
```

### Option D: `curl` (SMTP URL mode)

If `curl` is available in the pod:

```bash
kubectl exec -it <any-pod> -n YOUR_NAMESPACE -- \
  curl --url "smtp://james-service:587" \
       --ssl-reqd \
       --mail-from "test@yourdomain.com" \
       --mail-rcpt "recipient@yourdomain.com" \
       --user "your-username:your-password" \
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
| `Connection refused` on port 25 | **Port 25 blocked in GDC** (spam prevention) | Use **port 587** with STARTTLS + auth instead |
| `Connection refused` on port 587 | James SMTP not listening or Service misconfigured | Check `kubectl get svc`, verify `targetPort` matches James config |
| `Relay access denied` | James not configured to relay for your sender domain | Add domain to James `domainlist.xml` or admin API |
| `Recipient refused` | Mailbox doesn't exist | Create user via James admin API: `curl -X PUT http://james-admin:8000/users/user@domain` |
| `STARTTLS required` | James requires TLS on port 587 | Use `s.starttls()` or `--tls` flag |
| `Authentication required` | James requires auth for submission | Provide credentials via `s.login()` or `--auth` flag |
| Email sent but not received | Check mail queue for stuck messages | `curl http://james-admin:8000/mailQueues/spool` |
| `NetworkPolicy` blocking | Pod-to-pod traffic blocked | Check `kubectl get networkpolicies` and add allow rules |

---

## James SMTP Ports — GDC Compatibility

| Port | Service | Auth | TLS | GDC Status |
|------|---------|------|-----|------------|
| 25 | SMTP (relay) | Usually no | Optional | ❌ **BLOCKED** |
| 587 | Submission | Yes | STARTTLS | ✅ **Working** |
| 465 | SMTPS | Yes | Implicit TLS | ⚠️ Check firewall |
| 993 | IMAPS | Yes | Implicit TLS | ✅ Working |
| 8000 | Admin API | Depends | Optional | ✅ Working |
