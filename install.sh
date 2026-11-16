#!/data/data/com.termux/files/usr/bin/bash
set -e

REPO_URL="https://github.com/xxpxx1/FlashLab.git"
BIN_PATH="$PREFIX/bin/flashlab"

GREEN='\033[1;32m'
YELLOW='\033[1;33m'
CYAN='\033[1;36m'
NC='\033[0m'

echo -e "${CYAN}⚡ Memasang FlashLab v2.4 di Termux...${NC}"
pkg update -y
pkg install -y python git android-tools libusb termux-api libzstd

if [ -f "$PWD/flashlab.py" ]; then
    INSTALL_DIR="$PWD"
else
    INSTALL_DIR="$HOME/FlashLab"
    if [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" pull --ff-only
    else
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
fi

mkdir -p "$INSTALL_DIR/archives" "$INSTALL_DIR/firmware" "$INSTALL_DIR/logs" "$INSTALL_DIR/backups"
pkg install -y python-zstandard 2>/dev/null || true
pip install --upgrade pip
pip install -r "$INSTALL_DIR/requirements.txt" || pip install rich

cat << WRAPPER > "$BIN_PATH"
#!/data/data/com.termux/files/usr/bin/bash
cd "$INSTALL_DIR" || exit 1
exec python3 flashlab.py "\$@"
WRAPPER

chmod +x "$BIN_PATH" "$INSTALL_DIR/flashlab.py"
echo -e "${GREEN}✔ Selesai! Ketik 'flashlab' dari mana saja untuk menjalankan.${NC}"

if [ -r /dev/tty ]; then
    exec "$BIN_PATH" < /dev/tty
fi
