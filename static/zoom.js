// Purpose: diagram zoom shared by the Architecture tab (index.html) and How it works (guide.html).
// The zoom bar placed directly before a diagram box.
const ZOOM_BAR = `<div class="zoom" aria-label="Zoom the diagram"><button data-z="-" title="Zoom out" aria-label="Zoom out">−</button><span>100%</span>`
  + `<button data-z="+" title="Zoom in" aria-label="Zoom in">+</button><button data-z="0" title="Fit to the box again">Fit</button>`
  + `<em>or pinch / Ctrl + scroll</em></div>`;
// Diagram zoom: scale the SVG's width (its viewBox keeps the shape); the box scrolls when the diagram is wider.
// Every diagram starts fitted to its box (never above 100 %) and refits on window resize until the user zooms.
// Zoom with the buttons, a two-finger pinch on touch screens, or Ctrl + scroll / trackpad pinch on desktop.
// The point under the fingers (or the mouse) stays in place while zooming.
function setupZoom(svg, bar, box) {
  const w = svg.viewBox.baseVal.width;
  if (!w || !bar) return;
  const fit = () => {
    const avail = box.clientWidth - parseFloat(getComputedStyle(box).paddingLeft) * 2;
    return avail > 0 ? Math.min(1, avail / w) : 1;
  };
  let z = fit(), touched = false;
  const label = bar.querySelector("span");
  const apply = () => {
    Object.assign(svg.style, {width: `${w * z}px`, maxWidth: "none", height: "auto"});
    label.textContent = `${Math.round(z * 100)}%`;
  };
  const zoomTo = (nz, cx = box.clientWidth / 2) => {   // cx: x of the fixed point, relative to the box
    const old = z;
    z = Math.min(3, Math.max(Math.min(0.25, fit()), nz));
    touched = true;
    const x = box.scrollLeft + cx;
    apply();
    box.scrollLeft = x * z / old - cx;
  };
  bar.onclick = e => {
    const d = e.target.closest("button")?.dataset.z;
    if (d === "+") zoomTo(z * 1.25);
    else if (d === "-") zoomTo(z / 1.25);
    else if (d === "0") { z = fit(); touched = false; apply(); box.scrollLeft = 0; }
  };
  // touch: two-finger pinch (one finger still scrolls normally)
  let pinch = null;
  const span = t => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const midX = t => (t[0].clientX + t[1].clientX) / 2 - box.getBoundingClientRect().left;
  box.addEventListener("touchstart", e => {
    if (e.touches.length === 2) pinch = {d: span(e.touches), z};
  }, {passive: true});
  box.addEventListener("touchmove", e => {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();   // the page itself must not zoom or scroll during the pinch
    zoomTo(pinch.z * span(e.touches) / pinch.d, midX(e.touches));
  }, {passive: false});
  box.addEventListener("touchend", e => { if (e.touches.length < 2) pinch = null; });
  box.addEventListener("touchcancel", () => { pinch = null; });
  // desktop: Ctrl + wheel, and trackpad pinch (browsers report it as a wheel event with ctrlKey)
  box.addEventListener("wheel", e => {
    if (!e.ctrlKey) return;   // a plain wheel keeps scrolling the page
    e.preventDefault();
    zoomTo(z * Math.exp(-e.deltaY * 0.01), e.clientX - box.getBoundingClientRect().left);
  }, {passive: false});
  addEventListener("resize", () => { if (!touched) { z = fit(); apply(); } });
  apply();
}
