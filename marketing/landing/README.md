# Outcome landing-page sources

The numbered Markdown files, `_shared.md` and `_facts.md` are the source for the published
use-case pages. Keep public copy and its verifiable price sources here. Campaign hypotheses,
paid targeting, execution records and review plans live in
[treg-internal](https://github.com/superdesigndev/treg-internal/tree/main/docs/marketing).

Run from this directory:

```sh
python3 build.py --check
python3 build.py
python3 build_html.py
```

`build.py` expands shared blocks into ignored `dist/` Markdown. `build_html.py` renders the
public HTML in `src/treg/web/`. Edit the source, never the generated pages. `build_preview.py`
and `build_review.py` produce previews using the same renderers.

Update `_facts.md` when a published price or claim changes, and update `_shared.md` for copy
shared by all five pages. The public build does not require the private checkout.
