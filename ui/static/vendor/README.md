# Vendored front-end code

`reactbits.js` / `reactbits.css` bundle four components from [React Bits](https://github.com/DavidHDev/react-bits)
(Particles, CountUp, BlurText, SpotlightCard; MIT + Commons Clause) with React 18.3.1 and motion 12, built once
with esbuild so the pages work offline with no build step. They are exposed as `window.ReactBits`:

- `particles(el, props)`: 3D WebGL particle field (results page hero)
- `countUp(el, props)`: animated numbers; `.set({ to })` animates from the previous value (dashboard tiles, results)
- `blurText(el, props)`: word-by-word blur-in headline (results page)
- `spotlight(el, { html, ...props })`: card with a cursor spotlight (results page)

To rebuild: put the component sources and a small entry file in a folder, `npm install react@18.3.1 react-dom@18.3.1 motion@12 esbuild`, then
`npx esbuild entry.jsx --bundle --minify --format=iife --jsx=automatic --define:process.env.NODE_ENV='"production"' --outfile=reactbits.js`.
