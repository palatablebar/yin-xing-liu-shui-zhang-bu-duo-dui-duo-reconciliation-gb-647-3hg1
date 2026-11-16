const state = { timer: null, busy: false };

function flash(message, kind) {
  const box = document.querySelector("#flash");
  if (!box) return;
  box.textContent = message;
  box.className = kind === "ok" ? "flash ok" : "flash";
  box.hidden = false;
  window.clearTimeout(state.timer);
  state.timer = window.setTimeout(() => { box.hidden = true; }, 6000);
}

async function post(url, body) {
  const options = { method: "POST" };
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.detail || "请求失败");
  }
  return data;
}

function setBusy(value) {
  state.busy = value;
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = value;
  });
}

async function runAction(name, payload) {
  if (state.busy) return;
  if (name === "quarantine") {
    window.location.href = "/quarantine";
    return;
  }
  if (name === "switch-result") {
    setBusy(true);
    try {
      await post("/api/rates", { result_id: payload.resultId });
      window.location.reload();
    } catch (error) {
      flash(error.message);
    } finally {
      setBusy(false);
    }
    return;
  }
  if (name === "rates-reset") {
    const form = document.querySelector("#rates-form");
    if (!form) return;
    form.querySelectorAll("input[data-currency]").forEach((input) => {
      if (input.dataset.currency in state.defaultRates) {
        input.value = state.defaultRates[input.dataset.currency];
      }
    });
    return;
  }
  const urls = {
    demo: "/api/demo/run",
    candidates: "/api/candidates",
    matches: "/api/matches",
  };
  const url = urls[name];
  if (!url) return;
  setBusy(true);
  try {
    await post(url);
    window.location.reload();
  } catch (error) {
    flash(error.message);
  } finally {
    setBusy(false);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (button) {
      runAction(button.dataset.action, { resultId: button.dataset.resultId });
    }
  });

  const ratesForm = document.querySelector("#rates-form");
  if (ratesForm) {
    const defaults = {};
    ratesForm.querySelectorAll("input[data-currency]").forEach((input) => {
      defaults[input.dataset.currency] = input.defaultValue;
    });
    state.defaultRates = defaults;
    ratesForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const rates = {};
      ratesForm.querySelectorAll("input[data-currency]").forEach((input) => {
        rates[input.dataset.currency] = input.value.trim();
      });
      try {
        await post("/api/rates", { rates });
        window.location.reload();
      } catch (error) {
        flash(error.message);
      }
    });
  }

  const form = document.querySelector("#import-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fileInput = form.querySelector('input[type="file"]');
      if (!fileInput || !fileInput.files.length) {
        flash("请选择文件");
        return;
      }
      const data = new FormData(form);
      try {
        await post("/api/statements/import", data);
        window.location.reload();
      } catch (error) {
        flash(error.message);
      }
    });
  }
});
