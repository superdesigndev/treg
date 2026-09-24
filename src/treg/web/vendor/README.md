# Package-generated browser runtime

Vue is installed from `frontend/package-lock.json`. `frontend/scripts/copy-runtime.mjs` copies
its global production build and license for Enrich Arena and the frozen legacy Dashboard.
The maintained Dashboard imports the same npm package through Vite.

Generated JavaScript and licenses are ignored by Git and included in Python distributions by
`hatch_build.py`. Run `bash scripts/build-dashboard.sh`; local `npm run dev` also prepares them.
Do not download or commit library builds here.

Standalone pages retain their versioned, same-origin URLs: a blocked CDN previously caused blank
signed-in dashboards ([#137](https://github.com/superdesigndev/treg/issues/137)). The copy step checks
that page URLs match the installed Vue version, so a dependency upgrade cannot silently leave
missing runtime URLs. Retire the legacy copy with the deprecated Dashboard.
