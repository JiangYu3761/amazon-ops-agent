/* Advertise the installable application only after the real workspace API has answered. */
window.workspaceInstall = (() => {
  const standalone = window.matchMedia('(display-mode: standalone)');
  const button = document.getElementById('workspaceInstallButton');
  let mode, promptEvent, touchIcon, installed = false;
  let manifest = document.querySelector('link[rel="manifest"]');

  function showButton() {
    button.hidden = mode !== 'server' || !promptEvent || installed || standalone.matches;
  }
  function link(rel, href) {
    const element = document.createElement('link');
    element.rel = rel;
    element.href = href;
    document.head.append(element);
    return element;
  }
  function update(nextMode) {
    mode = nextMode;
    if (mode === 'server' && window.isSecureContext && /^https?:$/.test(location.protocol)) {
      if (!manifest) manifest = link('manifest', '/api/workspace-app/manifest.webmanifest');
      if (!touchIcon) touchIcon = link('apple-touch-icon', 'icons/homa-192.png');
    } else if (manifest) {
      manifest.remove();
      touchIcon?.remove();
      manifest = touchIcon = promptEvent = null;
    }
    showButton();
  }
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    promptEvent = event;
    showButton();
  });
  window.addEventListener('appinstalled', () => {
    installed = true;
    promptEvent = null;
    showButton();
  });
  standalone.addEventListener('change', showButton);
  button.addEventListener('click', async () => {
    const event = promptEvent;
    if (!event || mode !== 'server') return;
    promptEvent = null;
    showButton();
    try { await event.prompt(); }
    catch { button.hidden = true; }
  });
  return {
    update,
    expectsServer: () => standalone.matches || navigator.standalone === true ||
      new URLSearchParams(location.search).get('launch') === 'workspace'
  };
})();
