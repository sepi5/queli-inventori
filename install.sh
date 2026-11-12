#!/usr/bin/env bash
set -Eeuo pipefail

# Queli Shop - clean Ubuntu installer
# Existing application data and admin credentials are preserved.

REPO="https://github.com/sepi5/queli-inventori.git"
APP_DIR="/opt/shoe-shop"
DATA_DIR="/var/lib/shoe-shop"
ENV_FILE="/etc/shoe-shop.env"
SERVICE="shoe-shop"
NGINX_SITE="/etc/nginx/sites-available/queli-shop"

log() { printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

if [[ "$EUID" -ne 0 ]]; then
  fail "Run this installer as root: sudo bash install.sh"
fi

echo "=========================================="
echo "          QUELI SHOP INSTALLER"
echo "=========================================="

read -r -p "Domain name or server IP [test.dearshop.ir]: " SITE_HOST
SITE_HOST="${SITE_HOST:-test.dearshop.ir}"
[[ "$SITE_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || fail "Invalid domain or IP address."

read -r -p "Enable HTTPS (Let's Encrypt)? [y/N]: " SSL_CHOICE
SSL_CHOICE="$(printf '%s' "${SSL_CHOICE:-n}" | tr '[:upper:]' '[:lower:]')"
case "$SSL_CHOICE" in
  y|yes)
    [[ ! "$SITE_HOST" =~ ^[0-9.]+$ ]] || fail "HTTPS requires a domain name, not an IP address."
    read -r -p "Email address for certificate notices: " SSL_EMAIL
    [[ "$SSL_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "A valid email address is required."
    WANT_SSL=1
    ;;
  n|no|"") WANT_SSL=0; SSL_EMAIL="" ;;
  *) fail "Answer y or n for HTTPS." ;;
esac

export DEBIAN_FRONTEND=noninteractive

log "Installing required Ubuntu packages"
apt-get update
apt-get install -y git ca-certificates curl openssl python3 python3-venv python3-pip \
  build-essential nginx certbot python3-certbot-nginx sudo

log "Checking system Python permissions"
PYTHON_BIN="$(readlink -f "$(command -v python3)")"
[[ -f "$PYTHON_BIN" ]] || fail "Python executable not found."
chmod 0755 "$PYTHON_BIN"
"$PYTHON_BIN" --version

log "Getting Queli Shop source code"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only origin main || fail "Could not update source. Check local Git changes and try again."
elif [[ -e "$APP_DIR" ]]; then
  fail "$APP_DIR exists but is not a Git repository. Nothing was deleted."
else
  git clone --depth 1 --branch main "$REPO" "$APP_DIR"
fi

[[ -f "$APP_DIR/app.py" ]] || fail "app.py is missing from the repository."
[[ -f "$APP_DIR/templates/inventory.html" ]] || fail "templates/inventory.html is missing."
[[ -f "$APP_DIR/stock_update_3aban.json" ]] || fail "stock_update_3aban.json is missing."

log "Creating a clean Python virtual environment"
# Only the disposable virtual environment is recreated. Database and uploaded data are untouched.
rm -rf "$APP_DIR/venv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/venv/bin/python" -m pip install \
  "Flask>=3,<4" "gunicorn>=22,<24" "openpyxl>=3.1,<4" "pywebpush>=2,<3"

log "Creating service account and persistent data directory"
if ! getent group www-data >/dev/null; then
  fail "The www-data group does not exist."
fi
if ! id shoe-shop >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin --gid www-data shoe-shop
else
  usermod -g www-data shoe-shop
fi
install -d -o shoe-shop -g www-data -m 0750 "$DATA_DIR"

log "Applying safe application permissions"
chown -R root:www-data "$APP_DIR"
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type d -exec chmod 0750 {} +
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type f -exec chmod 0640 {} +

# Do not chmod venv/bin/python: it is a symlink to the system Python executable.
find "$APP_DIR/venv" -type d -exec chmod 0750 {} +
find "$APP_DIR/venv" -type f -perm /111 -exec chmod 0750 {} +
find "$APP_DIR/venv" -type f ! -perm /111 -exec chmod 0640 {} +

# Validate that the service account can run the virtualenv.
sudo -u shoe-shop "$APP_DIR/venv/bin/python" --version
sudo -u shoe-shop "$APP_DIR/venv/bin/gunicorn" --version

log "Creating environment file without overwriting existing secrets"
FIRST_INSTALL=0
if [[ ! -f "$ENV_FILE" ]]; then
  FIRST_INSTALL=1
  ADMIN_PASSWORD="$(openssl rand -hex 16)"
  SECRET_KEY="$(openssl rand -hex 32)"
  cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
SHOESHOP_DB=$DATA_DIR/users.db
COOKIE_SECURE=0
VAPID_SUBJECT=mailto:${SSL_EMAIL:-admin@$SITE_HOST}
EOF
else
  if ! grep -q '^SHOESHOP_DB=' "$ENV_FILE"; then
    printf '\nSHOESHOP_DB=%s/users.db\n' "$DATA_DIR" >> "$ENV_FILE"
  fi
  if ! grep -q '^COOKIE_SECURE=' "$ENV_FILE"; then
    echo 'COOKIE_SECURE=0' >> "$ENV_FILE"
  fi
fi
chown root:www-data "$ENV_FILE"
chmod 0640 "$ENV_FILE"

log "Writing systemd service"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Queli Shop Flask Application
After=network.target

[Service]
Type=simple
User=shoe-shop
Group=www-data
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --threads 4 --timeout 180 --bind 127.0.0.1:8000 app:app
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

log "Writing Nginx configuration"
cat > "$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $SITE_HOST _;

    client_max_body_size 120m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 60;
    }
}
EOF

# Disable only Ubuntu's stock welcome-site symlink. Do not remove other site files.
if [[ -L /etc/nginx/sites-enabled/default || -f /etc/nginx/sites-enabled/default ]]; then
  rm -f /etc/nginx/sites-enabled/default
fi
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/queli-shop

nginx -t || fail "Nginx configuration test failed. Existing site files were not deleted."
systemctl daemon-reload
systemctl enable "$SERVICE" nginx
systemctl restart "$SERVICE"
systemctl restart nginx

log "Checking the application service"
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 80 --no-pager || true
  fail "Queli Shop service did not start. See the log above."
fi

if ! curl -fsS -o /dev/null http://127.0.0.1:8000/; then
  journalctl -u "$SERVICE" -n 50 --no-pager || true
  fail "Gunicorn is running but the application health check failed."
fi

SSL_OK=0
if [[ "$WANT_SSL" -eq 1 ]]; then
  log "Checking DNS before requesting the SSL certificate"
  if ! getent ahostsv4 "$SITE_HOST" >/dev/null 2>&1; then
    echo "WARNING: $SITE_HOST does not resolve yet. HTTPS was not enabled."
  else
    log "Requesting Let's Encrypt certificate"
    if certbot --nginx -d "$SITE_HOST" --redirect --non-interactive --agree-tos --email "$SSL_EMAIL" --no-eff-email; then
      sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' "$ENV_FILE"
      systemctl restart "$SERVICE"
      SSL_OK=1
    else
      echo "WARNING: Certificate request failed. The website remains on HTTP."
      sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=0/' "$ENV_FILE"
      systemctl restart "$SERVICE"
    fi
  fi
else
  sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=0/' "$ENV_FILE"
  systemctl restart "$SERVICE"
  echo "HTTPS was skipped. The site will use HTTP."
fi

echo
echo "=========================================="
echo "       INSTALLATION FINISHED"
echo "=========================================="
SERVER_IP="$(hostname -I | awk '{print $1}')"
if [[ "$SSL_OK" -eq 1 ]]; then
  echo "Website: https://$SITE_HOST"
else
  echo "Website: http://$SITE_HOST"
  echo "Server IP: http://$SERVER_IP"
fi
echo "Service status: systemctl status $SERVICE"
echo "Application logs: journalctl -u $SERVICE -f"
echo "Database: $DATA_DIR/users.db"

if [[ "$FIRST_INSTALL" -eq 1 ]]; then
  echo "Admin username: admin"
  echo "Initial admin password: $ADMIN_PASSWORD"
  echo "Save the password securely."
else
  echo "Existing database, environment settings and admin credentials were preserved."
fi
