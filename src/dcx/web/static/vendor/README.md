# Vendored frontend dependencies

## mermaid

- File: `mermaid.min.js`
- Version: 11.17.2 (UMD bundle)
- Source: https://www.npmjs.com/package/mermaid/v/11.17.2
- License: MIT, see `mermaid.LICENSE`

The bundle is committed instead of loaded from a CDN so the web host keeps its
strict `script-src 'self'` policy and works offline. Upgrade by replacing the
file and this note together.

## katex

- Files: `katex/katex.min.js`, `katex/katex.min.css`, `katex/auto-render.min.js`,
  `katex/fonts/`
- Version: 0.19.1
- Source: https://www.npmjs.com/package/katex/v/0.19.1
- License: MIT, see `katex/katex.LICENSE`

KaTeX renders inline and display math locally. The CSS loads the bundled fonts,
so formulas work offline under `default-src 'self'`. Upgrade the JS, CSS,
auto-render extension, and font directory together.
