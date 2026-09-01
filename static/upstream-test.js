(function () {
  const overlay = document.getElementById("upstream-test-overlay");
  const modal = document.getElementById("upstream-test-modal");
  const summary = document.getElementById("upstream-test-summary");
  const state = document.getElementById("upstream-test-state");
  const details = document.getElementById("upstream-test-details");
  const cancelButton = document.getElementById("upstream-test-cancel");
  const confirmButton = document.getElementById("upstream-test-confirm");
  let activeUpstreamId = null;
  let busy = false;

  if (!overlay || !modal || !summary || !state || !details || !cancelButton || !confirmButton) {
    return;
  }

  function refreshIcons() {
    if (window.lucide) {
      window.lucide.createIcons();
    }
  }

  function setState(label, className) {
    state.textContent = label;
    state.className = className ? `status-pill ${className}` : "status-pill";
  }

  function setDetails(rows) {
    details.replaceChildren();
    for (const [label, value] of rows) {
      const row = document.createElement("div");
      const labelEl = document.createElement("span");
      const valueEl = document.createElement("strong");
      row.className = "upstream-test-detail-row";
      labelEl.textContent = label;
      valueEl.textContent = value ?? "-";
      row.append(labelEl, valueEl);
      details.appendChild(row);
    }
  }

  function openUpstreamTestModal(upstreamId) {
    activeUpstreamId = upstreamId;
    busy = false;
    summary.textContent = `将向 ${upstreamId} 发起一次真实固定生图测试，可能消耗上游账号资源。`;
    setState("待确认", "");
    setDetails([
      ["上游渠道", upstreamId],
      ["模型", "nai-diffusion-4-5-full"],
      ["规格", "512x512 / 28 步 / 1 张"],
      ["提示词", "A simple red apple on a white plate."],
    ]);
    cancelButton.disabled = false;
    cancelButton.textContent = "取消";
    confirmButton.disabled = false;
    confirmButton.innerHTML = `<i data-lucide="play" class="btn-icon"></i>开始测试`;
    overlay.hidden = false;
    modal.hidden = false;
    refreshIcons();
    confirmButton.focus();
  }

  function closeUpstreamTestModal() {
    if (busy) return;
    overlay.hidden = true;
    modal.hidden = true;
    activeUpstreamId = null;
  }

  function disabledStateRows(data) {
    return data?.upstream_enabled === false
      ? [["当前状态", "已禁用（本次测试不改变启用状态）"]]
      : [];
  }

  function renderSuccess(data) {
    busy = false;
    summary.textContent = "测试完成，上游返回了可预览图片。";
    setState("成功", "status-success");
    setDetails([
      ["上游渠道", data.upstream_id],
      ...disabledStateRows(data),
      ["耗时", `${data.elapsed_ms ?? 0} ms`],
      ["Zip 大小", `${data.zip_bytes ?? 0} bytes`],
      ["图片数量", String(data.image_count ?? 0)],
      ["预览文件", data.preview_image?.filename || "-"],
      ["消息", data.message || "上游测试成功"],
    ]);
    if (data.preview_image?.data_url) {
      const preview = document.createElement("figure");
      const image = document.createElement("img");
      const caption = document.createElement("figcaption");
      preview.className = "upstream-test-preview";
      image.src = data.preview_image.data_url;
      image.alt = data.preview_image.filename || "上游测试预览";
      caption.textContent = data.preview_image.filename || "preview image";
      preview.append(image, caption);
      details.appendChild(preview);
    }
    cancelButton.disabled = false;
    cancelButton.textContent = "关闭";
    confirmButton.disabled = true;
    confirmButton.textContent = "已完成";
  }

  function renderFailure(data, fallbackStatus) {
    busy = false;
    summary.textContent = "测试失败，上游或队列返回了错误。";
    setState("失败", "status-error");
    setDetails([
      ["上游渠道", data?.upstream_id || activeUpstreamId || "-"],
      ...disabledStateRows(data),
      ["错误码", data?.error_code || `HTTP ${fallbackStatus}`],
      ["错误类型", data?.error_type || "RequestError"],
      ["错误消息", data?.message || "上游测试失败"],
      ["耗时", `${data?.elapsed_ms ?? 0} ms`],
    ]);
    cancelButton.disabled = false;
    cancelButton.textContent = "关闭";
    confirmButton.disabled = false;
    confirmButton.innerHTML = `<i data-lucide="rotate-cw" class="btn-icon"></i>重试`;
    refreshIcons();
  }

  async function runUpstreamTest() {
    if (!activeUpstreamId || busy) return;
    busy = true;
    summary.textContent = "测试请求已提交，等待上游响应。";
    setState("测试中", "status-info");
    setDetails([
      ["上游渠道", activeUpstreamId],
      ["状态", "等待上游响应"],
    ]);
    cancelButton.disabled = true;
    confirmButton.disabled = true;
    confirmButton.textContent = "测试中...";
    try {
      const response = await fetch(`/admin/api/upstreams/${encodeURIComponent(activeUpstreamId)}/test`, {
        method: "POST",
        headers: {"Accept": "application/json"},
      });
      let data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = {ok: false, message: `HTTP ${response.status}`};
      }
      if (response.ok && data?.ok) {
        renderSuccess(data);
      } else {
        renderFailure(data, response.status);
      }
    } catch (error) {
      renderFailure(
        {ok: false, error_code: "network_error", error_type: error.name || "NetworkError", message: error.message || "网络请求失败"},
        0,
      );
    }
  }

  cancelButton.addEventListener("click", closeUpstreamTestModal);
  overlay.addEventListener("click", closeUpstreamTestModal);
  confirmButton.addEventListener("click", runUpstreamTest);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) {
      closeUpstreamTestModal();
    }
  });

  window.openUpstreamTestModal = openUpstreamTestModal;
})();
