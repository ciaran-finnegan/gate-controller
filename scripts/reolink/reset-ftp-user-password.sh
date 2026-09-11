#!/bin/bash
# Give the ftp-user service account (the camera's FTP login) a new random password,
# record it root-only in /root/ftp-user.credentials, and prove an FTP login works.
# Run as root: sudo bash /root/reset-ftp-user-password.sh
set -euo pipefail
umask 077
PW=$(head -c 300 /dev/urandom | base64 | tr -dc A-Za-z0-9)
PW=${PW:0:24}
[ ${#PW} -eq 24 ]
printf 'ftp-user:%s\n' "$PW" | chpasswd
printf 'FTP_USER=ftp-user\nFTP_PASSWORD=%s\n' "$PW" > /root/ftp-user.credentials
chmod 0600 /root/ftp-user.credentials
echo "ftp-user password reset; stored in /root/ftp-user.credentials"
python3 - <<'PY'
import ftplib, re
pw = re.search(r"FTP_PASSWORD=(.+)", open("/root/ftp-user.credentials").read()).group(1).strip()
f = ftplib.FTP(); f.connect("192.168.0.33", 21, timeout=10)
print("ftp login:", f.login("ftp-user", pw)); print("cwd:", f.pwd()); f.quit()
PY
