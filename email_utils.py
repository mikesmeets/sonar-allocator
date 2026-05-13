"""Shared SMTP email helper — reads config from the settings table at call time."""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _smtp_config():
    from app import get_setting
    return {
        'host':       get_setting('smtp_host', ''),
        'port':       get_setting('smtp_port', '587'),
        'encryption': get_setting('smtp_encryption', 'tls'),
        'username':   get_setting('smtp_username', ''),
        'password':   get_setting('smtp_password', ''),
        'from_name':  get_setting('smtp_from_name', 'Sonar Fleet'),
    }


def _log(to_addr, subject, success, error_msg=None):
    """Write one row to the email_log table; silently swallows errors."""
    from datetime import datetime
    try:
        from app import get_db
        db = get_db()
        db.execute(
            'INSERT INTO email_log (to_addr, subject, success, error_msg, sent_at) '
            'VALUES (?,?,?,?,?)',
            (to_addr, subject, 1 if success else 0, error_msg, datetime.now().isoformat())
        )
        db.commit()
        db.close()
    except Exception:
        pass


def _send_raw(cfg, all_recipients, msg_str):
    """Open SMTP connection and send.  Returns None on success, error str on failure."""
    port = int(cfg['port'])
    try:
        if cfg['encryption'] == 'ssl':
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg['host'], port, context=ctx, timeout=10) as srv:
                srv.login(cfg['username'], cfg['password'])
                srv.sendmail(cfg['username'], all_recipients, msg_str)
        else:
            with smtplib.SMTP(cfg['host'], port, timeout=10) as srv:
                if cfg['encryption'] == 'tls':
                    srv.starttls(context=ssl.create_default_context())
                srv.login(cfg['username'], cfg['password'])
                srv.sendmail(cfg['username'], all_recipients, msg_str)
        return None
    except smtplib.SMTPAuthenticationError:
        return 'SMTP authentication failed — check your username and password.'
    except smtplib.SMTPConnectError:
        return f"Could not connect to {cfg['host']}:{port}."
    except smtplib.SMTPServerDisconnected:
        return 'Server disconnected unexpectedly.'
    except smtplib.SMTPSenderRefused:
        return f"Server refused the sender address ({cfg['username']})."
    except smtplib.SMTPRecipientsRefused:
        return 'Server refused one or more recipient addresses.'
    except smtplib.SMTPException as e:
        return f'SMTP error: {e}'
    except OSError as e:
        s = str(e)
        if 'Name or service not known' in s or 'getaddrinfo failed' in s:
            return f"Could not resolve host \"{cfg['host']}\"."
        if 'Connection refused' in s or 'Errno 111' in s:
            return f'Connection refused on port {port}.'
        if 'Network is unreachable' in s or 'Errno 101' in s:
            return 'Network is unreachable — outbound SMTP may be blocked.'
        if 'timed out' in s.lower():
            return f'Connection timed out on port {port}.'
        return f'Network error: {e}'
    except Exception as e:
        return f'Unexpected error: {e}'


def send_email(to_addr, subject, body_text, body_html=None):
    """Send a single email.  Returns (True, None) or (False, error_message)."""
    cfg = _smtp_config()

    if not cfg['host'] or not cfg['username'] or not cfg['password']:
        missing = [k for k in ('host', 'username', 'password') if not cfg[k]]
        err = f'SMTP not fully configured — missing: {", ".join(missing)}.'
        _log(to_addr, subject, False, err)
        return False, err

    try:
        int(cfg['port'])
    except (ValueError, TypeError):
        err = f"Invalid SMTP port \"{cfg['port']}\"."
        _log(to_addr, subject, False, err)
        return False, err

    if body_html:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(body_text, 'plain'))
        msg.attach(MIMEText(body_html, 'html'))
    else:
        msg = MIMEText(body_text, 'plain')

    msg['Subject'] = subject
    msg['From']    = f"{cfg['from_name']} <{cfg['username']}>"
    msg['To']      = to_addr

    err = _send_raw(cfg, [to_addr], msg.as_string())
    if err is None:
        _log(to_addr, subject, True)
        return True, None
    _log(to_addr, subject, False, err)
    return False, err


def send_email_multi(to_addrs, bcc_addrs, subject, body_text, body_html=None):
    """Send one email to multiple recipients (BCC list kept private in headers).

    Returns (True, None) or (False, error_message).
    """
    to_addrs  = [a for a in (to_addrs  or []) if a]
    bcc_addrs = [a for a in (bcc_addrs or []) if a and a not in to_addrs]
    all_recipients = to_addrs + bcc_addrs

    if not all_recipients:
        return False, 'No recipients specified.'

    log_to = all_recipients[0] if len(all_recipients) == 1 \
        else f'{all_recipients[0]} + {len(all_recipients) - 1} more'

    cfg = _smtp_config()

    if not cfg['host'] or not cfg['username'] or not cfg['password']:
        missing = [k for k in ('host', 'username', 'password') if not cfg[k]]
        err = f'SMTP not fully configured — missing: {", ".join(missing)}.'
        _log(log_to, subject, False, err)
        return False, err

    try:
        int(cfg['port'])
    except (ValueError, TypeError):
        err = f"Invalid SMTP port \"{cfg['port']}\"."
        _log(log_to, subject, False, err)
        return False, err

    if body_html:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(body_text, 'plain'))
        msg.attach(MIMEText(body_html, 'html'))
    else:
        msg = MIMEText(body_text, 'plain')

    msg['Subject'] = subject
    msg['From']    = f"{cfg['from_name']} <{cfg['username']}>"
    msg['To']      = ', '.join(to_addrs) if to_addrs else cfg['username']
    # BCC addresses intentionally NOT added to headers

    err = _send_raw(cfg, all_recipients, msg.as_string())
    if err is None:
        _log(log_to, subject, True)
        return True, None
    _log(log_to, subject, False, err)
    return False, err
