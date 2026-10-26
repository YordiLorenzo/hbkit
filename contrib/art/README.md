# Art

Every image in `docs/images/` and `site/` is built from the files here, so the screenshots
can be redone whenever the TUI changes. Run from anywhere:

```sh
pip install -e . fonttools brotli        # hbkit itself renders the TUI screenshot
brew install webp imagemagick            # plus Google Chrome, used headless
contrib/art/render.sh
```

| source | builds |
|---|---|
| `site/images/icon.svg` | the icon, drawn by hand; everything else derives from it |
| `logo.py` | `docs/images/logo.svg` and `logo-light.svg`: the icon and the wordmark, in Outfit SemiBold as outlines |
| `render_tui.py`, `tui.html` | `docs/images/tui.png` and the site's `tui-*.webp`: hbkit's real browser screen over a made-up archive |
| `banner.html` | `docs/images/banner.png`, the README header |
| `social.html` | `site/social-preview.png`, the link preview card |
| `icon.html` | `docs/images/icon-1024.png` |

The demo archive in `render_tui.py` is invented: no real backup or personal data goes into
any image. Outfit is under the SIL Open Font License (`site/fonts/OFL.txt`).
