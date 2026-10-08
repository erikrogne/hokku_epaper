// Dialog accessibility and install help, shared across the existing UI flows.
document.addEventListener('DOMContentLoaded', () => {
  const main = document.querySelector('main.page');
  const nav = document.querySelector('.section-nav');
  let activeDialog = null;
  let returnFocus = null;
  const focusables = dialog => Array.from(dialog.querySelectorAll(
    'button, a[href], input, select, textarea, [tabindex="0"]'
  )).filter(el => !el.disabled && !el.closest('[hidden]') && el.getClientRects().length);
  function syncDialogs() {
    const backdrop = Array.from(document.querySelectorAll('.modal-backdrop.show')).pop();
    const dialog = backdrop?.querySelector('.modal') || null;
    if (dialog === activeDialog) return;
    if (dialog) {
      if (!activeDialog) returnFocus = backdrop._returnFocus || document.activeElement;
      dialog.setAttribute('role', 'dialog');
      dialog.setAttribute('aria-modal', 'true');
      dialog.setAttribute('tabindex', '-1');
      const heading = dialog.querySelector('h3');
      if (heading) {
        if (!heading.id) heading.id = `${backdrop.id}-heading`;
        dialog.setAttribute('aria-labelledby', heading.id);
      }
      main.inert = true;
      nav.inert = true;
      document.body.classList.add('dialog-open');
      if (!dialog.contains(document.activeElement)) (focusables(dialog)[0] || dialog).focus();
    } else {
      main.inert = false;
      nav.inert = false;
      document.body.classList.remove('dialog-open');
      if (returnFocus?.isConnected && returnFocus.getClientRects().length) returnFocus.focus();
      returnFocus = null;
    }
    activeDialog = dialog;
  }
  document.querySelectorAll('.modal-backdrop').forEach(el =>
    new MutationObserver(syncDialogs).observe(el, {attributes: true, attributeFilter: ['class']})
  );
  document.addEventListener('keydown', e => {
    if (!activeDialog || e.key !== 'Tab') return;
    const controls = focusables(activeDialog);
    const first = controls[0] || activeDialog;
    const last = controls[controls.length - 1] || activeDialog;
    if (e.shiftKey && (document.activeElement === first || document.activeElement === activeDialog)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || document.activeElement === activeDialog)) {
      e.preventDefault(); first.focus();
    }
  });
  // Associate existing visual labels, including dynamically mounted editors.
  function associateLabels(root) {
    root.querySelectorAll('.config-row').forEach(row => {
      const label = row.querySelector('label:not([for])');
      const input = row.querySelector('input:not([type="hidden"]), select, textarea');
      if (label && input && !label.contains(input)) {
        if (input.id) label.htmlFor = input.id;
        else if (!input.hasAttribute('aria-label')) input.setAttribute('aria-label', label.textContent.trim());
      }
    });
  }
  associateLabels(document);
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === 1) associateLabels(node);
  }))).observe(document.body, {childList: true, subtree: true});

  const help = document.getElementById('install-help');
  document.getElementById('install-help-toggle').addEventListener('click', e => {
    help.hidden = !help.hidden;
    e.currentTarget.setAttribute('aria-expanded', String(!help.hidden));
  });
  document.getElementById('install-context').textContent = window.isSecureContext
    ? 'On iPhone, open in Safari, then use Share → Add to Home Screen. Other browsers may offer Install in their menu.'
    : 'On iPhone, open in Safari and use Share → Add to Home Screen. This HTTP address works online; offline support needs HTTPS. Use your current bookmark until a secure address is configured.';
  if (!window.isSecureContext || !('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/hokku/service-worker.js', {scope: '/hokku/', updateViaCache: 'none'})
    .then(registration => {
      const showUpdate = () => {
        if (registration.waiting && navigator.serviceWorker.controller) document.getElementById('app-update').hidden = false;
      };
      showUpdate();
      registration.addEventListener('updatefound', () => {
        const worker = registration.installing;
        worker?.addEventListener('statechange', showUpdate);
      });
      // Wait for open pages to close; never reload a form or interrupt an upload.
    }).catch(() => {
      document.getElementById('install-context').textContent += ' Offline support could not start; online use is still available.';
    });
});
