#!/usr/bin/env bash
set -Eeuo pipefail

# Queli Shop - Ubuntu installer
# Installs the app from the public GitHub repository and configures Gunicorn,
# systemd, Nginx and (when DNS/email are ready) Let's Encrypt SSL.

REPO="https://github.com/sepi5/queli-inventori.git"
APP_DIR="/opt/shoe-shop"
DATA_DIR="/var/lib/shoe-shop"
ENV_FILE="/etc/shoe-shop.env"
SERVICE="shoe-shop"
NGINX_SITE="/etc/nginx/sites-available/queli-shop"

if [[ "${EUID}" -ne 0 ]]; then
  echo "این اسکریپت را با کاربر root اجرا کن."
  echo "مثال: sudo bash install.sh"
  exit 1
fi

echo "=========================================="
echo "        نصب خودکار کوئلی شاپ"
echo "=========================================="
read -r -p "دامنه سایت [queli.dearshop.ir]: " DOMAIN
DOMAIN="${DOMAIN:-queli.dearshop.ir}"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "دامنه واردشده معتبر نیست: $DOMAIN"
  exit 1
fi
read -r -p "ایمیل برای صدور SSL (برای رد کردن خالی بگذار): " SSL_EMAIL

export DEBIAN_FRONTEND=noninteractive
echo "[1/8] نصب پیش‌نیازها..."
apt-get update
apt-get install -y git ca-certificates curl openssl python3 python3-venv python3-pip \
  build-essential nginx certbot python3-certbot-nginx

echo "[2/8] دریافت سورس از GitHub..."
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only origin main
elif [[ -e "$APP_DIR" ]]; then
  echo "مسیر $APP_DIR وجود دارد اما مخزن Git نیست؛ برای جلوگیری از حذف فایل‌ها نصب متوقف شد."
  exit 1
else
  git clone --depth 1 --branch main "$REPO" "$APP_DIR"
fi

if [[ ! -f "$APP_DIR/app.py" || ! -f "$APP_DIR/templates/inventory.html" || ! -f "$APP_DIR/stock_update_3aban.json" ]]; then
  echo "فایل‌های ضروری پروژه در مخزن پیدا نشدند؛ نصب متوقف شد."
  exit 1
fi

echo "[3/8] ساخت محیط Python و نصب کتابخانه‌ها..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/venv/bin/pip" install "Flask>=3,<4" "gunicorn>=22,<24" "openpyxl>=3.1,<4" "pywebpush>=2,<3"

echo "[4/8] ساخت کاربر و مسیر دیتابیس..."
if ! id shoe-shop >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin --gid www-data shoe-shop
fi
install -d -o shoe-shop -g www-data -m 0750 "$DATA_DIR"
chown -R root:www-data "$APP_DIR"
find "$APP_DIR" -type d -exec chmod 0750 {} \;
find "$APP_DIR" -type f -exec chmod 0640 {} \;
chmod 0750 "$APP_DIR/venv/bin"
chmod 0750 "$APP_DIR/venv/bin/"*

echo "[5/8] تنظیم رمز مدیر و کلید امنیتی..."
if [[ ! -f "$ENV_FILE" ]]; then
  ADMIN_PASSWORD="$(openssl rand -hex 16)"
  SECRET_KEY="$(openssl rand -hex 32)"
  cat > "$ENV_FILE" <<EOF
SECRET_KEY=$SECRET_KEY
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
SHOESHOP_DB=$DATA_DIR/users.db
COOKIE_SECURE=0
VAPID_SUBJECT=mailto:${SSL_EMAIL:-admin@$DOMAIN}
EOF
  chmod 0640 "$ENV_FILE"
  chown root:www-data "$ENV_FILE"
  FIRST_INSTALL=1
else
  FIRST_INSTALL=0
  # Ensure the database remains outside the source checkout.
  if ! grep -q '^SHOESHOP_DB=' "$ENV_FILE"; then
    echo "SHOESHOP_DB=$DATA_DIR/users.db" >> "$ENV_FILE"
  fi
fi

echo "[6/8] ساخت سرویس systemd..."
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
systemctl daemon-reload
systemctl enable "$SERVICE"

echo "[7/8] تنظیم Nginx..."
cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

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
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/queli-shop
nginx -t
systemctl enable nginx
systemctl restart "$SERVICE"
systemctl restart nginx

echo "[8/8] بررسی برنامه و تلاش برای فعال‌سازی SSL..."
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "سرویس اجرا نشد. گزارش خطا:"
  journalctl -u "$SERVICE" -n 60 --no-pager || true
  exit 1
fi

SSL_OK=0
if [[ -n "$SSL_EMAIL" ]]; then
  if getent ahosts "$DOMAIN" >/dev/null 2>&1; then
    if certbot --nginx -d "$DOMAIN" --redirect --non-interactive --agree-tos --email "$SSL_EMAIL" --no-eff-email; then
      sed -i 's/^COOKIE_SECURE=.*/COOKIE_SECURE=1/' "$ENV_FILE"
      systemctl restart "$SERVICE"
      SSL_OK=1
    else
      echo "صدور SSL ناموفق بود. مطمئن شو DNS دامنه به IP همین سرور اشاره می‌کند و پورت‌های 80 و 443 باز هستند."
    fi
  else
    echo "دامنه هنوز در DNS پیدا نشد؛ پس از تنظیم DNS دوباره برای SSL اقدام کن."
  fi
else
  echo "ایمیل SSL وارد نشد؛ سایت فعلاً با HTTP بالا می‌آید."
fi

echo
echo "=========================================="
echo "       نصب کوئلی شاپ به پایان رسید"
echo "=========================================="
if [[ "$SSL_OK" -eq 1 ]]; then
  echo "آدرس سایت: https://$DOMAIN"
else
  echo "آدرس سایت: http://$DOMAIN"
fi
echo "سرویس: systemctl status $SERVICE"
echo "مشاهده خطاها: journalctl -u $SERVICE -f"
echo "مسیر دیتابیس: $DATA_DIR/users.db"
if [[ "$FIRST_INSTALL" -eq 1 ]]; then
  echo
  echo "نام کاربری مدیر: admin"
  echo "رمز عبور اولیه مدیر: $ADMIN_PASSWORD"
  echo "این رمز را در جای امن نگه دار."
else
  echo "فایل تنظیمات قبلی حفظ شد؛ رمز مدیر تغییر نکرد."
fi
