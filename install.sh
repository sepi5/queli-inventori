#!/usr/bin/env bash
set -Eeuo pipefail

# Queli Shop - Ubuntu installer
# Installs the app and configures Python, Gunicorn, systemd, Nginx and optional SSL.

REPO="https://github.com/sepi5/queli-inventori.git"
APP_DIR="/opt/shoe-shop"
DATA_DIR="/var/lib/shoe-shop"
ENV_FILE="/etc/shoe-shop.env"
SERVICE="shoe-shop"
NGINX_SITE="/etc/nginx/sites-available/queli-shop"

if [[ "\${EUID}" -ne 0 ]]; then
  echo "ERROR: Run this installer as root."
  echo "Example: sudo bash install.sh"
  exit 1
fi

echo "=========================================="
echo "          QUELI SHOP INSTALLER"
echo "=========================================="

read -r -p "Website domain [test.dearshop.ir]: " DOMAIN
DOMAIN="\${DOMAIN:-test.dearshop.ir}"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "ERROR: Invalid domain or host: $DOMAIN"
  exit 1
fi

read -r -p "Enable HTTPS with Let's Encrypt? [y/N]: " ENABLE_SSL
ENABLE_SSL="\${ENABLE_SSL:-N}"
ENABLE_SSL="$(printf '%s' "$ENABLE_SSL" | tr '[:upper:]' '[:lower:]')"

SSL_EMAIL=""
if [[ "$ENABLE_SSL" == "y" || "$ENABLE_SSL" == "yes" ]]; then
  read -r -p "Email address for SSL certificate: " SSL_EMAIL
  if [[ -z "$SSL_EMAIL" || ! "$SSL_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
    echo "ERROR: A valid email address is required when HTTPS is enabled."
    exit 1
  fi
elif [[ "$ENABLE_SSL" != "n" && "$ENABLE_SSL" != "no" ]]; then
  echo "ERROR: Please answer y or n."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[1/8] Installing system packages..."
apt-get update
apt-get install -y git ca-certificates curl openssl python3 python3-venv python3-pip \
  build-essential nginx certbot python3-certbot-nginx

echo "[2/8] Fixing system Python executable permissions..."
PYTHON_BIN="$(readlink -f "$(command -v python3)")"
if [[ -x "$PYTHON_BIN" ]]; then
  chown root:root "$PYTHON_BIN"
  chmod 0755 "$PYTHON_BIN"
else
  echo "ERROR: Python executable not found: $PYTHON_BIN"
  exit 1
fi

echo "[3/8] Downloading application source from GitHub..."
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only origin main
elif [[ -e "$APP_DIR" ]]; then
  echo "ERROR: $APP_DIR exists but is not a Git repository."
  echo "Installation stopped to avoid overwriting existing files."
  exit 1
else
  git clone --depth 1 --branch main "$REPO" "$APP_DIR"
fi

if [[ ! -f "$APP_DIR/app.py" || ! -f "$APP_DIR/templates/inventory.html" || ! -f "$APP_DIR/stock_update_3aban.json" ]]; then
  echo "ERROR: Required application files are missing from the repository."
  exit 1
fi

echo "[4/8] Creating Python virtual environment and installing dependencies..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/venv/bin/pip" install "Flask>=3,<4" "gunicorn>=22,<24" "openpyxl>=3.1,<4" "pywebpush>=2,<3"

echo "[5/8] Configuring application user, file permissions and database directory..."
if ! id shoe-shop >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin --gid www-data shoe-shop
fi

install -d -o shoe-shop -g www-data -m 0750 "$DATA_DIR"
chown -R root:www-data "$APP_DIR"

# Set source permissions without following virtual-environment symlinks.
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type d -exec chmod 0750 {} +
find "$APP_DIR" -path "$APP_DIR/venv" -prune -o -type f -exec chmod 0640 {} +

# Keep executable scripts executable; do not chmod Python symlinks or system binaries.
find "$APP_DIR/venv" -type d -exec chmod 0750 {} +
find "$APP_DIR/venv" -type f -perm /111 -exec chmod 0750 {} +
find "$APP_DIR/venv" -type f ! -perm /111 -exec chmod 0640 {} +

echo "[6/8] Creating or preserving application environment settings..."
if [[ ! -f "$ENV_FILE" ]]; then
  ADMIN_PASSWORD="$(openssl rand -hex 16)"
  SECRET_KEY="$(openssl rand -hex 32)"
  cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
SHOESHOP_DB=$DATA_DIR/users.db
COOKIE_SECURE=0
VAPID_SUBJECT=mailto:\${SSL_EMAIL:-admin@$DOMAIN}
EOF
  chmod 0640 "$ENV_FILE"
  chown root:www-data "$ENV_FILE"
  FIRST_INSTALL=1
else
  FIRST_INSTALL=0
  if ! grep -q '^SHOESHOP_DB=' "$ENV_FILE"; then
    echo "SHOESHOP_DB=$DATA_DIR/users.db" >> "$ENV_FILE"
  fi
  if ! grep -q '^COOKIE_SECURE=' "$ENV_FILE"; then
    echo "COOKIE_SECURE=0" >> "$ENV_FILE"
  fi
  chown root:www-data "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
fi

echo "[7/8] Creating systemd service and configuring Nginx..."
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
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 2 --threads 4 --timeout 120 --bind 127.0.0.1:8000 app:app
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

cat > "$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $DOMAIN _;

    client_max_body_size 120m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \\$host;
        proxy_set_header X-Real-IP \\$remote_addr;
        proxy_set_header X-Forwarded-For \\$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \\$scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 60;
    }
}
EOF

# Disable Ubuntu's default welcome page so requests to the server IP reach Queli Shop.
rm -f /etc/nginx/sites-enabled/default
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/queli-shop

nginx -t
systemctl daemon-reload
systemctl enable "$SERVICE" nginx
systemctl restart "$SERVICE"
systemctl restart nginx

echo "[8/8] Checking application and configuring optional HTTPS..."
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "ERROR: The application service did not start. Recent logs:"
  journalctl -u "$SERVICE" -n 60 --no-pager || true
  exit 1
fi

SSL_OK=0
if [[ "$ENABLE_SSL" == "y" || "$ENABLE_SSL" == "yes" ]]; then
  if getent ahosts "$DOMAIN" >/dev/null 2>&1; then
    if certbot --nginx -d "$DOMAIN" --redirect --non-interactive --agree-tos --email "$SSL_EMAIL" --no-eff-email; then
      if grep -q '^COOKIE_SECURE=' "$ENV_FILE"; then
        sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' "$ENV_FILE"
      else
        echo "COOKIE_SECURE=1" >> "$ENV_FILE"
      fi
      systemctl restart "$SERVICE"
      SSL_OK=1
    else
      echo "WARNING: SSL certificate issuance failed."
      echo "Check that the domain points to this server and ports 80 and 443 are open."
      echo "The site will remain available over HTTP."
      if grep -q '^COOKIE_SECURE=' "$ENV_FILE"; then
        sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=0/' "$ENV_FILE"
      fi
      systemctl restart "$SERVICE"
    fi
  else
    echo "WARNING: The domain does not resolve in DNS yet: $DOMAIN"
    echo "The site will remain available over HTTP. Configure DNS and run Certbot later."
  fi
else
  if grep -q '^COOKIE_SECURE=' "$ENV_FILE"; then
    sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=0/' "$ENV_FILE"
  fi
  systemctl restart "$SERVICE"
  echo "HTTPS was disabled by your choice. The site will use HTTP."
fi

echo
echo "=========================================="
echo "       QUELI SHOP INSTALLATION COMPLETE"
echo "=========================================="
if [[ "$SSL_OK" -eq 1 ]]; then
  echo "Website (HTTPS): https://$DOMAIN"
else
  echo "Website (HTTP):  http://$DOMAIN"
  echo "Website (IP):    http://$(hostname -I | awk '{print $1}')"
fi
echo "Service status: systemctl status $SERVICE"
echo "Application logs: journalctl -u $SERVICE -f"
echo "Database path: $DATA_DIR/users.db"

if [[ "$FIRST_INSTALL" -eq 1 ]]; then
  echo
  echo "Initial admin username: admin"
  echo "Initial admin password: $ADMIN_PASSWORD"
  echo "Save this password in a secure place."
else
  echo "Existing environment file and admin credentials were preserved."
fi

if [[ "$SSL_OK" -eq 0 && ( "$ENABLE_SSL" == "y" || "$ENABLE_SSL" == "yes" ) ]]; then
  echo "To retry HTTPS later, run: certbot --nginx -d $DOMAIN --redirect"
fi
