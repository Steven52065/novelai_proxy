(() => {
  const confirmDialogModal = document.getElementById('confirm-dialog-modal');
  const confirmDialogCard = document.getElementById('confirm-dialog-card');
  const confirmDialogIcon = document.getElementById('confirm-dialog-icon');
  const confirmDialogMessage = document.getElementById('confirm-dialog-message');
  const confirmDialogCancel = document.getElementById('confirm-dialog-cancel');
  const confirmDialogOk = document.getElementById('confirm-dialog-ok');
  let confirmDialogResolve = null;

  if (!confirmDialogModal || !confirmDialogCard || !confirmDialogIcon || !confirmDialogMessage || !confirmDialogCancel || !confirmDialogOk) {
    return;
  }

  function renderIcons() {
    if (window.lucide?.createIcons) {
      window.lucide.createIcons();
    }
  }

  function closeConfirmDialog(result) {
    confirmDialogModal.hidden = true;
    const resolve = confirmDialogResolve;
    confirmDialogResolve = null;
    if (resolve) {
      resolve(result);
    }
  }

  function confirmDialog(message, options = {}) {
    if (confirmDialogResolve) {
      closeConfirmDialog(false);
    }
    confirmDialogMessage.textContent = message;
    confirmDialogCard.classList.toggle('danger', Boolean(options.danger));
    confirmDialogIcon.setAttribute('data-lucide', options.danger ? 'alert-triangle' : 'circle-help');
    confirmDialogOk.className = options.danger ? 'danger' : '';
    confirmDialogOk.textContent = options.confirmText || '确认';
    confirmDialogCancel.textContent = options.cancelText || '取消';
    confirmDialogModal.hidden = false;
    renderIcons();
    confirmDialogOk.focus();
    return new Promise((resolve) => {
      confirmDialogResolve = resolve;
    });
  }

  function confirmSubmit(event, message, options = {}) {
    const form = event.currentTarget;
    if (form.dataset.confirmed === 'true') {
      delete form.dataset.confirmed;
      form.dataset.loadingConfirmed = 'true';
      return true;
    }
    event.preventDefault();
    confirmDialog(message, options).then((confirmed) => {
      if (!confirmed) return;
      form.dataset.confirmed = 'true';
      form.requestSubmit();
    });
    return false;
  }

  confirmDialogCancel.addEventListener('click', () => closeConfirmDialog(false));
  confirmDialogOk.addEventListener('click', () => closeConfirmDialog(true));
  confirmDialogModal.addEventListener('click', (event) => {
    if (event.target === confirmDialogModal) {
      closeConfirmDialog(false);
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !confirmDialogModal.hidden) {
      closeConfirmDialog(false);
    }
  });

  window.confirmDialog = confirmDialog;
  window.confirmSubmit = confirmSubmit;
})();
