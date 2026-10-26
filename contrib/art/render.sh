#!/bin/sh
# Rebuild every image in docs/images/ and site/ from the sources in this folder.
# Needs: Google Chrome, cwebp and ImageMagick (brew install webp imagemagick), and a Python
# with hbkit installed plus fonttools and brotli (pip install -e . fonttools brotli).
set -eu
cd "$(dirname "$0")/../.."
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
PY=${PYTHON:-python3}
A=contrib/art D=docs/images S=site

shot() {  # shot <html> <width,height> <out.png> [scale] [extra chrome flags...]
  html=$1 size=$2 out=$3 scale=${4:-1}; shift 3; [ $# -gt 0 ] && shift
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --force-device-scale-factor="$scale" --window-size="$size" --virtual-time-budget=3000 \
    "$@" --screenshot="$PWD/$out" "file://$PWD/$html" 2>/dev/null
}

$PY $A/render_tui.py
shot $A/tui.html 1458,611 $D/tui.png 2
for w in 640 960 1280 1600 2000; do cwebp -quiet -q 86 -resize $w 0 $D/tui.png -o $S/images/tui-$w.webp; done
magick $D/tui.png -crop 856x996+2024+142 +repage /tmp/hbkit-panel.png
cwebp -quiet -q 88 /tmp/hbkit-panel.png -o $S/images/tui-panel-856.webp
cwebp -quiet -q 88 -resize 428 0 /tmp/hbkit-panel.png -o $S/images/tui-panel-428.webp

cp $S/images/icon.svg $D/icon.svg
$PY $A/logo.py
shot $A/icon.html 1024,1024 $D/icon-1024.png 1 --default-background-color=00000000
shot $A/banner.html 1600,500 $D/banner.png
shot $A/social.html 1280,640 $S/social-preview.png
cp $S/social-preview.png $D/social-preview.png

# Favicons come from Chrome's 1024 px render: ImageMagick's own SVG reader drops the
# gradient and the check mark.
for n in 16 32 48; do magick $D/icon-1024.png -resize ${n}x$n /tmp/hbkit-ic$n.png; done
magick /tmp/hbkit-ic16.png /tmp/hbkit-ic32.png /tmp/hbkit-ic48.png $S/favicon.ico
magick $D/icon-1024.png -resize 180x180 -background '#0a171c' -flatten $S/apple-touch-icon.png
echo "art rebuilt"
