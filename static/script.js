async function checkHealth() {
  const out = document.getElementById("health-output");
  out.textContent = "Consultando...";
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    out.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

async function simulateWebhook() {
  const out = document.getElementById("webhook-output");
  out.textContent = "Enviando...";
  const phone = document.getElementById("phone").value;
  const message = document.getElementById("message").value;
  const phoneNumberId = document.getElementById("phone-number-id").value;
  try {
    const res = await fetch("/api/dev/simulate-webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        customer_phone: phone,
        message_text: message,
        phone_number_id: phoneNumberId,
      }),
    });
    const data = await res.json();
    out.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

function useLastTenantPhoneNumberId() {
  const info = document.getElementById("last-tenant-info");
  if (!lastPhoneNumberId) {
    info.textContent = "Todavía no has generado ningún tenant — usa la sección 3 primero.";
    return;
  }
  document.getElementById("phone-number-id").value = lastPhoneNumberId;
  info.textContent = `Usando phone_number_id del tenant ${lastTenantId} — ahora "Enviar webhook simulado" le llegará a ese mismo negocio.`;
}

async function generateAndTest() {
  const out = document.getElementById("jwt-output");
  out.textContent = "Generando JWT...";
  try {
    const jwtRes = await fetch("/api/dev/generate-test-jwt", { method: "POST" });
    const jwtData = await jwtRes.json();

    lastTenantId = jwtData.tenant_id;
    lastAccessToken = jwtData.access_token;
    lastPhoneNumberId = jwtData.phone_number_id;
    document.getElementById("last-tenant-info").textContent =
      `Último tenant generado: ${lastTenantId} (phone_number_id: ${lastPhoneNumberId})`;

    const protectedRes = await fetch("/api/conversations", {
      headers: { Authorization: "Bearer " + jwtData.access_token },
    });
    const protectedData = await protectedRes.json();

    out.textContent =
      "Tenant generado: " + jwtData.tenant_id +
      "\n\nRespuesta del endpoint protegido:\n" +
      JSON.stringify(protectedData, null, 2);
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

// ===== Plantillas de n8n =====
let allTemplates = [];
let selectedTemplateId = null;
let lastTenantId = null;
let lastAccessToken = null;
let lastPhoneNumberId = null;

async function loadTemplates() {
  const gallery = document.getElementById("templates-gallery");
  try {
    const res = await fetch("/api/templates");
    const data = await res.json();
    allTemplates = data.templates;

    if (allTemplates.length === 0) {
      gallery.innerHTML = "<p class='hint'>No hay plantillas en la carpeta /templates todavía.</p>";
      return;
    }

    gallery.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "templates-grid";

    allTemplates.forEach((t) => {
      const item = document.createElement("div");
      item.className = "template-item";
      item.id = "template-item-" + t.id;
      item.innerHTML = `<h4>${escapeHtml(t.name)}</h4><p>${escapeHtml(t.description)}</p>`;
      item.onclick = () => selectTemplate(t.id);
      grid.appendChild(item);
    });

    gallery.appendChild(grid);
  } catch (e) {
    gallery.innerHTML = "<p class='hint'>Error al cargar plantillas: " + e.message + "</p>";
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function selectTemplate(templateId) {
  selectedTemplateId = templateId;

  document.querySelectorAll(".template-item").forEach((el) => el.classList.remove("selected"));
  document.getElementById("template-item-" + templateId).classList.add("selected");

  const template = allTemplates.find((t) => t.id === templateId);
  document.getElementById("template-config-title").textContent = template.name;
  document.getElementById("template-config-desc").textContent = template.description;

  const form = document.getElementById("template-config-form");
  form.innerHTML = "";

  template.variables.forEach((v) => {
    const field = document.createElement("div");
    field.className = "form-field";

    const label = document.createElement("label");
    label.textContent = v.label + (v.required ? " *" : "");
    field.appendChild(label);

    let input;
    if (v.type === "select") {
      input = document.createElement("select");
      (v.options || []).forEach((opt) => {
        const optionEl = document.createElement("option");
        optionEl.value = opt;
        optionEl.textContent = opt;
        input.appendChild(optionEl);
      });
    } else if (v.type === "textarea") {
      input = document.createElement("textarea");
    } else {
      input = document.createElement("input");
      input.type = v.type === "number" ? "number" : "text";
    }
    input.id = "var-" + v.key;
    input.dataset.key = v.key;
    field.appendChild(input);
    form.appendChild(field);
  });

  document.getElementById("template-config").style.display = "block";
}

async function deployTemplate() {
  const out = document.getElementById("deploy-output");

  if (!selectedTemplateId) {
    out.textContent = "Selecciona una plantilla primero.";
    return;
  }
  if (!lastAccessToken) {
    out.textContent = "Primero genera un JWT de prueba en la sección 3 (necesitas un tenant_id válido).";
    return;
  }

  const form = document.getElementById("template-config-form");
  const variables = {};
  form.querySelectorAll("input, select, textarea").forEach((el) => {
    variables[el.dataset.key] = el.value;
  });

  out.textContent = "Desplegando en n8n...";
  try {
    const res = await fetch(`/api/templates/${selectedTemplateId}/deploy`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + lastAccessToken,
      },
      body: JSON.stringify({ variables }),
    });
    const data = await res.json();
    out.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
}

// Se ejecuta al cargar la página
loadTemplates();
