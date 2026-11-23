(() => {
  'use strict';

  const workerUrl = new URL('./worker.mjs', document.currentScript.src);
  const pending = new Map();
  let worker;
  let failure;
  let nextId = 0;

  function stopWorker() {
    failure = new Error('The browser preview stopped. Refresh the page to reopen your saved orders.');
    worker?.terminate();
    for (const request of pending.values()) request.reject(failure);
    pending.clear();
  }

  function startWorker() {
    const instance = new Worker(workerUrl, {type: 'module'});
    instance.onerror = stopWorker;
    instance.onmessageerror = stopWorker;
    instance.onmessage = ({data}) => {
      if (typeof data?.progress === 'string') {
        window.dispatchEvent(new CustomEvent('bakery-preview-progress', {detail: data.progress}));
      }
      const request = pending.get(data?.id);
      if (!request || typeof data.ok !== 'boolean') return;
      pending.delete(data.id);
      if (data.ok) request.resolve(data.value);
      else request.reject(new Error(data.error?.message || data.error || 'The browser preview could not complete this action.'));
    };
    return instance;
  }

  function request(action, payload) {
    return new Promise((resolve, reject) => {
      if (failure) { reject(failure); return; }
      const id = ++nextId;
      try {
        if (!worker) worker = startWorker();
        pending.set(id, {resolve, reject});
        worker.postMessage({id, action, payload});
      } catch (error) {
        pending.delete(id);
        reject(new Error('The browser could not start or contact the order desk. '+error.message));
      }
    });
  }

  window.bakeryPreview = {
    state: () => request('state'),
    apply: payload => request('apply', payload)
  };
})();
