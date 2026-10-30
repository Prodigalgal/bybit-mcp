document.querySelectorAll("time[data-timestamp]").forEach((el) => {
  el.textContent = new Date(
    Number(el.dataset.timestamp) * 1000,
  ).toLocaleString();
});
document.querySelector("#consent-form")?.addEventListener("submit", (event) => {
  const form = event.currentTarget;
  if (form.dataset.submitting) {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "true";
  // Keep the submitter enabled so its decision is included in the POST.
  if (event.submitter) {
    event.submitter.setAttribute("aria-disabled", "true");
    event.submitter.textContent =
      event.submitter.value === "verify" ? "正在验证…" : "正在处理…";
  }
});
