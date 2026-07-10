(() => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : "";
  if (!token) return;

  document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
    if (form.querySelector('input[name="_csrf_token"]')) return;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "_csrf_token";
    input.value = token;
    form.appendChild(input);
  });

  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const isRequest = input instanceof Request;
    const inputUrl = input instanceof URL ? input.href : (isRequest ? input.url : input);
    const url = new URL(inputUrl, window.location.href);
    const method = String(init.method || (isRequest ? input.method : "GET") || "GET").toUpperCase();
    if (url.origin !== window.location.origin || ["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
      return originalFetch(input, init);
    }
    const headers = new Headers(init.headers || (isRequest ? input.headers : undefined));
    headers.set("X-CSRF-Token", token);
    return originalFetch(input, {...init, headers});
  };
})();
