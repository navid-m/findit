#!/bin/bash

# This script creates and installs a .desktop file for the Findit application

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="${SCRIPT_DIR}/main.py"
ICON_PATH="${SCRIPT_DIR}/icon.png"
USER_DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${USER_DESKTOP_DIR}/findit.desktop"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' 
echo "Findit Desktop Entry Installer"
echo "==============================="
echo ""

if [ ! -f "$APP_PATH" ]; then
    echo -e "${RED}Error: main.py not found at $APP_PATH${NC}"
    exit 1
fi

if [ ! -x "$APP_PATH" ]; then
    echo -e "${YELLOW}Making main.py executable...${NC}"
    chmod +x "$APP_PATH"
fi

if [ ! -d "$USER_DESKTOP_DIR" ]; then
    echo -e "${YELLOW}Creating applications directory...${NC}"
    mkdir -p "$USER_DESKTOP_DIR"
fi

if [ -f "$ICON_PATH" ]; then
    ICON_LINE="Icon=${ICON_PATH}"
else
    echo -e "${YELLOW}Note: No icon.png found. Using system search icon.${NC}"
    ICON_LINE="Icon=system-search"
fi

echo -e "${GREEN}Creating desktop entry...${NC}"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Findit
Comment=Fast file search utility for Linux with NTFS support
Exec=uv run --directory ${SCRIPT_DIR} ${APP_PATH}
${ICON_LINE}
Terminal=false
Categories=Utility;System;FileTools;
Keywords=search;find;files;ntfs;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

echo -e "${GREEN}Desktop entry created successfully!${NC}"
echo ""
echo "Location: $DESKTOP_FILE"
echo ""
echo "Findit should now appear in your application menu."
echo "You may need to log out and back in, or run:"
echo "  update-desktop-database ~/.local/share/applications"
echo ""

if command -v update-desktop-database &> /dev/null; then
    echo -e "${GREEN}Updating desktop database...${NC}"
    update-desktop-database "$USER_DESKTOP_DIR" 2>/dev/null || true
    echo -e "${GREEN}Done!${NC}"
else
    echo -e "${YELLOW}Note: update-desktop-database not found. You may need to log out/in.${NC}"
fi

echo ""
echo "To uninstall, simply run:"
echo "  rm $DESKTOP_FILE"
