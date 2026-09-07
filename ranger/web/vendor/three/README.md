# three.js, vendored

Version **0.160.0**, MIT licensed, copied unmodified from the npm package
`three@0.160.0`. `LICENSE` is theirs.

Only the closure the orb actually imports is here: the module build, the three
post-processing passes it uses, and the two passes and two shaders those pull
in. Nothing else from the package.

## Why this is not a CDN link

The prototype loaded three.js from jsdelivr. Three reasons that had to stop
before the browser became a real front end:

- **It has to work on a locked-down laptop.** A corporate proxy that blocks a
  CDN turns the whole interface into a black rectangle, and the failure looks
  like a bug in Ranger.
- **This page will show account names and draft text.** A third-party script
  tag means a third party gets a request every time the operator opens it.
  The vault is private; the page that renders it should be too.
- **It pins the version.** A CDN link is a dependency that can change under
  you between one morning and the next.

## Upgrading

Fetch the package, copy the same file list, and update the version above:

    npm pack three@<version>
    tar xzf three-<version>.tgz

The import map in `web/index.html` maps the bare `three` specifier and
`three/addons/` at these paths, so nothing else needs editing.
