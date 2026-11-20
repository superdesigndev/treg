// Keep recovery outside the module graph: a failed dependency never executes gateway-3d.js.
(() => {
  function fallback(reason, error) {
    const host = document.querySelector('.gateway-sculpture');
    if (host) host.dataset.modelState = 'fallback';
    window.tregOpening?.release();
    console.warn(`3D gateway ${reason}; showing the treg mark.`, error ?? '');
  }
  // Ask before downloading three: on a CPU-emulated GPU the build holds the main thread for seconds
  // and every frame after it takes hundreds of milliseconds.
  const probe = document.createElement('canvas').getContext('webgl2', {failIfMajorPerformanceCaveat: true});
  const software = !probe || window.tregSoftwareGL?.(probe);
  probe?.getExtension('WEBGL_lose_context')?.loseContext();
  if (software) return fallback(probe ? 'skipped on software rendering' : 'needs hardware WebGL 2');
  import('./gateway-3d.js').catch(error => fallback('could not load', error));
})();
