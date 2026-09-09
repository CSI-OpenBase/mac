(function () {
  "use strict";

  document.documentElement.classList.add("js-enabled");

  function renderIcons(root) {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons({ root: root || document });
    }
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function setPending(form, pending) {
    var button = form.querySelector('button[type="submit"], input[type="submit"]');
    if (!button) return;

    if (pending) {
      if (button.dataset.pendingActive === "true") return;
      button.dataset.pendingActive = "true";
      button.dataset.originalHtml = button.innerHTML;
      var label = form.dataset.pendingLabel;
      if (label) button.innerHTML = '<span class="pending-spinner" aria-hidden="true">◌</span><span></span>';
      if (label) button.lastElementChild.textContent = label;
      button.disabled = true;
      button.classList.add("is-pending");
      return;
    }

    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    delete button.dataset.originalHtml;
    delete button.dataset.pendingActive;
    button.disabled = false;
    button.classList.remove("is-pending");
    renderIcons(button);
  }

  function closeMobileNav() {
    var nav = document.querySelector("[data-primary-nav]");
    var toggle = document.querySelector("[data-nav-toggle]");
    if (!nav || !toggle) return;
    nav.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
    toggle.title = "打开导航";
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderIcons(document);

    var nav = document.querySelector("[data-primary-nav]");
    var toggle = document.querySelector("[data-nav-toggle]");
    if (nav && toggle) {
      toggle.addEventListener("click", function () {
        var open = nav.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", String(open));
        toggle.title = open ? "关闭导航" : "打开导航";
      });
      nav.addEventListener("click", function (event) {
        if (event.target.closest("a")) closeMobileNav();
      });
    }

    document.addEventListener("click", function (event) {
      var close = event.target.closest("[data-toast-close]");
      if (close) {
        var toast = close.closest("[data-toast]");
        if (toast) toast.remove();
      }
    });

    document.addEventListener("change", function (event) {
      var selectAll = event.target.closest("[data-select-all]");
      if (selectAll) {
        var group = selectAll.dataset.selectAll;
        document.querySelectorAll('[data-select-group="' + group + '"]').forEach(function (box) {
          box.checked = selectAll.checked;
        });
        return;
      }

      var fileInput = event.target.closest("[data-file-input]");
      if (fileInput) {
        var form = fileInput.closest("[data-upload-form]");
        var label = form ? form.querySelector("[data-file-name]") : null;
        if (label) label.textContent = fileInput.files.length ? fileInput.files[0].name : "选择 JSONL 文件";
      }
    });

    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      var message = form.dataset.confirm;
      if (message && form.dataset.confirmed !== "true") {
        if (!window.confirm(message)) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        form.dataset.confirmed = "true";
      }
      window.setTimeout(function () { setPending(form, true); }, 0);
    }, true);

    document.querySelectorAll("[data-file-drop]").forEach(function (drop) {
      ["dragenter", "dragover"].forEach(function (name) {
        drop.addEventListener(name, function (event) {
          event.preventDefault();
          drop.classList.add("is-dragging");
        });
      });
      ["dragleave", "drop"].forEach(function (name) {
        drop.addEventListener(name, function (event) {
          event.preventDefault();
          drop.classList.remove("is-dragging");
        });
      });
      drop.addEventListener("drop", function (event) {
        var input = drop.querySelector("[data-file-input]");
        if (!input || !event.dataTransfer.files.length) return;
        input.files = event.dataTransfer.files;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeMobileNav();
    });
  });

  document.body.addEventListener("htmx:configRequest", function (event) {
    var token = csrfToken();
    if (token) event.detail.headers["X-CSRF-Token"] = token;
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    var form = event.detail.elt instanceof HTMLFormElement ? event.detail.elt : event.detail.elt.closest("form");
    if (form) {
      delete form.dataset.confirmed;
      setPending(form, false);
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    renderIcons(event.detail.target);
  });

  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form").forEach(function (form) { setPending(form, false); });
  });
})();
