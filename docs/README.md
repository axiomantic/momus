# `docs/`

Static assets published via GitHub Pages.

## Enabling Pages

Once, in the repo settings:

1. **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main` (or whichever you ship from), folder: `/docs`
4. Save

The installer page is then live at
`https://<owner>.github.io/<repo>/install/`. For
`axiomantic/momus`: <https://axiomantic.github.io/momus/latest/install/>.

## What's here

- `install/index.html` — single-file App-manifest installer. Pick "user" or
  "org", click the button, GitHub creates the App from a pre-filled
  manifest, and the page hands back the App ID and a downloadable private
  key plus a copy-paste command for `scripts/install.sh`. No build step,
  no external dependencies.

If you fork Momus, edit `REUSABLE_OWNER` near the top of the
`<script>` in `install/index.html` so consumer workflows reference your
fork's `.github` repo.
