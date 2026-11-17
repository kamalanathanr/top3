#!/bin/zsh
# Build ~/Desktop/Top3.app — double-click opens Terminal and runs run.sh.
# Usage: ./make_shortcut.sh [path/to/icon.png]   (PNG ideally 1024x1024; a default icon is generated if omitted)

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Desktop/Top3.app"
WORK="$(mktemp -d)"
ICON_PNG="$1"

# 1. Create the app bundle from a tiny AppleScript
rm -rf "$APP"
osacompile -o "$APP" -e "tell application \"Terminal\"
  do script \"'$PROJECT_DIR/run.sh'\"
  activate
end tell"

# 2. Generate a default icon if none provided (blue rounded square with "3")
if [[ -z "$ICON_PNG" ]]; then
  ICON_PNG="$WORK/icon.png"
  "$PROJECT_DIR/.venv/bin/python" - "$ICON_PNG" <<'PY'
import sys
from PIL import Image, ImageDraw, ImageFont
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((80, 80, S - 80, S - 80), radius=190, fill=(37, 99, 235, 255))
font = None
for f in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"]:
    try:
        font = ImageFont.truetype(f, 560); break
    except OSError:
        pass
font = font or ImageFont.load_default()
d.text((S / 2, S / 2 + 10), "3", font=font, fill="white", anchor="mm")
img.save(sys.argv[1])
PY
fi

# 3. Convert PNG -> .icns and install it into the bundle
ICONSET="$WORK/Top3.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size*2)) $((size*2)) "$ICON_PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns"

# Newer osacompile adds Assets.car + CFBundleIconName, which override applet.icns — remove them
rm -f "$APP/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null
codesign --force --deep -s - "$APP" 2>/dev/null

# 4. Refresh Finder's icon cache for the app
touch "$APP"
rm -rf "$WORK"
echo "Created $APP"
