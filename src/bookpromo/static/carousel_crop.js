// Local framing only. The separate prepare form is the only remote upload action.
(() => {
  const clamp = value => Math.max(0, Math.min(1, value));
  function wire(root) {
    const form = root.querySelector('[data-carousel-crop]');
    if (!form) return () => {};
    const frame = form.querySelector('[data-crop-frame]');
    const image = form.querySelector('[data-crop-image]');
    const x = form.elements.namedItem('focus_x');
    const y = form.elements.namedItem('focus_y');
    const prepare = root.querySelector('[data-carousel-prepare]');
    const button = prepare?.querySelector('button[type="submit"]');
    const unavailable = button?.disabled;
    const abort = new AbortController();
    const options = {signal: abort.signal};
    const initial = [x.value, y.value];
    let dragging = null;
    function draw() {
      image.style.objectPosition = `${Number(x.value) * 100}% ${Number(y.value) * 100}%`;
      const dirty = x.value !== initial[0] || y.value !== initial[1];
      if (button) button.disabled = unavailable || dirty;
      const pending = root.querySelector('[data-crop-pending]');
      if (pending) pending.hidden = !dirty;
    }
    function change(nextX, nextY) {
      x.value = String(Math.round(clamp(nextX) * 1000) / 1000);
      y.value = String(Math.round(clamp(nextY) * 1000) / 1000);
      x.dispatchEvent(new Event('input', {bubbles: true}));
      y.dispatchEvent(new Event('input', {bubbles: true}));
      draw();
    }
    function overflow() {
      const rect = frame.getBoundingClientRect();
      const scale = Math.max(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
      return [Math.max(0, image.naturalWidth * scale - rect.width), Math.max(0, image.naturalHeight * scale - rect.height)];
    }
    function axes() {
      if (!image.naturalWidth) return;
      const excess = overflow();
      // Keep named controls enabled for POST, but don't offer a no-op slider.
      x.closest('label').hidden = excess[0] < 0.5;
      y.closest('label').hidden = excess[1] < 0.5;
      x.setAttribute('aria-description', excess[0] < 0.5 ? 'Dieses Bild füllt die Rahmenbreite; horizontal ist kein Versatz sichtbar.' : 'Horizontalen Bildausschnitt wählen.');
      y.setAttribute('aria-description', excess[1] < 0.5 ? 'Dieses Bild füllt die Rahmenhöhe; vertikal ist kein Versatz sichtbar.' : 'Vertikalen Bildausschnitt wählen.');
    }
    form.addEventListener('input', draw, options);
    form.querySelector('[data-crop-center]').addEventListener('click', () => change(.5, .5), options);
    frame.addEventListener('pointerdown', event => {
      if (!image.naturalWidth || (event.pointerType === 'mouse' && event.button !== 0)) return;
      event.preventDefault();
      dragging = {id: event.pointerId, left: event.clientX, top: event.clientY, x: Number(x.value), y: Number(y.value), excess: overflow()};
      frame.setPointerCapture(event.pointerId);
      frame.classList.add('is-dragging');
    }, options);
    frame.addEventListener('pointermove', event => {
      if (!dragging || dragging.id !== event.pointerId) return;
      const [dx, dy] = dragging.excess;
      change(dx > .5 ? dragging.x - (event.clientX - dragging.left) / dx : dragging.x,
        dy > .5 ? dragging.y - (event.clientY - dragging.top) / dy : dragging.y);
    }, options);
    const stop = () => { dragging = null; frame.classList.remove('is-dragging'); };
    frame.addEventListener('pointerup', stop, options);
    frame.addEventListener('pointercancel', stop, options);
    frame.addEventListener('lostpointercapture', stop, options);
    prepare?.addEventListener('submit', event => {
      if (x.value !== initial[0] || y.value !== initial[1]) { event.preventDefault(); event.stopImmediatePropagation(); }
    }, {capture: true, signal: abort.signal});
    image.addEventListener('load', axes, options);
    const resize = new ResizeObserver(axes);
    resize.observe(frame);
    axes(); draw();
    return () => { abort.abort(); resize.disconnect(); };
  }
  window.bookpromoCrop = {wire};
})();
