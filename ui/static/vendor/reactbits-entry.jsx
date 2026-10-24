// Mount React Bits components into plain pages: window.ReactBits.{particles, countUp, blurText, spotlight}
import { createElement as h } from 'react';
import { createRoot } from 'react-dom/client';
import Particles from './Particles.jsx';
import CountUp from './CountUp.jsx';
import BlurText from './BlurText.jsx';
import SpotlightCard from './SpotlightCard.jsx';

function mount(el, render) {
  const root = createRoot(el);
  let props = {};
  const api = { set(next) { props = { ...props, ...next }; root.render(render(props)); return api; }, unmount() { root.unmount(); } };
  return api;
}

window.ReactBits = {
  particles: (el, props) => mount(el, (p) => h(Particles, p)).set(props),
  // set({ to }) again later animates from the previous value
  countUp: (el, props) => {
    let last = props.from ?? 0;
    const api = mount(el, (p) => h(CountUp, { key: `${p.from}-${p.to}`, ...p }));
    const set = api.set;
    api.set = (next) => { const to = next.to ?? last; set({ ...next, from: last, to }); last = to; return api; };
    return api.set(props);
  },
  blurText: (el, props) => mount(el, (p) => h(BlurText, p)).set(props),
  spotlight: (el, { html, ...props }) => mount(el, (p) => h(SpotlightCard, p, h('div', { dangerouslySetInnerHTML: { __html: html } }))).set(props),
};
