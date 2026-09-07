# Inter and JetBrains Mono, vendored

Latin subsets, woff2, copied unmodified from `@fontsource/inter@5.3.0` and
`@fontsource/jetbrains-mono@5.3.0`. Both are SIL Open Font License; the licences
are beside this file.

Only four faces, because only four are used: Inter at 400, 500 and 600 for the
interface, and JetBrains Mono at 400 for timestamps and numbers. No italics.

Same reasoning as `../three/README.md`. A Google Fonts link would be a request
to a third party every time the operator opens a page showing their account
names, and a black screen of the wrong typeface on a laptop behind a proxy.
These load from the same local server as everything else, so the page makes no
outbound request at all.

## Upgrading

    npm pack @fontsource/inter @fontsource/jetbrains-mono

then copy `files/inter-latin-{400,500,600}-normal.woff2` and
`files/jetbrains-mono-latin-400-normal.woff2`.
