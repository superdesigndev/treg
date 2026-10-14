# Landing design assets

The landing layout, styles, motion modules and agent marks come from
[treg-design/landing](https://github.com/l527497426-cyber/treg-design/tree/484066dd9d37a85eba24257ac9edb401b7710357/landing),
revision `484066dd9d37a85eba24257ac9edb401b7710357`, supplied as the implementation reference.

`../../landing.html` is server-rendered; assets ship via `/media` without a separate build.
Local integration covers authentication, setup commands, tracking and support chat.
WebGL failure shows the monochrome treg mark; BFCache restores animation and layout.
See `docs/context/interface/seo.md` for landing behavior.

## Third-party dependencies

| Dependency | Version | Delivery and license |
|---|---|---|
| Three.js, RoomEnvironment, FontLoader and Helvetiker font | 0.180.0 | jsDelivr npm CDN, pinned together by the import map in `landing.html`; MIT (font license in its metadata) |
| Lenis JavaScript and CSS | 1.3.26 | jsDelivr npm CDN, pinned in `landing.html`; MIT |
| `assets/*` | Reference revision above | Agent marks; `assets/LOBEHUB-LICENSE` retained. The prototype's orange treg mark is omitted. |

Use the [Three.js import-map installation](https://threejs.org/manual/pages/installation.html)
with one exact version and CDN for the full module graph. No third-party library builds are
committed here. A failed Three.js module download activates the static fallback; missing Lenis
leaves native scrolling available. Run landing E2E, SEO and tracking checks after upgrades.
