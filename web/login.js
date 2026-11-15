const form = document.querySelector("#login");
const password = document.querySelector("#password");
const message = document.querySelector("#login-message");
const submit = form.querySelector("button");

form.addEventListener("submit", async event => {
  event.preventDefault();
  submit.disabled = true;
  message.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Local-App": "1"},
      body: JSON.stringify({password: password.value}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error);
    window.location.replace("/");
  } catch (error) {
    message.textContent = error.message;
    password.select();
  } finally {
    submit.disabled = false;
  }
});
