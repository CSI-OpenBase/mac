document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons();

  const selectAll = document.querySelector("[data-select-all]");
  if (selectAll) {
    const selections = Array.from(
      document.querySelectorAll('#comment-batch input[name="video_id"]'),
    );
    const updateSelectAll = () => {
      const selected = selections.filter((input) => input.checked).length;
      selectAll.checked = selections.length > 0 && selected === selections.length;
      selectAll.indeterminate = selected > 0 && selected < selections.length;
    };
    selectAll.addEventListener("change", () => {
      selections.forEach((input) => {
        input.checked = selectAll.checked;
      });
      updateSelectAll();
    });
    selections.forEach((input) => input.addEventListener("change", updateSelectAll));
    updateSelectAll();
  }

  const clearDialog = document.querySelector("[data-clear-dialog]");
  const clearForm = document.querySelector("[data-clear-form]");
  const clearOpen = document.querySelector("[data-clear-open]");
  const clearConfirm = document.querySelector("[data-clear-confirm]");
  const clearSubmit = document.querySelector("[data-clear-submit]");
  const clearSummary = document.querySelector("[data-clear-summary]");
  const clearSummaries = {
    exports: "将删除平台导出的原始表格和对应任务记录。",
    comments: "将删除所有视频的评论文件和导出记录，平台可见评论数不受影响。",
    all: "将删除全部采集归档、本地索引和任务记录；工作目录、运行日志及浏览器登录授权会保留。",
  };

  const updateClearDialog = () => {
    if (!clearForm) return;
    const selected = clearForm.querySelector('input[name="scope"]:checked');
    if (clearSummary && selected) {
      clearSummary.textContent = clearSummaries[selected.value] || "";
    }
    if (clearSubmit && clearConfirm) {
      clearSubmit.disabled = !clearConfirm.checked;
    }
  };

  if (clearDialog && clearForm && clearOpen && clearConfirm && clearSubmit) {
    clearOpen.addEventListener("click", () => {
      updateClearDialog();
      clearDialog.showModal();
      const cancel = clearDialog.querySelector(
        ".dialog-actions [data-clear-close]",
      );
      if (cancel) cancel.focus();
    });
    clearDialog.querySelectorAll("[data-clear-close]").forEach((button) => {
      button.addEventListener("click", () => clearDialog.close());
    });
    clearDialog.addEventListener("click", (event) => {
      const bounds = clearDialog.getBoundingClientRect();
      const outside =
        event.clientX < bounds.left ||
        event.clientX > bounds.right ||
        event.clientY < bounds.top ||
        event.clientY > bounds.bottom;
      if (outside) clearDialog.close();
    });
    clearDialog.addEventListener("close", () => {
      clearForm.reset();
      updateClearDialog();
    });
    clearForm.querySelectorAll('input[name="scope"]').forEach((input) => {
      input.addEventListener("change", updateClearDialog);
    });
    clearConfirm.addEventListener("change", updateClearDialog);
    updateClearDialog();
  }

  const active = Number(document.body.dataset.activeJobs || "0");
  const latest = document.body.dataset.latestJob || "";
  if (active > 0) {
    const poll = async () => {
      try {
        const response = await fetch("/api/state", { credentials: "same-origin" });
        if (!response.ok) return;
        const state = await response.json();
        if (state.active_jobs === 0 || String(state.latest_job_id || "") !== latest) {
          window.location.reload();
          return;
        }
      } catch (_) {
        return;
      }
      window.setTimeout(poll, 2500);
    };
    window.setTimeout(poll, 1200);
  }
});
