let activeProject = null;
const projectPicker = document.getElementById("projectPicker");
const projectDelete = document.getElementById("deleteProjectButton");
const editDialog = document.getElementById("editRequirementDialog");
let editingRequirementId = null;

async function configureAuthentication() {
  const status = await request("/api/auth/status");
  if (!status.required) return true;
  if (status.user) {
    document.getElementById("logoutButton").classList.remove("is-hidden");
    document.getElementById("projectState").textContent = status.user.username + " · " + status.user.role;
    return true;
  }
  const main = document.querySelector("main");
  for (const child of main.children) if (child.id !== "loginPanel") { child.dataset.authWasHidden = String(child.classList.contains("is-hidden")); child.classList.add("auth-hidden", "is-hidden"); }
  document.getElementById("loginPanel").classList.remove("is-hidden");
  if (!status.configured_users) document.getElementById("loginNotice").textContent = "Authentication is enabled, but no users are configured. Set APP_USERS_JSON before signing in.";
  document.getElementById("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await request("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: document.getElementById("loginUsername").value, password: document.getElementById("loginPassword").value }) });
      document.getElementById("loginPanel").classList.add("is-hidden");
      for (const child of main.children) if (child.classList.contains("auth-hidden")) { child.classList.remove("auth-hidden"); if (child.dataset.authWasHidden === "false") child.classList.remove("is-hidden"); delete child.dataset.authWasHidden; }
      document.getElementById("logoutButton").classList.remove("is-hidden");
      document.getElementById("loginPassword").value = "";
      document.getElementById("projectState").textContent = result.username + " · " + result.role;
      await refreshProjectPicker();
    } catch (error) { toast(error.message); }
  });
  return false;
}
document.getElementById("logoutButton").addEventListener("click", async () => { await request("/api/auth/logout", { method: "POST" }); location.reload(); });

async function refreshProjectPicker(selectId) {
  try {
    const projects = await request("/api/projects");
    projectPicker.innerHTML = '<option value="">Select saved project…</option>' + projects.map((project) => '<option value="' + escapeHtml(project.id) + '">' + escapeHtml(project.functionality) + ' · ' + project.requirements + ' req</option>').join("");
    if (selectId) projectPicker.value = selectId;
  } catch (error) { toast("Could not load saved projects: " + error.message); }
}

function activateProject(project) {
  activeProject = project;
  projectId = project.id;
  projectPicker.value = project.id;
  projectDelete.disabled = false;
  document.getElementById("projectState").textContent = "Project selected";
  const exportLink = document.getElementById("exportLink");
  exportLink.href = "/api/projects/" + project.id + "/export";
  exportLink.classList.remove("is-hidden");
  for (const [id, format] of [["srsExport", "md"], ["csvExport", "csv"]]) {
    const link = document.getElementById(id);
    link.href = "/api/projects/" + project.id + "/documents/" + format;
    link.classList.remove("is-hidden");
  }
  if (project.requirements && project.requirements.length) {
    displayRequirements(project);
    displayIssues(project.quality_issues || [], project.clarification_questions || []);
    document.getElementById("sdlcPanel").classList.remove("is-hidden");
    if (project.sdlc) displaySdlc(project.sdlc);
  }
}

projectPicker.addEventListener("change", async () => {
  if (!projectPicker.value) return;
  try { activateProject(await request("/api/projects/" + projectPicker.value)); }
  catch (error) { toast(error.message); }
});

document.getElementById("createProject").addEventListener("click", () => {
  const initialId = projectId;
  const started = Date.now();
  const poll = setInterval(async () => {
    if (projectId && projectId !== initialId) {
      clearInterval(poll);
      try { await refreshProjectPicker(projectId); activateProject(await request("/api/projects/" + projectId)); }
      catch (error) { toast(error.message); }
    } else if (Date.now() - started > 30_000) clearInterval(poll);
  }, 200);
});

projectDelete.addEventListener("click", async () => {
  if (!projectId || !confirm("Delete this project and its indexed evidence? This cannot be undone.")) return;
  try {
    await request("/api/projects/" + projectId, { method: "DELETE" });
    projectId = null; activeProject = null; projectPicker.value = "";
    projectDelete.disabled = true;
    document.getElementById("exportLink").classList.add("is-hidden");
    document.getElementById("requirementsPanel").classList.add("is-hidden");
    document.getElementById("qualityPanel").classList.add("is-hidden");
    document.getElementById("documentsPanel").classList.add("is-hidden");
    await refreshProjectPicker();
    toast("Project and indexed evidence deleted.");
  } catch (error) { toast(error.message); }
});

const originalDisplayRequirements = displayRequirements;
displayRequirements = function(project) {
  activeProject = project;
  originalDisplayRequirements(project);
  for (const link of [document.getElementById("srsExport"), document.getElementById("csvExport")]) link.classList.remove("is-hidden");
  document.getElementById("srsExport").href = "/api/projects/" + project.id + "/documents/md";
  document.getElementById("csvExport").href = "/api/projects/" + project.id + "/documents/csv";
  document.querySelectorAll("#requirementsList .requirement").forEach((card) => {
    if (card.querySelector(".edit-req")) return;
    const id = card.querySelector(".req-id").textContent;
    const actions = document.createElement("div");
    actions.className = "requirement-actions";
    actions.innerHTML = '<button type="button" class="edit-req" data-id="' + escapeHtml(id) + '">Edit</button><button type="button" class="delete-req danger-button" data-id="' + escapeHtml(id) + '">Delete</button>';
    card.querySelector(".requirement-head").append(actions);
  });
};

const originalDisplayIssues = displayIssues;
displayIssues = function(issues, questions) {
  originalDisplayIssues(issues, questions);
  const queue = document.querySelector(".clarification-queue");
  if (!queue || !activeProject) return;
  const pending = questions || [];
  queue.innerHTML = '<p class="eyebrow">Clarification queue · answers are saved as project evidence</p>' + pending.map((question, index) => {
    const match = question.match(/^(REQ-[0-9]+):\s*/);
    const requirementId = match ? match[1] : "";
    return '<div class="clarification-item"><p>' + escapeHtml(question) + '</p><textarea aria-label="Clarification response ' + (index + 1) + '" placeholder="Record the stakeholder response"></textarea><button type="button" class="save-clarification" data-question="' + escapeHtml(question) + '" data-requirement="' + requirementId + '">Save response</button></div>';
  }).join("");
};

document.addEventListener("click", async (event) => {
  const edit = event.target.closest(".edit-req");
  if (edit) {
    const item = activeProject.requirements.find((r) => r.id === edit.dataset.id);
    if (!item) return;
    editingRequirementId = item.id;
    document.getElementById("editStatement").value = item.statement;
    document.getElementById("editJustification").value = item.business_justification;
    document.getElementById("editAcceptance").value = item.acceptance_criteria.join("\n");
    document.getElementById("editPriority").value = item.priority;
    document.getElementById("editRisk").value = item.risk_level;
    editDialog.showModal();
    return;
  }
  const remove = event.target.closest(".delete-req");
  if (remove && confirm("Delete requirement " + remove.dataset.id + "?")) {
    try {
      await request("/api/projects/" + projectId + "/requirements/" + remove.dataset.id, { method: "DELETE" });
      await reloadProject(); toast("Requirement deleted.");
    } catch (error) { toast(error.message); }
    return;
  }
  const answer = event.target.closest(".save-clarification");
  if (answer) {
    const response = answer.parentElement.querySelector("textarea").value.trim();
    if (!response) { toast("Enter the stakeholder response first."); return; }
    try {
      await request("/api/projects/" + projectId + "/clarifications", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: answer.dataset.question, response, requirement_id: answer.dataset.requirement || null }) });
      await reloadProject(); toast("Clarification saved and indexed as project evidence.");
    } catch (error) { toast(error.message); }
  }
});

async function reloadProject() {
  const project = await request("/api/projects/" + projectId);
  activateProject(project);
  displayIssues(project.quality_issues || [], project.clarification_questions || []);
  await refreshProjectPicker(project.id);
}

document.getElementById("editRequirementForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const item = activeProject.requirements.find((r) => r.id === editingRequirementId);
  if (!item) return;
  try {
    await request("/api/projects/" + projectId + "/requirements/" + editingRequirementId, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ statement: document.getElementById("editStatement").value.trim(), business_justification: document.getElementById("editJustification").value.trim(), acceptance_criteria: document.getElementById("editAcceptance").value.split("\n").map((x) => x.trim()).filter(Boolean), priority: document.getElementById("editPriority").value, risk_level: document.getElementById("editRisk").value })
    });
    editDialog.close(); await reloadProject(); toast("Requirement saved and returned to human review.");
  } catch (error) { toast(error.message); }
});
document.getElementById("cancelRequirementEdit").addEventListener("click", () => editDialog.close());
configureAuthentication().then((ready) => ready && refreshProjectPicker()).catch((error) => toast(error.message));
