#!/usr/bin/env bash
# Likelyread installer: one command on a fresh server (Ubuntu/Debian, 512 MB is plenty).
#
#   curl -fsSL https://raw.githubusercontent.com/Hugo-SEQUIER/likelyread/main/deploy/install.sh | sudo bash
#
# It installs Docker if needed, gets HTTPS for your address, starts the server and
# prints a setup link to paste into the Chrome extension. Run it again to update:
# your token and data are kept.
#
# Options (all optional; asks interactively when run in a terminal):
#   --domain t.example.com   address for the server (an A record pointing here)
#   --no-domain              no domain: use <your-ip>.sslip.io (for trying it out)
#   --dir /opt/likelyread    install directory
#   --source PATH|URL        local checkout or tarball URL instead of GitHub main
#   --http-port 80 --https-port 443
#   --yes                    do not ask questions (needs --domain or --no-domain)
#   --show-link              print the setup link of an existing install and exit
set -euo pipefail

REPO_TARBALL="https://github.com/Hugo-SEQUIER/likelyread/archive/refs/heads/main.tar.gz"
DIR=/opt/likelyread
DOMAIN=""
NO_DOMAIN=0
SOURCE=""
HTTP_PORT=""
HTTPS_PORT=""
YES=0
SHOW_LINK=0

say() { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --no-domain) NO_DOMAIN=1; shift ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    --source) SOURCE="${2:-}"; shift 2 ;;
    --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
    --https-port) HTTPS_PORT="${2:-}"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --show-link) SHOW_LINK=1; shift ;;
    -h|--help)
      cat <<'EOF'
Likelyread installer. Usage: install.sh [options]
  --domain t.example.com   address for the server (an A record pointing here)
  --no-domain              no domain: use <your-ip>.sslip.io (for trying it out)
  --dir /opt/likelyread    install directory
  --source PATH|URL        local checkout or tarball URL instead of GitHub main
  --http-port 80 --https-port 443
  --yes                    do not ask questions
  --show-link              print the setup link of an existing install and exit
Run it again to update: the address, token and data are kept.
EOF
      exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# Questions come from the terminal even when the script is piped into bash.
TTY=""
if [ "$YES" -eq 0 ] && { : </dev/tty; } 2>/dev/null; then TTY=/dev/tty; fi
ask() { # ask "question" default -> answer
  local a=""
  if [ -n "$TTY" ]; then read -r -p "$1 " a <"$TTY" || true; fi
  printf '%s' "${a:-$2}"
}
confirm() { # confirm "question" -> 0 for yes
  [ "$YES" -eq 1 ] && return 0
  [ -z "$TTY" ] && return 0
  case "$(ask "$1 [Y/n]" y)" in [nN]*) return 1 ;; *) return 0 ;; esac
}

ENV_FILE="$DIR/deploy/.env"
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -1; }

setup_link() {
  local d t port=""
  d="$(env_get DOMAIN)"; t="$(env_get LIKELYREAD_TOKEN)"
  [ -n "$d" ] && [ -n "$t" ] || die "no install found in $DIR"
  [ "$(env_get HTTPS_PORT)" != "" ] && [ "$(env_get HTTPS_PORT)" != "443" ] && port=":$(env_get HTTPS_PORT)"
  printf 'https://%s%s/#connect=%s' "$d" "$port" "$t"
}

if [ "$SHOW_LINK" -eq 1 ]; then setup_link; echo; exit 0; fi

# ------------------------------------------------------------------ requirements
step "1/5 Checking the server"
command -v curl >/dev/null || die "curl is required (apt-get install -y curl)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  [ "$(id -u)" -eq 0 ] || die "Docker is missing. Run this installer with sudo so it can install Docker."
  confirm "Docker is not installed. Install it now (official get.docker.com script)?" || die "Docker is required."
  curl -fsSL https://get.docker.com | sh
fi
docker info >/dev/null 2>&1 || die "Cannot talk to Docker. Run with sudo, or add your user to the docker group."
say "Docker: $(docker --version | cut -d, -f1)"

# ------------------------------------------------------------------ source
step "2/5 Getting Likelyread into $DIR"
UPDATE=0
[ -f "$ENV_FILE" ] && UPDATE=1 && say "Existing install found: updating it, keeping your token and data."
mkdir -p "$DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "$SOURCE" ] && [ -d "$SOURCE" ]; then
  tar -C "$SOURCE" --exclude=.git --exclude=node_modules --exclude=deploy/.env --exclude=deploy/data -cf - . | tar -C "$tmp" -xf -
else
  curl -fsSL "${SOURCE:-$REPO_TARBALL}" | tar -xz -C "$tmp" --strip-components=1 \
    || die "Could not download Likelyread from ${SOURCE:-$REPO_TARBALL}"
fi
[ -f "$tmp/deploy/docker-compose.yml" ] && [ -f "$tmp/server/app.py" ] || die "The download does not look like Likelyread."
# Replace code, never the .env or the data directory.
for d in server deploy cli skills; do
  if [ "$d" = deploy ]; then
    mkdir -p "$DIR/deploy"
    find "$tmp/deploy" -maxdepth 1 -type f ! -name .env -exec cp {} "$DIR/deploy/" \;
  else
    rm -rf "${DIR:?}/$d"; cp -r "$tmp/$d" "$DIR/$d"
  fi
done

# ------------------------------------------------------------------ address
step "3/5 Choosing the address"
PUBLIC_IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
[ -n "$PUBLIC_IP" ] && say "This server's public IPv4: $PUBLIC_IP"
if [ "$UPDATE" -eq 1 ]; then
  # An update keeps the saved address and ports unless new ones are given.
  [ -z "$DOMAIN" ] && [ "$NO_DOMAIN" -eq 0 ] && DOMAIN="$(env_get DOMAIN)"
  [ -z "$HTTP_PORT" ] && HTTP_PORT="$(env_get HTTP_PORT)"
  [ -z "$HTTPS_PORT" ] && HTTPS_PORT="$(env_get HTTPS_PORT)"
fi
HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-443}"
if [ -z "$DOMAIN" ] && [ "$NO_DOMAIN" -eq 0 ]; then
  say ""
  say "Use a subdomain of a domain you own, e.g. t.yourcompany.com: create an A record"
  say "pointing to ${PUBLIC_IP:-the IP of this server} (on Cloudflare: DNS only, grey cloud) first."
  say "Leave it empty to use a free ${PUBLIC_IP//./-}.sslip.io address. That is fine for a test,"
  say "but for real email use your own domain: it looks more trustworthy to spam filters, and it"
  say "keeps working if you move to another server."
  DOMAIN="$(ask "Address (empty for sslip.io):" "")"
  [ -z "$DOMAIN" ] && NO_DOMAIN=1
  [ -z "$TTY" ] && [ "$NO_DOMAIN" -eq 1 ] && [ "$YES" -eq 0 ] && say "(no terminal: using sslip.io; pass --domain to choose)"
fi
if [ "$NO_DOMAIN" -eq 1 ]; then
  [ -n "$PUBLIC_IP" ] || die "Could not detect the public IP; pass --domain."
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
fi
DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z' | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')"
printf '%s' "$DOMAIN" | grep -Eq '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' || die "Not a valid address: $DOMAIN"
say "Address: $DOMAIN"

if [ "$DOMAIN" != localhost ] && [ -n "$PUBLIC_IP" ]; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
  if [ -z "$resolved" ]; then
    warn "$DOMAIN does not resolve yet. Create the A record, wait a minute, then run this again."
    confirm "Continue anyway?" || exit 1
  elif [ "$resolved" != "$PUBLIC_IP" ]; then
    warn "$DOMAIN points to $resolved, not to this server ($PUBLIC_IP)."
    warn "On Cloudflare, switch the record to DNS only (grey cloud)."
    confirm "Continue anyway?" || exit 1
  fi
fi
for p in "$HTTP_PORT" "$HTTPS_PORT"; do
  if command -v ss >/dev/null && ss -ltnH "( sport = :$p )" 2>/dev/null | grep -q .; then
    if ! docker ps --format '{{.Ports}}' | grep -q ":$p->"; then
      warn "Port $p is already used by another program. HTTPS needs ports 80 and 443 free."
      confirm "Continue anyway?" || exit 1
    fi
  fi
done

# ------------------------------------------------------------------ config
step "4/5 Writing the configuration"
if [ "$UPDATE" -eq 1 ]; then
  TOKEN="$(env_get LIKELYREAD_TOKEN)"
else
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
[ "${#TOKEN}" -ge 16 ] || die "Could not create a token."
if [ "$UPDATE" -eq 0 ]; then
  cp "$DIR/deploy/.env.example" "$ENV_FILE"
fi
set_env() { # set_env KEY VALUE
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"; else printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"; fi
}
set_env DOMAIN "$DOMAIN"
set_env LIKELYREAD_TOKEN "$TOKEN"
set_env HTTP_PORT "$HTTP_PORT"
set_env HTTPS_PORT "$HTTPS_PORT"
chmod 600 "$ENV_FILE"
mkdir -p "$DIR/deploy/data"
say "Saved $ENV_FILE (readable by root only)."

# ------------------------------------------------------------------ start
step "5/5 Starting Likelyread"
( cd "$DIR/deploy" && docker compose --env-file .env up -d --build --remove-orphans ) >"$DIR/install.log" 2>&1 \
  || { tail -20 "$DIR/install.log" >&2; die "docker compose failed (full log: $DIR/install.log)"; }

PORT_SUFFIX=""; [ "$HTTPS_PORT" != 443 ] && PORT_SUFFIX=":$HTTPS_PORT"
BASE="https://$DOMAIN$PORT_SUFFIX"
CURL_TLS=(); [ "$DOMAIN" = localhost ] && CURL_TLS=(-k)
say "Waiting for HTTPS (getting a certificate can take a minute)..."
ok=0
for _ in $(seq 1 60); do
  if curl -fsS "${CURL_TLS[@]}" --max-time 5 "$BASE/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
if [ "$ok" -ne 1 ]; then
  warn "The server is running but $BASE is not answering over HTTPS yet."
  warn "Check that $DOMAIN points here and that ports 80 and 443 are open in your firewall,"
  warn "then look at: cd $DIR/deploy && docker compose logs caddy"
fi

LINK="$(setup_link)"
cat <<EOF

$( [ "$ok" -eq 1 ] && printf '\033[1;32mLikelyread is running at %s\033[0m' "$BASE" || printf '\033[1mLikelyread is installed at %s\033[0m' "$BASE" )

Your setup link (it contains your private token: do not share it):

  $LINK

Next:
  1. Install the Likelyread Chrome extension.
  2. In its settings, paste the setup link and click Save.
  3. Open the link in your browser to see your dashboard.

For an AI agent or script on any machine:
  likelyread setup '$LINK'

Show this link again:   sudo bash $DIR/deploy/install.sh --show-link --dir $DIR
Update later:           run the install command again
EOF
